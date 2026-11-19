import os
import json
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

STRICT_MYANMAR_SYSTEM = (
    "သင်သည် 'မာယာ (Maya)' အမည်ရှိ ကိုယ်ပိုင် AI Assistant ဖြစ်သည်။ "
    "အမြဲတမ်း မြန်မာဘာသာ (Unicode) ဖြင့်သာ သဘာဝကျကျ၊ ရှင်းလင်းပြတ်သားစွာ ဖြေဆိုပါ။ "
    "စာလုံးပေါင်းနှင့် စာလုံးအကူးအပြောင်းများ (ဥပမာ - နိုင်ငံခြားစာလုံး/သင်္ကေတများ မလိုအပ်ဘဲ ရောနှောခြင်း) ကို လုံးဝ ရှောင်ကြဉ်ပါ။ "
    "အသုံးပြုသူ၏ Profile၊ အတိတ်မှတ်တမ်းနှင့် အလေ့အထများကို နားလည်ပြီး အလိုက်သိသိနှင့် တိုတိုရှင်းရှင်း ဖြေပါ။ "
    "မေးခွန်းနှင့် မသက်ဆိုင်သော Search အချက်အလက်များကို လုံးဝ မသုံးပါနှင့်။ မသေချာပါက မသိကြောင်း ရိုးသားစွာ ဖြေပါ။"
)


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


# --- Safe Long Message Sender ---
async def send_long_message(message, text: str, chunk_size: int = 3500):
    if not text or not str(text).strip():
        await message.reply_text("ဟုတ်ကဲ့ရှင်၊ လောလောဆယ် အဖြေပြန်ထုတ်ရာတွင် အခက်အခဲ ရှိနေပါသည်။ နောက်မှ ထပ်မေးကြည့်ပါနော်။")
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


# --- Timestamp & Chat Memory Helpers ---
def _format_mm_timestamp(ts_str: str) -> str:
    if not ts_str:
        return ""
    try:
        dt = datetime.fromisoformat(str(ts_str).replace("Z", "+00:00"))
        return dt.astimezone(MM_TZ).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return str(ts_str)[:16]


def save_chat_message(user_id: int, role: str, content: str):
    try:
        supabase.table("chat_messages").insert({
            "user_id": user_id,
            "role": role,
            "content": content
        }).execute()
    except Exception as e:
        print(f"Error saving chat message: {e}")


def get_user_profile(user_id: int) -> str:
    """အသုံးပြုသူအကြောင်း တဖြည်းဖြည်း လေ့လာမှတ်သားထားသော Profile ကို ပြန်ယူသည်။"""
    try:
        res = (
            supabase.table("user_profile")
            .select("profile_summary")
            .eq("user_id", user_id)
            .limit(1)
            .execute()
        )
        rows = res.data or []
        if rows and rows[0].get("profile_summary"):
            return rows[0]["profile_summary"].strip()
    except Exception as e:
        print(f"Error reading user_profile: {e}")
    return ""


def evolve_user_profile(user_id: int, user_msg: str, assistant_reply: str):
    """ပြောခဲ့/မေးခဲ့သမျှထဲမှ အသုံးပြုသူ၏ အကြောင်းအရာ၊ ကြိုက်နှစ်သက်မှုနှင့် အလုပ်/လူနေမှုပုံစံကို တဖြည်းဖြည်း Update လုပ်သည်။"""
    try:
        current_profile = get_user_profile(user_id)
        update_prompt = f"""
You are building a concise, evolving memory profile of the user from their conversations so the AI assistant understands them naturally with minimal explanation.

Existing User Profile:
{current_profile or "None yet."}

Latest Exchange:
User: {user_msg}
Assistant: {assistant_reply[:400]}

Update the User Profile in Burmese (bullet points, max ~180 words):
- Keep important facts, preferences, work context, communication style, and recurring topics learned about the user.
- Remove outdated or contradictory details.
- Do not include raw temporary news, weather, or random search trivia.
Return ONLY the updated profile text.
"""
        completion = groq_client.with_options(timeout=5.0).chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[
                {"role": "system", "content": "Output concise factual user profile notes in Myanmar Unicode."},
                {"role": "user", "content": update_prompt}
            ],
            temperature=0.1,
            max_tokens=260,
        )
        updated = (completion.choices[0].message.content or "").strip()
        if updated:
            supabase.table("user_profile").upsert({
                "user_id": user_id,
                "profile_summary": updated,
                "updated_at": datetime.now(MM_TZ).isoformat()
            }).execute()
    except Exception as e:
        print(f"Error evolving user_profile: {e}")


