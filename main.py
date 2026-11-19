import os
import logging
import threading
import asyncio
import re
import urllib.parse
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests
from datetime import datetime, timedelta, timezone, time as dt_time
from http.server import HTTPServer, BaseHTTPRequestHandler
from telegram import Update, BotCommand, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    MessageHandler,
    CommandHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
)
from google import genai
from google.genai import types
from groq import Groq
from database import supabase
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)

# AI & Search Clients Initialization
gemini_client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
groq_client = Groq(api_key=os.getenv("GROQ_API_KEY"))
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ADMIN_USER_ID = int(os.getenv("MY_TELEGRAM_USER_ID", "0"))

# Myanmar Standard Time (UTC+6:30) and 30-Day Retention
MM_TZ = timezone(timedelta(hours=6, minutes=30))
RETENTION_DAYS = 30


# --- Dummy Health Check Server for Render ---
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Telegram AI Agent (Maya) is running!")


def run_health_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    server.serve_forever()


# --- Safe Long Message Sender (Prevents Telegram 'Text is too long' Error) ---
async def send_long_message(message, text: str, chunk_size: int = 3500):
    if not text or not str(text).strip():
        await message.reply_text("လောလောဆယ် အဖြေပြန်ထုတ်ရာတွင် အခက်အခဲ ရှိနေပါသည်။ နောက်မှ ထပ်မေးကြည့်ပါရှင်။")
        return

    clean_text = str(text).strip()
    if len(clean_text) <= chunk_size:
        await message.reply_text(clean_text)
        return

    for i in range(0, len(clean_text), chunk_size):
        part = clean_text[i:i + chunk_size]
        await message.reply_text(part)


# --- Auto-Cleanup Old Raw Logs (Older than 30 days) ---
def cleanup_old_raw_logs():
    try:
        cutoff_dt = datetime.now(MM_TZ) - timedelta(days=RETENTION_DAYS)
        supabase.table("raw_logs").delete().lt("created_at", cutoff_dt.isoformat()).execute()
    except Exception as e:
        print(f"Error cleaning up old raw_logs: {e}")


# --- Supabase Chat Memory Helpers ---
def save_chat_message(user_id: int, role: str, content: str):
    try:
        supabase.table("chat_messages").insert({
            "user_id": user_id,
            "role": role,
            "content": content
        }).execute()
    except Exception as e:
        print(f"Error saving chat message: {e}")


def extract_search_terms(query_text: str) -> list:
    terms = []
    for raw in query_text.split():
        cleaned = re.sub(r"[^\w\u1000-\u109F]", "", raw)
        if len(cleaned) >= 2:
            terms.append(cleaned)

    compact = re.sub(r"\s+", "", query_text)
    if len(compact) >= 4:
        for i in range(0, min(len(compact), 32), 4):
            chunk = compact[i:i + 5]
            if len(chunk) >= 3:
                terms.append(chunk)

    unique_terms = []
    for t in terms:
        if t not in unique_terms:
            unique_terms.append(t)
    return unique_terms[:6]


def recall_relevant_history(user_id: int, query_text: str, recent_limit: int = 6, search_limit: int = 4) -> str:
    try:
        recent_res = (
            supabase.table("chat_messages")
            .select("role, content, created_at")
            .eq("user_id", user_id)
            .order("id", desc=True)
            .limit(recent_limit)
            .execute()
        )
        recent_rows = list(reversed(recent_res.data or []))

        search_terms = extract_search_terms(query_text)
        matched_rows = []
        seen = set((r["role"], r["content"]) for r in recent_rows)

        for term in search_terms:
            search_res = (
                supabase.table("chat_messages")
                .select("role, content, created_at")
                .eq("user_id", user_id)
                .ilike("content", f"%{term}%")
                .order("id", desc=True)
                .limit(search_limit)
                .execute()
            )
            for r in reversed(search_res.data or []):
                key = (r["role"], r["content"])
                if key not in seen:
                    seen.add(key)
                    matched_rows.append(r)

        sections = []
        if matched_rows:
            matched_text = "\n".join([f"[{m['role']}]: {m['content']}" for m in matched_rows[-8:]])
            sections.append(f"--- Relevant Past History ---\n{matched_text}")

        if recent_rows:
            recent_text = "\n".join([f"[{r['role']}]: {r['content']}" for r in recent_rows])
            sections.append(f"--- Recent Conversation ---\n{recent_text}")

        return "\n\n".join(sections)
    except Exception as e:
        print(f"Error recalling chat history: {e}")
        return ""


# --- 3-Tier AI Fallback Helper (Gemini 3.5 Flash-Lite -> Groq -> OpenRouter) ---
def call_ai_with_3tier_fallback(prompt_text, image_bytes=None, mime_type=None, use_google_search=False):
    errors = []

    # 1st Attempt: Gemini 3.5 Flash-Lite (with optional Google Search Grounding)
    try:
        if image_bytes:
            contents = [
                types.Part.from_bytes(data=bytes(image_bytes), mime_type=mime_type or 'image/jpeg'),
                prompt_text
            ]
        else:
            contents = prompt_text

        config = None
        if use_google_search:
            config = types.GenerateContentConfig(
                tools=[types.Tool(google_search=types.GoogleSearch())]
            )

        response = gemini_client.models.generate_content(
            model="gemini-3.5-flash-lite",
            contents=contents,
            config=config
        )
        if response and response.text and response.text.strip():
            return response.text.strip()
    except Exception as e:
        errors.append(f"1. Gemini Error: {str(e)}")

    # 2nd Attempt: Groq (Llama 3.1 8B Instant)
    try:
        completion = groq_client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[
                {"role": "system", "content": "သင်သည် မြန်မာဘာသာဖြင့် တိုတိုရှင်းရှင်းနှင့် ထိရောက်စွာ ဖြေဆိုပေးသော AI Assistant ဖြစ်သည်။"},
                {"role": "user", "content": prompt_text}
            ],
            temperature=0.1,
        )
        if completion and completion.choices and completion.choices[0].message.content:
            txt = completion.choices[0].message.content.strip()
            if txt:
                return txt
    except Exception as e:
        errors.append(f"2. Groq Error: {str(e)}")

    # 3rd Attempt: OpenRouter Auto-Free Router (fast 8s timeout)
    try:
        headers = {
            "Authorization": f"Bearer {OPENROUTER_API_KEY}",
            "Content-Type": "application/json",
        }
        data = {
            "model": "openrouter/free",
            "messages": [
                {"role": "system", "content": "သင်သည် မြန်မာဘာသာဖြင့် တိုတိုရှင်းရှင်းနှင့် ထိရောက်စွာ ဖြေဆိုပေးသော AI Assistant ဖြစ်သည်။"},
                {"role": "user", "content": prompt_text}
            ]
        }
        response = requests.post("https://openrouter.ai/api/v1/chat/completions", headers=headers, json=data, timeout=8)
        res_json = response.json()
        if "choices" in res_json and len(res_json["choices"]) > 0:
            txt = (res_json["choices"][0]["message"].get("content") or "").strip()
            if txt:
                return txt
        else:
            errors.append(f"3. OpenRouter Response: {res_json}")
    except Exception as e:
        errors.append(f"3. OpenRouter Error: {str(e)}")

    return "လောလောဆယ် AI ဝန်ဆောင်မှုများ နှေးကွေးနေပါသည်။ ခဏနေမှ ထပ်မံမေးမြန်းကြည့်ပါရှင်။"


# --- Fast & Comprehensive Web Search Helpers (Tavily + Google News RSS in Parallel) ---
def _tavily_worker(search_query: str, max_results: int = 5) -> str:
    if not TAVILY_API_KEY:
        return ""
    try:
        resp = requests.post(
            "https://api.tavily.com/search",
            json={
                "api_key": TAVILY_API_KEY,
                "query": search_query,
                "search_depth": "basic",
                "topic": "general",
                "max_results": max_results,
                "include_answer": True,
            },
            timeout=6,
        )
        if resp.status_code != 200:
            return ""
        data = resp.json()
        lines = []
        if data.get("answer"):
            lines.append(f"[Tavily Answer]: {data['answer']}")
        for r in data.get("results", []):
            title = r.get("title", "")
            content = (r.get("content", "") or "")[:300]
            lines.append(f"- {title}: {content}")
        return "\n".join(lines)
    except Exception as e:
        print(f"Tavily fast search error: {e}")
        return ""


def _rss_worker(queries: list, per_query: int = 4) -> str:
    collected_items = []
    seen_titles = set()
    for q in queries:
        try:
            encoded_q = urllib.parse.quote(q)
            url = f"https://news.google.com/rss/search?q={encoded_q}&hl=en-US&gl=MM&ceid=MM:en"
            resp = requests.get(url, timeout=6)
            resp.raise_for_status()
            root = ET.fromstring(resp.content)
            for item in root.findall(".//channel/item")[:per_query]:
                title_el = item.find("title")
                pub_el = item.find("pubDate")
                title = (title_el.text or "").strip() if title_el is not None and title_el.text else ""
                published = (pub_el.text or "").strip() if pub_el is not None and pub_el.text else ""
                if title and title not in seen_titles:
                    seen_titles.add(title)
                    collected_items.append(f"- {title} ({published})")
        except Exception as e:
            print(f"RSS fast error for '{q}': {e}")
    return "\n".join(collected_items[:10])