def extract_search_terms(query_text: str) -> list:
    terms = []
    for raw in query_text.split():
        cleaned = re.sub(r"[^\w\u1000-\u109F]", "", raw)
        if len(cleaned) >= 2:
            terms.append(cleaned)

    compact = re.sub(r"\s+", "", query_text)
    if len(compact) >= 4:
        for i in range(0, min(len(compact), 36), 4):
            chunk = compact[i:i + 5]
            if len(chunk) >= 3 and chunk not in terms:
                terms.append(chunk)

    return terms[:10]


def get_recent_history_for_router(user_id: int, limit: int = 5) -> str:
    try:
        res = (
            supabase.table("chat_messages")
            .select("role, content")
            .eq("user_id", user_id)
            .order("id", desc=True)
            .limit(limit)
            .execute()
        )
        rows = list(reversed(res.data or []))
        return "\n".join([f"{r['role']}: {r['content'][:160]}" for r in rows])
    except Exception:
        return ""


def recall_relevant_history(user_id: int, query_text: str, recent_limit: int = 8, search_limit: int = 10) -> str:
    """User Profile + Past Memory Matches + Recent Conversation ကို စုစည်းထုတ်ပေးသည်။"""
    sections = []

    profile_text = get_user_profile(user_id)
    if profile_text:
        sections.append(f"--- Learned User Profile (How This User Works & Prefers) ---\n{profile_text}")

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
        extra_words = [w for w in re.findall(r"[\w\u1000-\u109F]{2,}", query_text) if w not in search_terms]
        search_terms = (search_terms + extra_words)[:10]

        matched_rows = []
        seen = set((r.get("role"), r.get("content"), r.get("created_at")) for r in recent_rows)

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
                key = (r.get("role"), r.get("content"), r.get("created_at"))
                if key not in seen:
                    seen.add(key)
                    matched_rows.append(r)

        matched_rows.sort(key=lambda x: x.get("created_at") or "")

        if matched_rows:
            matched_lines = [
                f"[{_format_mm_timestamp(m.get('created_at'))} | {m.get('role')}]: {m.get('content')}"
                for m in matched_rows[-15:]
            ]
            sections.append("--- Past Memory Matches ---\n" + "\n".join(matched_lines))

        if recent_rows:
            recent_lines = [
                f"[{_format_mm_timestamp(r.get('created_at'))} | {r.get('role')}]: {r.get('content')}"
                for r in recent_rows
            ]
            sections.append("--- Recent Conversation ---\n" + "\n".join(recent_lines))

    except Exception as e:
        print(f"Error recalling chat history: {e}")

    return "\n\n".join(sections)


# --- 3-Tier AI Fallback Helper (Gemini 3.5 Flash-Lite -> Groq -> OpenRouter) ---
def call_ai_with_3tier_fallback(prompt_text, image_bytes=None, mime_type=None, use_google_search=False):
    # 1st Attempt: Gemini 3.5 Flash-Lite
    try:
        if image_bytes:
            contents = [
                types.Part.from_bytes(data=bytes(image_bytes), mime_type=mime_type or 'image/jpeg'),
                prompt_text
            ]
        else:
            contents = prompt_text

        tools = [types.Tool(google_search=types.GoogleSearch())] if use_google_search else None
        config = types.GenerateContentConfig(
            system_instruction=STRICT_MYANMAR_SYSTEM,
            tools=tools,
        )

        response = gemini_client.models.generate_content(
            model="gemini-3.5-flash-lite",
            contents=contents,
            config=config
        )
        if response and response.text and response.text.strip():
            return response.text.strip()
    except Exception as e:
        print(f"1. Gemini Error: {e}")

    # 2nd Attempt: Groq (Llama 3.1 8B Instant)
    try:
        completion = groq_client.with_options(timeout=8.0).chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[
                {"role": "system", "content": STRICT_MYANMAR_SYSTEM},
                {"role": "user", "content": prompt_text}
            ],
            temperature=0.1,
        )
        if completion and completion.choices and completion.choices[0].message.content:
            txt = completion.choices[0].message.content.strip()
            if txt:
                return txt
    except Exception as e:
        print(f"2. Groq Error: {e}")

    # 3rd Attempt: OpenRouter Auto-Free Router
    try:
        headers = {
            "Authorization": f"Bearer {OPENROUTER_API_KEY}",
            "Content-Type": "application/json",
        }
        data = {
            "model": "openrouter/free",
            "messages": [
                {"role": "system", "content": STRICT_MYANMAR_SYSTEM},
                {"role": "user", "content": prompt_text}
            ]
        }
        response = requests.post("https://openrouter.ai/api/v1/chat/completions", headers=headers, json=data, timeout=8)
        res_json = response.json()
        if "choices" in res_json and len(res_json["choices"]) > 0:
            txt = (res_json["choices"][0]["message"].get("content") or "").strip()
            if txt:
                return txt
    except Exception as e:
        print(f"3. OpenRouter Error: {e}")

    return "ဟုတ်ကဲ့ရှင်၊ လောလောဆယ် AI ဝန်ဆောင်မှုများ နှေးကွေးနေပါသည်။ ခဏနေမှ ထပ်မံမေးမြန်းကြည့်ပါနော်။"