def fetch_combined_web_context(tavily_query: str, rss_queries: list = None, max_results: int = 5) -> str:
    results_map = {"tavily": "", "rss": ""}
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = {executor.submit(_tavily_worker, tavily_query, max_results): "tavily"}
        if rss_queries:
            futures[executor.submit(_rss_worker, rss_queries, 4)] = "rss"

        for fut in as_completed(futures, timeout=7):
            key = futures[fut]
            try:
                results_map[key] = fut.result() or ""
            except Exception:
                pass

    parts = []
    if results_map["tavily"]:
        parts.append(f"--- Web Search (Tavily) ---\n{results_map['tavily']}")
    if results_map["rss"]:
        parts.append(f"--- News Headlines (RSS) ---\n{results_map['rss']}")
    return "\n\n".join(parts)


# --- Real-Time Weather Helper (Open-Meteo with Fast Web Fallback) ---
MYANMAR_CITIES = {
    "yangon": ("ရန်ကုန် (Yangon)", 16.8661, 96.1951),
    "ရန်ကုန်": ("ရန်ကုန် (Yangon)", 16.8661, 96.1951),
    "mandalay": ("မန္တလေး (Mandalay)", 21.9588, 96.0891),
    "မန္တလေး": ("မန္တလေး (Mandalay)", 21.9588, 96.0891),
    "naypyidaw": ("နေပြည်တော် (Naypyidaw)", 19.7633, 96.0785),
    "နေပြည်တော်": ("နေပြည်တော် (Naypyidaw)", 19.7633, 96.0785),
    "bago": ("ပဲခူး (Bago)", 17.3352, 96.4814),
    "ပဲခူး": ("ပဲခူး (Bago)", 17.3352, 96.4814),
    "mawlamyine": ("မော်လမြိုင် (Mawlamyine)", 16.4905, 97.6283),
    "မော်လမြိုင်": ("မော်လမြိုင် (Mawlamyine)", 16.4905, 97.6283),
    "hpa-an": ("ဘားအံ (Hpa-An)", 16.8906, 97.6348),
    "ဘားအံ": ("ဘားအံ (Hpa-An)", 16.8906, 97.6348),
    "myawaddy": ("မြဝတီ (Myawaddy)", 16.6891, 98.5085),
    "မြဝတီ": ("မြဝတီ (Myawaddy)", 16.6891, 98.5085),
    "taunggyi": ("တောင်ကြီး (Taunggyi)", 20.7892, 97.0378),
    "တောင်ကြီး": ("တောင်ကြီး (Taunggyi)", 20.7892, 97.0378),
}

WEATHER_CODES = {
    0: "ကောင်းကင်ကြည်လင် (Clear sky)",
    1: "တိမ်အနည်းငယ်ရှိ (Mainly clear)",
    2: "တိမ်အသင့်အတင့်ရှိ (Partly cloudy)",
    3: "တိမ်ထူထပ် (Overcast)",
    45: "မြူဆိုင်း (Fog)",
    51: "မိုးဖွဲအနည်းငယ်ရွာ (Light drizzle)",
    61: "မိုးအနည်းငယ်ရွာ (Slight rain)",
    63: "မိုးအသင့်အတင့်ရွာ (Moderate rain)",
    65: "မိုးသည်းထန်စွာရွာ (Heavy rain)",
    80: "မိုးတစ်ပြိုက်နှစ်ပြိုက်ရွာ (Rain showers)",
    95: "မိုးကြိုးမုန်တိုင်း (Thunderstorm)",
}


def get_realtime_weather(query_text: str) -> str:
    q_lower = query_text.lower()
    city_label, lat, lon = ("ရန်ကုန် (Yangon)", 16.8661, 96.1951)
    city_en = "Yangon"
    for key, info in MYANMAR_CITIES.items():
        if key in q_lower:
            city_label, lat, lon = info
            city_en = info[0].split("(")[-1].replace(")", "").strip()
            break

    now_str = datetime.now(MM_TZ).strftime("%Y-%m-%d %H:%M MMT")
    today_date = datetime.now(MM_TZ).strftime("%Y-%m-%d")

    try:
        url = (
            f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}"
            f"&current=temperature_2m,relative_humidity_2m,weather_code,wind_speed_10m"
            f"&daily=temperature_2m_max,temperature_2m_min,precipitation_probability_max"
            f"&timezone=Asia%2FYangon&forecast_days=2"
        )
        resp = requests.get(url, timeout=5)
        resp.raise_for_status()
        data = resp.json()

        curr = data.get("current", {})
        daily = data.get("daily", {})

        temp = curr.get("temperature_2m", "-")
        humidity = curr.get("relative_humidity_2m", "-")
        wind = curr.get("wind_speed_10m", "-")
        w_code = curr.get("weather_code", 0)
        condition = WEATHER_CODES.get(w_code, f"Weather code {w_code}")

        max_t = daily.get("temperature_2m_max", ["-"])[0]
        min_t = daily.get("temperature_2m_min", ["-"])[0]
        rain_prob = daily.get("precipitation_probability_max", ["-"])[0]

        return (
            f"🌤️ {city_label} ရာသီဥတု ({now_str})\n\n"
            f"• လက်ရှိအခြေအနေ: {condition}\n"
            f"• လက်ရှိအပူချိန်: {temp}°C (အမြင့်ဆုံး {max_t}°C / အနိမ့်ဆုံး {min_t}°C)\n"
            f"• မိုးရွာနိုင်ခြေ: {rain_prob}%\n"
            f"• စိုထိုင်းဆ: {humidity}% | လေတိုက်နှုန်း: {wind} km/h"
        )
    except Exception:
        web_weather = fetch_combined_web_context(f"{city_en} Myanmar weather forecast {today_date}", max_results=4)
        prompt = (
            f"ယနေ့ ({today_date}) {city_label} ရာသီဥတု အခြေအနေကို အောက်ပါ ရှာဖွေတွေ့ရှိချက်များမှ ထုတ်နုတ်ပြီး "
            f"မြန်မာဘာသာဖြင့် တိုတိုရှင်းရှင်း ဖြေပေးပါ။ (အချက်အလက် မရှိပါက မသိကြောင်း ရိုးသားစွာ ဖြေပါ။)\n\n{web_weather}"
        )
        return call_ai_with_3tier_fallback(prompt, use_google_search=True)


# --- Myanmar News Fetcher & Summarizer (/news & 9:00 AM Daily Job) ---
def get_myanmar_news_summary() -> str:
    now_str = datetime.now(MM_TZ).strftime("%Y-%m-%d %H:%M MMT")
    today_date = datetime.now(MM_TZ).strftime("%Y-%m-%d")

    web_context = fetch_combined_web_context(
        tavily_query=f"Myanmar latest news {today_date} Yangon gold exchange rate",
        rss_queries=["Myanmar news", "မြန်မာ သတင်း", "Yangon news"],
        max_results=5,
    )

    prompt = f"""
    လက်ရှိအချိန်: {now_str} (မြန်မာစံတော်ချိန်)
    Google Search နှင့် အောက်ပါ Web Search / RSS အချက်အလက်များကို ပေါင်းစပ်၍ ယနေ့ မြန်မာပြည်နှင့် ပတ်သက်သည့်
    နောက်ဆုံးရ အရေးကြီး သတင်းအစုံအလင် (ပြည်တွင်းသတင်း၊ စီးပွားရေး၊ ရွှေဈေး/ငွေလဲနှုန်း နှင့် ထူးခြားဖြစ်စဉ်များ) ကို
    မြန်မာဘာသာဖြင့် ရှင်းလင်းပြတ်သားသော Bullet point များဖြင့် စာလုံးရေ ၁,၅၀၀ အတွင်း တိုတိုရှင်းရှင်း အနှစ်ချုပ် တင်ပြပေးပါ။

    {web_context}
    """
    summary = call_ai_with_3tier_fallback(prompt, use_google_search=True)
    return f"📰 Myanmar News Summary ({now_str})\n\n{summary}"