# --- 6-Intent AI Router (Zero-Keyword Smart Routing with Context) ---
def classify_user_intent(user_message: str, available_groups: list, recent_ctx: str = "") -> dict:
    groups_str = ", ".join(available_groups[:15]) if available_groups else "None"
    router_prompt = f"""
Classify the user's latest message using recent context if needed.
Intents:
1. GROUP_QUERY: Asking a specific question/check about internal operations, ways, cancellations, parcels, riders, branches, or events inside Telegram work groups.
2. GROUP_SUMMARY: Requesting a summary/report of unread or daily messages from Telegram groups.
3. TASK: Managing personal to-do tasks (adding a task, listing tasks, or marking a task done).
4. ADVICE: Asking for advice, recommendations, strategic options, or how to handle/solve a problem or decision.
5. GENERAL_TASK: General requests/instructions like drafting messages, translating, summarizing, coding/tech help, explanations, recalling earlier conversation/user profile, or casual chat.
6. GENERAL_SEARCH: Asking for current/live facts, weather, news, prices, sports scores/fixtures, or external web information.

Available Groups: [{groups_str}]
Recent Context:
{recent_ctx or "None"}

Latest Message: "{user_message}"
Return ONLY JSON:
{{"intent":"...","group_name":null,"period":"today|yesterday|recent","task_action":null,"task_title":null,"task_id":null}}
"""
    try:
        completion = groq_client.with_options(timeout=4.0).chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[
                {"role": "system", "content": "Output only valid JSON."},
                {"role": "user", "content": router_prompt}
            ],
            temperature=0.0,
            max_tokens=140,
        )
        raw = (completion.choices[0].message.content or "").strip()
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if match:
            return json.loads(match.group(0))
    except Exception as e:
        print(f"Fast router fallback: {e}")

    return {"intent": "GENERAL_TASK", "group_name": None, "period": "recent"}


# --- Keyword-Free Parallel Web Search Helpers (Tavily + Google News RSS) ---
def _tavily_worker(search_query: str, max_results: int = 4) -> str:
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
            timeout=5,
        )
        if resp.status_code != 200:
            return ""
        data = resp.json()
        lines = []
        if data.get("answer"):
            lines.append(f"[Tavily Answer]: {data['answer']}")
        for r in data.get("results", []):
            title = r.get("title", "")
            content = (r.get("content", "") or "")[:240]
            lines.append(f"- {title}: {content}")
        return "\n".join(lines)
    except Exception as e:
        print(f"Tavily fast search error: {e}")
        return ""


def _rss_worker(queries: list, per_query: int = 3) -> str:
    collected_items = []
    seen_titles = set()
    for q in queries:
        try:
            encoded_q = urllib.parse.quote(q)
            url = f"https://news.google.com/rss/search?q={encoded_q}&hl=en-US&gl=MM&ceid=MM:en"
            resp = requests.get(url, timeout=5)
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
    return "\n".join(collected_items[:6])


def fetch_combined_web_context(tavily_query: str, rss_queries: list = None, max_results: int = 4) -> str:
    results_map = {"tavily": "", "rss": ""}
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = {executor.submit(_tavily_worker, tavily_query, max_results): "tavily"}
        if rss_queries:
            futures[executor.submit(_rss_worker, rss_queries, 3)] = "rss"

        for fut in as_completed(futures, timeout=6):
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