# --- Fast & Comprehensive Universal Search + AI Answer ---
def answer_with_universal_search(user_id: int, user_message: str) -> str:
    now_str = datetime.now(MM_TZ).strftime("%Y-%m-%d %H:%M MMT")
    today_date = datetime.now(MM_TZ).strftime("%Y-%m-%d")
    memory_context = recall_relevant_history(user_id, user_message, recent_limit=5, search_limit=3)

    greetings = ["မင်္ဂလာပါ", "hi", "hello", "hey", "မာယာ", "ကျေးဇူး", "thanks", "thank you", "ok", "ဟုတ်"]
    is_simple_greeting = user_message.lower().strip() in greetings

    web_context = ""
    if not is_simple_greeting:
        clean_q = re.sub(r"[?？!]", "", user_message).strip()
        extra = ""
        rss_qs = None
        if "ရွှေ" in clean_q:
            extra = f" Myanmar gold price {today_date}"
            rss_qs = ["Myanmar gold price", "ရွှေဈေး"]
        elif "ဒေါ်လာ" in clean_q or "ငွေလဲ" in clean_q:
            extra = f" Myanmar USD exchange rate MMK {today_date}"
            rss_qs = ["Myanmar exchange rate USD MMK", "ဒေါ်လာဈေး"]
        elif any(k in clean_q.lower() for k in ["premier league", "ပွဲစဉ်", "chelsea", "man u", "manchester"]):
            extra = f" Premier League fixtures results {today_date}"
            rss_qs = ["Premier League fixtures results"]

        web_context = fetch_combined_web_context(f"{clean_q}{extra}", rss_queries=rss_qs, max_results=5)

    system_prompt = (
        f"သင်သည် မာယာ (Maya) အမည်ရှိ ကိုယ်ပိုင် အလုပ်အမှုဆောင် AI Assistant ဖြစ်သည်။\n"
        f"လက်ရှိအချိန်: {now_str} (မြန်မာစံတော်ချိန်)\n"
        f"စည်းမျဉ်းများ:\n"
        f"1. Web Search ရလဒ်များနှင့် Google Search Tool ကို အသုံးပြု၍ သတင်းအစုံ၊ အားကစားပွဲစဉ်/ရလဒ် အဟောင်းအသစ်၊ ဈေးနှုန်း၊ နည်းပညာနှင့် ဗဟုသုတ မေးခွန်းများကို တိကျပြည့်စုံစွာ မြန်မာဘာသာဖြင့် ဖြေကြားပါ။\n"
        f"2. အဖြေကို အဓိက အချက်အလက် (၃ - ၅ ချက်) သာ ဖော်ပြပြီး စာလုံးရေ ၁,၅၀၀ အတွင်း တိုတိုရှင်းရှင်းနှင့် မြန်မာစာလုံး မှန်ကန်သန့်ရှင်းစွာ ဖြေကြားပါ။\n"
        f"3. Web Search ရလဒ် ပါရှိပါက အတိတ် Chat History ထဲက ဈေးနှုန်း/ရာသီဥတု/ပွဲစဉ် အချက်အလက်ဟောင်းများကို မကိုးကားဘဲ လက်ရှိ Web Search ရလဒ်ကိုသာ ဦးစားပေး ဖြေပါ။\n"
        f"4. အချက်အလက် အတိအကျ ရှာမတွေ့ပါက မမှန်မကန် မဖြေဘဲ မတွေ့ရှိကြောင်း ရိုးသားစွာ ရှင်းပြပါ။ Task မှတ်လိုပါက /addtask ဖြင့် မှတ်နိုင်သည်။\n"
    )

    full_prompt = (
        f"{system_prompt}\n"
        f"{web_context or '--- Web Search Results ---\nNone'}\n\n"
        f"{memory_context}\n\n"
        f"လက်ရှိ မေးခွန်း/စကား: {user_message}"
    )

    return call_ai_with_3tier_fallback(full_prompt, use_google_search=True)


# --- Scheduled Daily 9:00 AM MMT Job ---
async def scheduled_morning_news_job(context: ContextTypes.DEFAULT_TYPE):
    if not ADMIN_USER_ID:
        return
    try:
        news_text = await asyncio.to_thread(get_myanmar_news_summary)
        save_chat_message(ADMIN_USER_ID, "assistant", news_text)
        for i in range(0, len(news_text), 3500):
            await context.bot.send_message(chat_id=ADMIN_USER_ID, text=news_text[i:i + 3500])
    except Exception as e:
        print(f"Error sending scheduled morning news: {e}")


# --- 1. Personal Tasks Helpers ---
def add_task(title: str) -> dict:
    supabase.table("tasks").insert({"title": title, "status": "pending"}).execute()
    return {"status": "success", "message": f"Task added: {title}"}


def list_tasks() -> list:
    res = supabase.table("tasks").select("*").order("id").execute()
    return res.data


def get_formatted_tasks() -> str:
    tasks = list_tasks()
    if not tasks:
        return "No tasks found."
    return "Current Tasks:\n" + "\n".join([f"- ID {t['id']}: {t['title']} ({t['status']})" for t in tasks])


# --- 2. Group Summary & Report Tools ---
def get_available_groups() -> list:
    cleanup_old_raw_logs()
    thirty_days_ago_iso = (datetime.now(MM_TZ) - timedelta(days=RETENTION_DAYS)).isoformat()
    res = supabase.table("raw_logs").select("group_name").gte("created_at", thirty_days_ago_iso).execute()
    groups = sorted(list({r["group_name"] for r in (res.data or []) if r.get("group_name")}))
    return groups


def get_date_bounds(day_mode: str):
    now_mm = datetime.now(MM_TZ)
    if day_mode == "yesterday":
        target_date = (now_mm - timedelta(days=1)).date()
        label = f"Yesterday ({target_date})"
    else:
        target_date = now_mm.date()
        label = f"Today ({target_date})"

    start_dt = datetime(target_date.year, target_date.month, target_date.day, 0, 0, 0, tzinfo=MM_TZ)
    end_dt = start_dt + timedelta(days=1)
    return start_dt.isoformat(), end_dt.isoformat(), label


def parse_date_range_from_text(text: str):
    text_lower = text.lower()
    if "မနေ့" in text_lower or "yesterday" in text_lower:
        return get_date_bounds("yesterday")
    if "ဒီနေ့" in text_lower or "today" in text_lower:
        return get_date_bounds("today")

    match = re.search(r"\b(\d{4}-\d{2}-\d{2})\b", text_lower)
    if match:
        try:
            target_date = datetime.strptime(match.group(1), "%Y-%m-%d").date()
            start_dt = datetime(target_date.year, target_date.month, target_date.day, 0, 0, 0, tzinfo=MM_TZ)
            end_dt = start_dt + timedelta(days=1)
            return start_dt.isoformat(), end_dt.isoformat(), f"{target_date}"
        except ValueError:
            pass

    return None, None, None


def get_group_report(user_query: str = "", exact_group_name: str = None, day_mode: str = None) -> str:
    cleanup_old_raw_logs()

    if day_mode in ["today", "yesterday"]:
        start_iso, end_iso, date_label = get_date_bounds(day_mode)
    else:
        start_iso, end_iso, date_label = parse_date_range_from_text(user_query)

    thirty_days_ago_iso = (datetime.now(MM_TZ) - timedelta(days=RETENTION_DAYS)).isoformat()
    query = supabase.table("raw_logs").select("*").gte("created_at", thirty_days_ago_iso).order("id")

    if start_iso and end_iso:
        query = query.gte("created_at", start_iso).lt("created_at", end_iso)
    elif not exact_group_name:
        query = query.eq("is_read", False)

    logs_res = query.execute()
    logs = logs_res.data or []

    matched_group_name = exact_group_name
    if exact_group_name:
        logs = [l for l in logs if l.get("group_name") == exact_group_name]
    elif logs:
        all_groups = list({l.get("group_name") for l in logs if l.get("group_name")})
        q_lower = user_query.lower()
        for g_name in all_groups:
            g_words = [w for w in g_name.lower().split() if len(w) >= 3]
            if g_name.lower() in q_lower or any(w in q_lower for w in g_words):
                matched_group_name = g_name
                logs = [l for l in logs if l.get("group_name") == g_name]
                break

    if not logs:
        scope_desc = f" [{matched_group_name}]" if matched_group_name else ""
        if date_label:
            return f"No logs found for {date_label}{scope_desc}."
        return f"No unread messages found{scope_desc}."

    raw_text = "\n".join(
        [f"[{l['group_name']} | {l['sender_name']} | {str(l.get('created_at', ''))[:16]}]: {l['content_summary']}" for l in logs]
    )

    header_info = f"Group: {matched_group_name or 'All Groups'} | Period: {date_label or 'Unread'}"

    prompt = f"""
    အောက်ပါတို့မှာ Telegram Group များမှ ရရှိထားသော မက်ဆေ့ခ်ျများနှင့် ပုံ/ဖိုင် အနှစ်ချုပ်များ ဖြစ်သည်။
    ({header_info})
    နေ့စဉ် တွေ့နေကျ ရိုးရိုးနှုတ်ဆက်စကားများ၊ အရေးမပါသော စကားပြောဆိုမှုများကို လုံးဝ ဖယ်ထုတ်ပါ။
    အောက်ပါ အချက်များကိုသာ ဦးစားပေး၍ အနှစ်ချုပ် Report တိုတိုရှင်းရှင်းနှင့် ထိရောက်စွာ ထုတ်ပေးပါ:
    1. ပုံမှန်မဟုတ်သော ပြဿနာများ (Issues / Blockers)
    2. ဆုံးဖြတ်ချက် အသစ်များနှင့် ညွှန်ကြားချက်များ
    3. အရေးကြီးသော လုပ်ငန်းတာဝန်များ (Action Items)

    ဒေတာများ:
    {raw_text}
    """

    report_text = call_ai_with_3tier_fallback(prompt, use_google_search=False)

    supabase.table("daily_reports").insert({"report_content": f"[{header_info}]\n{report_text}"}).execute()

    if not date_label and not exact_group_name:
        for l in logs:
            supabase.table("raw_logs").update({"is_read": True}).eq("id", l["id"]).execute()

    return f"{header_info}\n\n{report_text}"


# --- 3. Quick Menu & Command Handlers (English Labels) ---
def build_main_menu_keyboard() -> InlineKeyboardMarkup:
    keyboard = [
        [
            InlineKeyboardButton("Unread Summary", callback_data="cmd_unread_summary"),
            InlineKeyboardButton("Today Summary", callback_data="cmd_today_summary"),
        ],
        [
            InlineKeyboardButton("Yesterday Summary", callback_data="cmd_yesterday_summary"),
            InlineKeyboardButton("Select Group", callback_data="cmd_list_groups"),
        ],
        [
            InlineKeyboardButton("News Summary", callback_data="cmd_news_summary"),
            InlineKeyboardButton("Tasks List", callback_data="cmd_list_tasks"),
        ],
    ]
    return InlineKeyboardMarkup(keyboard)


def build_groups_keyboard() -> tuple[str, InlineKeyboardMarkup | None]:
    groups = get_available_groups()
    if not groups:
        return "No groups found within the last 30 days.", None

    keyboard = []
    for idx, g_name in enumerate(groups):
        keyboard.append([InlineKeyboardButton(g_name, callback_data=f"grp_sel_{idx}")])

    return "Select a group:", InlineKeyboardMarkup(keyboard)