# --- Myanmar News Fetcher & Summarizer (/news & 9:00 AM Daily Job) ---
def get_myanmar_news_summary() -> str:
    now_str = datetime.now(MM_TZ).strftime("%Y-%m-%d %H:%M MMT")
    today_date = datetime.now(MM_TZ).strftime("%Y-%m-%d")

    web_context = fetch_combined_web_context(
        tavily_query=f"Myanmar latest news {today_date} Yangon",
        rss_queries=["Myanmar news", "မြန်မာ သတင်း", "Yangon news"],
        max_results=4,
    )

    prompt = f"""
    လက်ရှိအချိန်: {now_str} (မြန်မာစံတော်ချိန်)
    Google Search နှင့် အောက်ပါ Web Search / RSS အချက်အလက်များကို ပေါင်းစပ်၍ ယနေ့ မြန်မာပြည်နှင့် ပတ်သက်သည့်
    နောက်ဆုံးရ အရေးကြီး သတင်းအစုံအလင်ကို မြန်မာဘာသာဖြင့် ရှင်းလင်းပြတ်သားသော Bullet point များဖြင့်
    စာလုံးရေ ၁,၅၀၀ အတွင်း တိုတိုရှင်းရှင်း အနှစ်ချုပ် တင်ပြပေးပါ။

    {web_context}
    """
    summary = call_ai_with_3tier_fallback(prompt, use_google_search=True)
    return f"📰 Myanmar News Summary ({now_str})\n\n{summary}"