def build_group_date_keyboard(group_idx: int, group_name: str) -> tuple[str, InlineKeyboardMarkup]:
    keyboard = [
        [
            InlineKeyboardButton("Today", callback_data=f"grp_day_{group_idx}_today"),
            InlineKeyboardButton("Yesterday", callback_data=f"grp_day_{group_idx}_yesterday"),
        ],
        [
            InlineKeyboardButton("Back to Groups", callback_data="cmd_list_groups"),
        ],
    ]
    return f"Group: {group_name}\nSelect date for daily report:", InlineKeyboardMarkup(keyboard)


async def handle_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message or message.chat.type != "private":
        return

    if message.from_user.id != ADMIN_USER_ID:
        await message.reply_text("Unauthorized.")
        return

    parts = message.text.split(maxsplit=1)
    cmd = parts[0].lower()
    arg = parts[1].strip() if len(parts) > 1 else ""

    if cmd in ["/start", "/menu"]:
        await message.reply_text(
            "Quick Menu:",
            reply_markup=build_main_menu_keyboard()
        )
        return

    if cmd == "/addtask":
        if not arg:
            await message.reply_text("Usage: /addtask <task description>")
            return
        add_task(arg)
        await message.reply_text(f"Task added: {arg}")
        return

    await context.bot.send_chat_action(chat_id=message.chat_id, action="typing")

    if cmd == "/summary":
        reply_text = get_group_report("unread summary")
        await send_long_message(message, reply_text)
    elif cmd == "/today":
        reply_text = get_group_report("today summary", day_mode="today")
        await send_long_message(message, reply_text)
    elif cmd == "/yesterday":
        reply_text = get_group_report("yesterday summary", day_mode="yesterday")
        await send_long_message(message, reply_text)
    elif cmd == "/groups":
        text, markup = build_groups_keyboard()
        await message.reply_text(text, reply_markup=markup)
    elif cmd == "/news":
        reply_text = await asyncio.to_thread(get_myanmar_news_summary)
        await send_long_message(message, reply_text)
    elif cmd == "/tasks":
        await send_long_message(message, get_formatted_tasks())


async def handle_button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return

    if query.from_user.id != ADMIN_USER_ID:
        await query.answer("Unauthorized.", show_alert=True)
        return

    await query.answer()
    data = query.data

    if data == "cmd_list_groups":
        text, markup = build_groups_keyboard()
        await query.message.reply_text(text, reply_markup=markup)
        return

    if data.startswith("grp_sel_"):
        idx = int(data.replace("grp_sel_", ""))
        groups = get_available_groups()
        if 0 <= idx < len(groups):
            text, markup = build_group_date_keyboard(idx, groups[idx])
            await query.message.reply_text(text, reply_markup=markup)
        else:
            await query.message.reply_text("Group not found. Please run /groups again.")
        return

    await context.bot.send_chat_action(chat_id=query.message.chat_id, action="typing")

    if data == "cmd_unread_summary":
        reply_text = get_group_report("unread summary")
    elif data == "cmd_today_summary":
        reply_text = get_group_report("today summary", day_mode="today")
    elif data == "cmd_yesterday_summary":
        reply_text = get_group_report("yesterday summary", day_mode="yesterday")
    elif data == "cmd_news_summary":
        reply_text = await asyncio.to_thread(get_myanmar_news_summary)
    elif data == "cmd_list_tasks":
        reply_text = get_formatted_tasks()
    elif data.startswith("grp_day_"):
        parts = data.split("_")
        idx = int(parts[2])
        day_mode = parts[3]
        groups = get_available_groups()
        if 0 <= idx < len(groups):
            selected_group = groups[idx]
            reply_text = get_group_report(exact_group_name=selected_group, day_mode=day_mode)
        else:
            reply_text = "Group not found. Please run /groups again."
    else:
        reply_text = "Unknown action."

    await send_long_message(query.message, reply_text)