# --- Keyword-Free Universal Web Search + Profile/Memory Answer ---
def answer_with_universal_search(user_id: int, user_message: str) -> str:
    now_str = datetime.now(MM_TZ).strftime("%Y-%m-%d %H:%M MMT")
    memory_context = recall_relevant_history(user_id, user_message, recent_limit=8, search_limit=10)

    clean_q = re.sub(r"[?？!]", "", user_message).strip()
    web_context = fetch_combined_web_context(clean_q, rss_queries=[clean_q], max_results=4)

    system_prompt = (
        f"သင်သည် 'မာယာ (Maya)' အမည်ရှိ ကိုယ်ပိုင် AI Assistant ဖြစ်သည်။\n"
        f"လက်ရှိအချိန်: {now_str} (မြန်မာစံတော်ချိန်)\n"
        f"စည်းမျဉ်းများ:\n"
        f"1. Learned User Profile၊ Past Memory Matches နှင့် Recent Conversation တို့ကို အခြေခံ၍ အသုံးပြုသူ၏ အကြောင်း၊ အလေ့အထနှင့် အဓိပ္ပာယ်ကို အလိုက်သိသိ နားလည်ပြီး ဖြေကြားပါ။\n"
        f"2. Web Search ရလဒ်များနှင့် Google Search Tool ကို အသုံးပြု၍ သက်ဆိုင်ရာ အချက်အလက်များကို တိကျစွာ မြန်မာဘာသာဖြင့် ဖြေပါ။\n"
        f"3. Web Search ရလဒ်ထဲတွင် မေးခွန်းနှင့် မသက်ဆိုင်သော သို့မဟုတ် ဘာသာစကားရောထွေးနေသော အချက်များ ပါလာပါက လုံးဝ မသုံးပါနှင့်။ သေချာမသိပါက မသိကြောင်း ရိုးသားစွာ ဖြေပါ။\n"
        f"4. မြန်မာစာလုံး သန့်သန့်ရှင်းရှင်းနှင့် တိုတိုရှင်းရှင်း ဖြေပါ။\n"
    )

    full_prompt = (
        f"{system_prompt}\n"
        f"{memory_context or '--- Memory ---\nNone'}\n\n"
        f"{web_context or '--- Web Search Results ---\nNone'}\n\n"
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


def handle_task_intent(user_message: str, route: dict) -> str:
    action = (route.get("task_action") or "").lower()
    task_title = (route.get("task_title") or "").strip()
    task_id = route.get("task_id")

    if action == "list":
        return get_formatted_tasks()

    if action == "done" and task_id:
        try:
            supabase.table("tasks").update({"status": "done"}).eq("id", int(task_id)).execute()
            return f"Task ID {task_id} ကို ပြီးစီးကြောင်း မှတ်သားလိုက်ပါပြီ။"
        except Exception as e:
            return f"Task update error: {e}"

    clean_title = task_title or user_message.strip()
    add_task(clean_title)
    return f"Task added: {clean_title}"


# --- 2. Group Summary, Report & Specific Group Q&A Tools ---
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
    if "ဒီနေ့" in text_lower or "today" in text_lower or "ယနေ့" in text_lower:
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


def query_specific_group_logs(user_message: str, router_group: str = None, router_period: str = "recent") -> str:
    cleanup_old_raw_logs()

    if router_period in ["today", "yesterday"]:
        start_iso, end_iso, date_label = get_date_bounds(router_period)
    else:
        start_iso, end_iso, date_label = parse_date_range_from_text(user_message)

    thirty_days_ago_iso = (datetime.now(MM_TZ) - timedelta(days=RETENTION_DAYS)).isoformat()
    query = supabase.table("raw_logs").select("*").gte("created_at", thirty_days_ago_iso).order("id")

    if start_iso and end_iso:
        query = query.gte("created_at", start_iso).lt("created_at", end_iso)
    else:
        query = query.limit(150)

    logs_res = query.execute()
    all_logs = logs_res.data or []

    if not all_logs:
        scope_d = f" ({date_label})" if date_label else ""
        return f"သက်ဆိုင်ရာ ကာလ{scope_d} အတွက် Group log မှတ်တမ်းများ မရှိသေးပါ။"

    matched_group = router_group
    if not matched_group:
        q_lower = user_message.lower()
        available_groups = list({l.get("group_name") for l in all_logs if l.get("group_name")})
        best_score = 0
        for g_name in available_groups:
            g_lower = g_name.lower()
            score = 100 if g_lower in q_lower else 0
            for word in re.findall(r"[a-z0-9\u1000-\u109F]+", g_lower):
                if len(word) >= 2 and word in q_lower:
                    score += len(word) * 2
            if score > best_score:
                best_score = score
                matched_group = g_name

    if matched_group:
        filtered_logs = [l for l in all_logs if l.get("group_name") == matched_group]
    else:
        filtered_logs = all_logs

    if not filtered_logs:
        scope_g = matched_group or "သက်ဆိုင်ရာ Group"
        scope_d = f" ({date_label})" if date_label else ""
        return f"{scope_g}{scope_d} အတွက် Log မှတ်တမ်း မရှိပါ။"

    raw_text = "\n".join(
        [
            f"[{l.get('group_name', '')} | {str(l.get('created_at', ''))[:16]} | {l.get('sender_name', 'Unknown')}]: {l.get('content_summary', '')}"
            for l in filtered_logs
        ]
    )

    now_str = datetime.now(MM_TZ).strftime("%Y-%m-%d %H:%M MMT")
    header_info = f"Group: {matched_group or 'All Groups'} | Period: {date_label or 'Recent'}"

    prompt = f"""
    လက်ရှိအချိန်: {now_str}
    {header_info}

    အောက်ပါတို့မှာ အဆိုပါ Group ထဲမှ စုစည်းရရှိထားသော Log မှတ်တမ်းများ (Overall) ဖြစ်သည်။
    အသုံးပြုသူ၏ မေးခွန်းနှင့် တိုက်ရိုက် သက်ဆိုင်သော အချက်အလက်ကိုသာ ရှာဖွေပြီး လူတစ်ယောက်လို သဘာဝကျကျ၊
    တိတိကျကျနှင့် တိုတိုရှင်းရှင်း မြန်မာဘာသာဖြင့် ဖြေကြားပေးပါ။
    - မေးထားသည့် အချက် ပါဝင်ပါက အရေအတွက်၊ အချိန်၊ အကြောင်းရင်း/အသေးစိတ်ကို တိတိကျကျ ဖော်ပြပါ။
    - မေးထားသည့် အချက် Log များထဲတွင် မပါဝင်ပါက မတွေ့ရှိကြောင်း ရှင်းလင်းစွာ ဖြေပါ။ မဆိုင်သည့် အကြောင်းအရာများကို ထည့်မရေးပါနှင့်။

    အသုံးပြုသူ မေးခွန်း: {user_message}

    Group Log မှတ်တမ်းများ:
    {raw_text}
    """

    answer = call_ai_with_3tier_fallback(prompt, use_google_search=False)
    return f"📌 {header_info}\n\n{answer}"


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


# --- 3. Quick Menu & Command Handlers ---
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


# --- 4. General Telegram Message Handler (6-Intent AI Router + Self-Improving Memory) ---
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

    # CASE B: Private Chat
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

            stripped_lower = user_message.strip().lower()
            if stripped_lower in ["menu", "help"]:
                await message.reply_text("Quick Menu:", reply_markup=build_main_menu_keyboard())
                return

            if stripped_lower in ["မာယာ", "maya", "hi", "hello", "hey", "မင်္ဂလာပါ"]:
                memory_ctx = await asyncio.to_thread(recall_relevant_history, message.from_user.id, user_message, 4, 2)
                greet_prompt = (
                    f"အသုံးပြုသူက နှုတ်ဆက်/ခေါ်နေပါသည်။ လက်ရှိ လေ့လာမှတ်သားထားသော Profile/Conversation ရှိလျှင် အလိုက်သိသိ "
                    f"ရင်းနှီးယဉ်ကျေးစွာ နှုတ်ဆက်ပြီး ဘာကူညီပေးနိုင်မလဲ တိုတိုရှင်းရှင်း မြန်မာဘာသာဖြင့် ဖြေပါ။\n\n{memory_ctx}"
                )
                reply_text = await asyncio.to_thread(call_ai_with_3tier_fallback, greet_prompt, None, None, False)
                save_chat_message(message.from_user.id, "assistant", reply_text)
                await send_long_message(message, reply_text)
                return

            available_groups = await asyncio.to_thread(get_available_groups)
            recent_ctx = await asyncio.to_thread(get_recent_history_for_router, message.from_user.id, 5)
            route = await asyncio.to_thread(classify_user_intent, user_message, available_groups, recent_ctx)

            intent = route.get("intent", "GENERAL_TASK")
            target_group = route.get("group_name")
            period = route.get("period", "recent")

            if intent == "GROUP_QUERY":
                reply_text = await asyncio.to_thread(
                    query_specific_group_logs, user_message, target_group, period
                )
            elif intent == "GROUP_SUMMARY":
                day_m = period if period in ["today", "yesterday"] else None
                reply_text = await asyncio.to_thread(
                    get_group_report, user_message, target_group, day_m
                )
            elif intent == "TASK":
                reply_text = await asyncio.to_thread(handle_task_intent, user_message, route)
            elif intent == "ADVICE":
                memory_context = await asyncio.to_thread(
                    recall_relevant_history, message.from_user.id, user_message, 8, 10
                )
                advice_prompt = (
                    f"အသုံးပြုသူ၏ Profile၊ အတိတ်မှတ်ဉာဏ်နှင့် မေးခွန်းကို သုံးသပ်ပြီး အလိုက်သိသိ မြန်မာဘာသာဖြင့် အကြံပြုပါ:\n"
                    f"1. အခြေအနေ သုံးသပ်ချက်\n"
                    f"2. အကြံပြု လုပ်ဆောင်သင့်သော နည်းလမ်းများ\n"
                    f"3. ချက်ချင်း စတင်နိုင်မည့် Next Step\n\n"
                    f"{memory_context}\n\n"
                    f"အကြံတောင်းသည့် အကြောင်းအရာ: {user_message}"
                )
                reply_text = await asyncio.to_thread(call_ai_with_3tier_fallback, advice_prompt, None, None, False)
            elif intent == "GENERAL_TASK":
                memory_context = await asyncio.to_thread(
                    recall_relevant_history, message.from_user.id, user_message, 8, 10
                )
                gen_prompt = (
                    f"အသုံးပြုသူ၏ Profile၊ အတိတ်စကားပြောဆိုမှုများနှင့် ခိုင်းစေချက်ကို သေချာမှတ်မိပြီး "
                    f"အလိုက်သိသိနှင့် မြန်မာစာ သန့်သန့်ရှင်းရှင်းဖြင့် တိုတိုရှင်းရှင်း ကူညီဆောင်ရွက်ပေးပါ။\n\n"
                    f"{memory_context}\n\n"
                    f"ခိုင်းစေချက်: {user_message}"
                )
                reply_text = await asyncio.to_thread(call_ai_with_3tier_fallback, gen_prompt, None, None, False)
            else:
                reply_text = await asyncio.to_thread(
                    answer_with_universal_search, message.from_user.id, user_message
                )

            save_chat_message(message.from_user.id, "assistant", reply_text)
            asyncio.create_task(asyncio.to_thread(evolve_user_profile, message.from_user.id, user_message, reply_text))
            await send_long_message(message, reply_text)

        except Exception as e:
            await send_long_message(message, "ဟုတ်ကဲ့ရှင်၊ လောလောဆယ် အဖြေပြန်ထုတ်ရာတွင် အခက်အခဲ ရှိနေပါသည်။ နောက်မှ ထပ်မံမေးကြည့်ပါနော်။")
            print(f"Error in handle_message: {e}")


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

    print("🤖 Self-Improving 6-Intent AI Agent (Maya) started...")

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