# --- 4. General Telegram Message Handler ---
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message:
        return

    chat_type = message.chat.type
    sender_name = message.from_user.first_name if message.from_user else "Unknown"
    group_name = message.chat.title if chat_type in ["group", "supergroup"] else "Private"

    # CASE A: Group Messages
    if chat_type in ["group", "supergroup"]:
        content_summary = ""

        if message.text:
            content_summary = message.text
        elif message.photo:
            photo_file = await message.photo[-1].get_file()
            photo_bytes = await photo_file.download_as_bytearray()
            img_prompt = "ဒီပုံထဲမှာ ပါဝင်တဲ့ အဓိက အချက်အလက်များ၊ စာသားများ သို့မဟုတ် လုပ်ငန်းဆိုင်ရာ အခြေအနေများကို အတိုချုံး ဖော်ပြပေးပါ။"
            img_summary = call_ai_with_3tier_fallback(img_prompt, image_bytes=photo_bytes, mime_type='image/jpeg')
            content_summary = f"[Photo] {img_summary}"
        elif message.document:
            doc_file = await message.document.get_file()
            doc_bytes = await doc_file.download_as_bytearray()
            doc_prompt = "ဒီဖိုင်ထဲပါတဲ့ အဓိက အကြောင်းအရာနဲ့ အရေးကြီးတဲ့ အချက်တွေကို အနှစ်ချုပ်ပေးပါ။"
            doc_summary = call_ai_with_3tier_fallback(doc_prompt, image_bytes=doc_bytes,
                                                      mime_type=message.document.mime_type or 'application/pdf')
            content_summary = f"[Document: {message.document.file_name}] {doc_summary}"

        if content_summary:
            supabase.table("raw_logs").insert({
                "group_name": group_name,
                "sender_name": sender_name,
                "content_summary": content_summary,
                "is_read": False
            }).execute()
            cleanup_old_raw_logs()

        return

    # CASE B: Private Chat Instructions
    if chat_type == "private":
        if message.from_user.id != ADMIN_USER_ID:
            await message.reply_text("Unauthorized.")
            return

        user_message = message.text
        if not user_message:
            return

        await context.bot.send_chat_action(chat_id=message.chat_id, action="typing")

        try:
            save_chat_message(message.from_user.id, "user", user_message)

            msg_lower = user_message.lower().strip()
            is_question = msg_lower.endswith("?") or any(q in msg_lower for q in ["လား", "လဲ", "ရလား", "နိုင်မလား"])

            if msg_lower in ["menu", "help"]:
                await message.reply_text(
                    "Quick Menu:",
                    reply_markup=build_main_menu_keyboard()
                )
                return
            elif any(w in msg_lower for w in ["ရာသီဥတု", "weather", "အပူချိန်"]):
                reply_text = await asyncio.to_thread(get_realtime_weather, user_message)
            elif not is_question and any(k in msg_lower for k in ["group report", "group summary", "အနှစ်ချုပ်", "အကျဉ်းချုပ်"]) and "သတင်း" not in msg_lower and "news" not in msg_lower:
                reply_text = get_group_report(user_message)
            elif not is_question and (msg_lower.startswith("task:") or msg_lower.startswith("လုပ်စရာ:") or "task မှတ်ထား" in msg_lower or "task ထည့်" in msg_lower):
                clean_task = re.sub(r"^(task:|လုပ်စရာ:)\s*", "", user_message, flags=re.IGNORECASE).strip()
                add_task(clean_task)
                reply_text = f"Task added: {clean_task}"
            elif msg_lower in ["tasks", "task list", "task စာရင်း", "လုပ်စရာစာရင်း"]:
                reply_text = get_formatted_tasks()
            else:
                reply_text = await asyncio.to_thread(answer_with_universal_search, message.from_user.id, user_message)

            if not reply_text or not str(reply_text).strip():
                reply_text = "လောလောဆယ် အချက်အလက် ပြန်ထုတ်ရာတွင် အခက်အခဲ ရှိနေပါသည်။ နောက်တစ်ကြိမ် ထပ်မံမေးကြည့်ပေးပါရှင်။"

            save_chat_message(message.from_user.id, "assistant", reply_text)

            await send_long_message(message, reply_text)

        except Exception as e:
            await send_long_message(message, f"Error: {str(e)}")


async def main_async():
    threading.Thread(target=run_health_server, daemon=True).start()

    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    # Slash Commands
    application.add_handler(
        CommandHandler(["start", "menu", "summary", "today", "yesterday", "groups", "news", "tasks", "addtask"], handle_command)
    )
    # Inline Button Clicks
    application.add_handler(CallbackQueryHandler(handle_button_callback))
    # Normal Messages
    application.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_message))

    # Daily 9:00 AM MMT News Scheduler
    if application.job_queue:
        morning_9_mmt = dt_time(hour=9, minute=0, second=0, tzinfo=MM_TZ)
        application.job_queue.run_daily(
            scheduled_morning_news_job,
            time=morning_9_mmt,
            name="daily_9am_myanmar_news"
        )

    await application.initialize()

    # English Bot Commands Menu
    await application.bot.set_my_commands([
        BotCommand("menu", "Quick menu buttons"),
        BotCommand("summary", "Unread group summary"),
        BotCommand("today", "Today group summary"),
        BotCommand("yesterday", "Yesterday group summary"),
        BotCommand("groups", "Select group report"),
        BotCommand("news", "Latest Myanmar news summary"),
        BotCommand("tasks", "View current tasks"),
        BotCommand("addtask", "Add a new task"),
    ])

    print("🤖 Fast & Universal AI Agent (Maya) started...")

    await application.start()
    await application.updater.start_polling()

    stop_signal = asyncio.Event()
    await stop_signal.wait()


def main():
    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()