import os
import json
import time
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
logging.getLogger("httpx").setLevel(logging.WARNING)

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

MYANMAR_WEEKDAYS = [
    "တနင်္လာနေ့",
    "အင်္ဂါနေ့",
    "ဗုဒ္ဓဟူးနေ့",
    "ကြာသပတေးနေ့",
    "သောကြာနေ့",
    "စနေနေ့",
    "တနင်္ဂနွေနေ့",
]

STRICT_MYANMAR_SYSTEM = (
    "သင်သည် 'မာယာ (Maya)' အမည်ရှိ ကိုယ်ပိုင် AI Assistant ဖြစ်သည်။ "
    "အမြဲတမ်း မြန်မာဘာသာ (Unicode) ဖြင့်သာ လူတစ်ယောက်ကဲ့သို့ သဘာဝကျကျ၊ ရင်းနှီးဖော်ရွေစွာ၊ ရှင်းလင်းပြတ်သားစွာ ဖြေဆိုပါ။ "
    "မလိုအပ်ဘဲ နိုင်ငံခြားစာလုံး/သင်္ကေတများ ရောနှောခြင်းကို လုံးဝ ရှောင်ကြဉ်ပါ။ "
    "အသုံးပြုသူ၏ Profile၊ နှစ်သက်သည့်ဟန် (Style Preferences)၊ အတိတ်မှတ်တမ်း၊ အလေ့အထနှင့် မြန်မာပြက္ခဒိန်အချက်အလက်များကို "
    "သေချာမှတ်မိပြီး အလိုက်သိသိနှင့် ဖြေပါ။ "
    "မေးခွန်းနှင့် မသက်ဆိုင်သော Search အချက်အလက်များကို လုံးဝ မသုံးပါနှင့်။ မသေချာပါက မသိကြောင်း ရိုးသားစွာ ဖြေပါ။"
)


# --- Dummy Health Check Server for Render ---
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Telegram AI Agent (Maya) is running!")

    def log_message(self, format, *args):
        return


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


# --- Pure Python Myanmar Calendar Helper ---
def _julian_day_from_gregorian(year: int, month: int, day: int) -> int:
    a = (14 - month) // 12
    y = year + 4800 - a
    m = month + 12 * a - 3
    return day + (153 * m + 2) // 5 + 365 * y + y // 4 - y // 100 + y // 400 - 32045


def _calculate_myanmar_date(dt_mm: datetime) -> dict:
    """Pure Python Myanmar Calendar calculation (Julian Day based)."""
    jdn = _julian_day_from_gregorian(dt_mm.year, dt_mm.month, dt_mm.day)
    anchor_jdn = _julian_day_from_gregorian(2026, 11, 10)  # 1388 ME Tazaundaing Full Moon (15 waxing)
    delta_days = jdn - anchor_jdn

    month_names = [
        "တန်ခူး",
        "ကဆုန်",
        "နယုန်",
        "ဝါဆို",
        "ဝါခေါင်",
        "တော်သလင်း",
        "သီတင်းကျွတ်",
        "တန်ဆောင်မုန်း",
        "နတ်တော်",
        "ပြာသို",
        "တပို့တွဲ",
        "တပေါင်း",
    ]
    month_days = [30, 29, 30, 29, 30, 29, 30, 29, 30, 29, 30, 30]

    cur_year = 1388
    cur_m_idx = 7
    cur_day = 15 + delta_days

    while cur_day > month_days[cur_m_idx]:
        cur_day -= month_days[cur_m_idx]
        cur_m_idx += 1
        if cur_m_idx >= 12:
            cur_m_idx = 0
            cur_year += 1

    while cur_day < 1:
        cur_m_idx -= 1
        if cur_m_idx < 0:
            cur_m_idx = 11
            cur_year -= 1
        cur_day += month_days[cur_m_idx]

    m_day_int = int(cur_day)
    m_len = month_days[cur_m_idx]

    if m_day_int <= 15:
        moon_phase = "လဆန်း"
        fortnight_day = m_day_int
    else:
        moon_phase = "လဆုတ်"
        fortnight_day = m_day_int - 15

    is_full_moon = (m_day_int == 15)
    is_new_moon = (m_day_int == m_len)
    is_sabbath = (fortnight_day in [8, 15]) or is_full_moon or is_new_moon

    wd = dt_mm.weekday()  # 0=Mon .. 6=Sun
    is_yatyaza = (
        (cur_m_idx in [0, 3, 6, 9] and wd in [1, 5]) or
        (cur_m_idx in [1, 4, 7, 10] and wd in [3, 6]) or
        (cur_m_idx in [2, 5, 8, 11] and wd in [2, 4])
    )
    is_pyathada = (
        (cur_m_idx in [0, 3, 6, 9] and wd == 5) or
        (cur_m_idx in [1, 4, 7, 10] and wd in [1, 2]) or
        (cur_m_idx in [2, 5, 8, 11] and wd in [0, 3])
    )

    return {
        "myanmar_year": cur_year,
        "month_name": month_names[cur_m_idx],
        "moon_phase": moon_phase,
        "fortnight_day": fortnight_day,
        "is_full_moon": is_full_moon,
        "is_new_moon": is_new_moon,
        "is_sabbath": is_sabbath,
        "is_yatyaza": is_yatyaza,
        "is_pyathada": is_pyathada,
    }


def get_myanmar_calendar_context(dt_mm: datetime = None) -> str:
    """လက်ရှိ မြန်မာစံတော်ချိန်အတွက် ပြက္ခဒိန်အချက်အလက်ကို စုစည်းပေးသည်။"""
    now_mm = dt_mm or datetime.now(MM_TZ)
    weekday_mm = MYANMAR_WEEKDAYS[now_mm.weekday()]
    gregorian_str = now_mm.strftime(f"%Y-%m-%d ({weekday_mm}) %H:%M MMT")

    info = _calculate_myanmar_date(now_mm)
    if info["is_full_moon"]:
        lunar_str = f"{info['month_name']} လပြည့်နေ့"
    elif info["is_new_moon"]:
        lunar_str = f"{info['month_name']} လကွယ်နေ့"
    else:
        lunar_str = f"{info['month_name']} {info['moon_phase']} {info['fortnight_day']} ရက်"

    return "\n".join([
        f"Gregorian Date/Time: {gregorian_str}",
        f"Myanmar Date: ကောဇာသက္ကရာဇ် {info['myanmar_year']} ခုနှစ်၊ {lunar_str}",
        f"Sabbath (ဥပုသ်): {'ဟုတ်ပါသည်' if info['is_sabbath'] else 'မဟုတ်ပါ'}",
        f"Yatyaza (ရက်ရာဇာ): {'ဟုတ်ပါသည်' if info['is_yatyaza'] else 'မဟုတ်ပါ'}",
        f"Pyathada (ပြဿဒါး): {'ဟုတ်ပါသည်' if info['is_pyathada'] else 'မဟုတ်ပါ'}",
    ])


# --- Timestamp, Chat Memory & Self-Improving User Profile Helpers ---
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
    try:
        res = (
            supabase.table("user_profile")
            .select("profile_summary, style_preferences")
            .eq("user_id", user_id)
            .limit(1)
            .execute()
        )
        rows = res.data or []
        if rows:
            summary = (rows[0].get("profile_summary") or "").strip()
            prefs = (rows[0].get("style_preferences") or "").strip()
            parts = []
            if summary:
                parts.append(f"Profile Notes:\n{summary}")
            if prefs:
                parts.append(f"Style & Routing Preferences:\n{prefs}")
            return "\n\n".join(parts)
    except Exception as e:
        print(f"Error reading user_profile: {e}")
    return ""


def evolve_user_profile(user_id: int, user_msg: str, assistant_reply: str):
    """User profile + style/routing preferences နှစ်မျိုးလုံးကို Self-improve လုပ်ပြီး Supabase ထဲ သိမ်းသည်။"""
    try:
        current_profile = get_user_profile(user_id)
        update_prompt = f"""
You are building a concise, evolving memory profile of the user from their conversations so the AI assistant understands them naturally with minimal explanation.

Existing User Profile & Preferences:
{current_profile or "None yet."}

Latest Exchange:
User: {user_msg}
Assistant: {assistant_reply[:400]}

Return ONLY valid JSON with two concise fields in Burmese (max ~150 words each):
1. "profile_summary": Key facts, work context, recurring topics, and recent routing corrections (e.g., when user clarifies a query should be web search vs group log). Keep accurate details, drop outdated/contradictory info, and exclude temporary news/weather trivia.
2. "style_preferences": How the user likes responses formatted/toned (e.g., quick & friendly, concise vs detailed, bullet points) and any explicit feedback.

JSON format:
{{"profile_summary":"...","style_preferences":"..."}}
"""
        completion = groq_client.with_options(timeout=6.0).chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[
                {"role": "system", "content": "Output only valid JSON in Myanmar Unicode."},
                {"role": "user", "content": update_prompt}
            ],
            temperature=0.1,
            max_tokens=360,
        )
        raw = (completion.choices[0].message.content or "").strip()
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if not match:
            print(f"evolve_user_profile: no JSON found in response: {raw[:120]}")
            return

        parsed = json.loads(match.group(0))
        summary = str(parsed.get("profile_summary") or "").strip()
        prefs = str(parsed.get("style_preferences") or "").strip()

        if not summary and not prefs:
            return

        payload = {
            "user_id": user_id,
            "profile_summary": summary,
            "style_preferences": prefs,
            "updated_at": datetime.now(MM_TZ).isoformat(),
        }
        supabase.table("user_profile").upsert(payload, on_conflict="user_id").execute()
        print(f"[User Profile Updated] user_id={user_id}")
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


def get_recent_history_for_router(user_id: int, limit: int = 6) -> str:
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
        return "\n".join([f"{r['role']}: {r['content'][:220]}" for r in rows])
    except Exception:
        return ""


def recall_relevant_history(user_id: int, query_text: str, recent_limit: int = 8, search_limit: int = 10) -> str:
    sections = []

    profile_text = get_user_profile(user_id)
    if profile_text:
        sections.append(f"--- Learned User Profile & Preferences ---\n{profile_text}")

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


# --- Speed-First & Burmese-Safe 3-Tier AI Fallback Helper ---
def call_ai_with_3tier_fallback(prompt_text, image_bytes=None, mime_type=None, use_google_search=False):
    contents = (
        [types.Part.from_bytes(data=bytes(image_bytes), mime_type=mime_type or 'image/jpeg'), prompt_text]
        if image_bytes else prompt_text
    )
    tools = [types.Tool(google_search=types.GoogleSearch())] if use_google_search else None
    config = types.GenerateContentConfig(
        system_instruction=STRICT_MYANMAR_SYSTEM,
        tools=tools,
    )

    # Tier 1: Gemini 3.5 Flash-Lite (Fast + 1 retry)
    for attempt in range(2):
        try:
            response = gemini_client.models.generate_content(
                model="gemini-3.5-flash-lite",
                contents=contents,
                config=config
            )
            if response and response.text and response.text.strip():
                return response.text.strip()
        except Exception as e:
            print(f"1. Gemini 3.5 Flash-Lite (attempt {attempt + 1}) Error: {e}")
            if attempt == 0:
                time.sleep(1.2)

    # Tier 2: Gemini 3.8 Flash
    try:
        response = gemini_client.models.generate_content(
            model="gemini-3.8-flash",
            contents=contents,
            config=config
        )
        if response and response.text and response.text.strip():
            return response.text.strip()
    except Exception as e:
        print(f"2. Gemini 3.8 Flash Error: {e}")

    # Tier 3A: OpenRouter Specific Burmese-Capable Model (google/gemma-4-31b-it:free)
    if OPENROUTER_API_KEY:
        try:
            headers = {
                "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                "Content-Type": "application/json",
            }
            data = {
                "model": "google/gemma-4-31b-it:free",
                "messages": [
                    {"role": "system", "content": STRICT_MYANMAR_SYSTEM},
                    {"role": "user", "content": prompt_text}
                ],
                "temperature": 0.2,
            }
            response = requests.post("https://openrouter.ai/api/v1/chat/completions", headers=headers, json=data, timeout=9)
            res_json = response.json()
            if "choices" in res_json and len(res_json["choices"]) > 0:
                txt = (res_json["choices"][0]["message"].get("content") or "").strip()
                if txt:
                    return txt
        except Exception as e:
            print(f"3A. OpenRouter (google/gemma-4-31b-it:free) Error: {e}")

    # Tier 3B: Groq 70B Safety Backup
    try:
        completion = groq_client.with_options(timeout=8.0).chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {"role": "system", "content": STRICT_MYANMAR_SYSTEM},
                {"role": "user", "content": prompt_text}
            ],
            temperature=0.2,
        )
        if completion and completion.choices and completion.choices[0].message.content:
            txt = completion.choices[0].message.content.strip()
            if txt:
                return txt
    except Exception as e:
        print(f"3B. Groq 70B Error: {e}")

    return "ဟုတ်ကဲ့ရှင်၊ လောလောဆယ် AI ဆာဗာ ခဏအလုပ်များနေပါသည်။ စက္ကန့်အနည်းငယ်နေပြီး ပြန်ပို့ပေးပါနော်။"


# --- Dedicated 3-Bucket Router (WEB_SEARCH / TASK_GROUP / ADVICE_GENERAL + Learned Preferences) ---
def classify_dedicated_bucket(
    user_message: str,
    available_groups: list,
    recent_ctx: str = "",
    user_pref_ctx: str = "",
) -> dict:
    """Task+Group / Advice+General / Web Search သုံးမျိုးကို သီးသန့် ခွဲခြားပေးသည်။"""
    groups_str = ", ".join(available_groups[:20]) if available_groups else "None"
    prompt = f"""
Route the latest user message into ONE of 3 dedicated buckets:
1. `WEB_SEARCH`: Songs/music, entertainment, online trends, news, weather, prices, sports, live info, or outside web questions — EVEN IF Recent Context was about a Telegram group. Set `group_name` to `null`.
2. `TASK_GROUP`: Personal to-do tasks/reminders (add/list/done task) OR asking/summarizing/following up on messages, people, or operations inside Telegram work groups.
3. `ADVICE_GENERAL`: Advice, problem-solving, drafting messages, translating, coding/tech help, explanations, recalling private chat/profile, or casual conversation.

Learned User Preferences & Corrections:
{user_pref_ctx[:400] or "None"}

Available Groups: [{groups_str}]
Recent Context:
{recent_ctx or "None"}

Latest Message: "{user_message}"
Return ONLY JSON:
{{"bucket":"WEB_SEARCH|TASK_GROUP|ADVICE_GENERAL","sub_type":"group_query|group_summary|task|advice|general","group_name":null,"period":"today|yesterday|recent","task_action":null,"task_title":null,"task_id":null}}
"""
    try:
        completion = groq_client.with_options(timeout=4.0).chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[
                {"role": "system", "content": "Output only valid JSON."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.0,
            max_tokens=150,
        )
        raw = (completion.choices[0].message.content or "").strip()
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if match:
            parsed = json.loads(match.group(0))
            if parsed.get("bucket") in {"WEB_SEARCH", "TASK_GROUP", "ADVICE_GENERAL"}:
                if parsed.get("bucket") == "WEB_SEARCH":
                    parsed["group_name"] = None
                return parsed
    except Exception as e:
        print(f"Dedicated router error: {e}")

    return {"bucket": "ADVICE_GENERAL", "sub_type": "general", "group_name": None, "period": "recent"}


# --- Supabase Pending Follow-ups Queue ("ရရင် ပြန်ပြောပေးမယ်") ---
def queue_pending_followup(user_id: int, followup_type: str, question: str, target_group: str = None, delay_seconds: int = 120):
    try:
        now_mm = datetime.now(MM_TZ)
        retry_dt = now_mm + timedelta(seconds=delay_seconds)
        supabase.table("pending_followups").insert({
            "user_id": user_id,
            "followup_type": followup_type,  # "group_query" | "web_search"
            "question": question,
            "target_group": target_group,
            "status": "pending",
            "attempts": 0,
            "retry_at": retry_dt.isoformat(),
            "created_at": now_mm.isoformat(),
        }).execute()
    except Exception as e:
        print(f"Error queuing pending_followup: {e}")


async def check_pending_followups_job(context: ContextTypes.DEFAULT_TYPE):
    """Supabase pending_followups ထဲမှ အချက်အလက်ရလာပြီဖြစ်သော Group / Web Search များကို အလိုအလျောက် ပြန်ပို့ပေးသည်။"""
    try:
        res = (
            supabase.table("pending_followups")
            .select("*")
            .eq("status", "pending")
            .order("id")
            .limit(30)
            .execute()
        )
        rows = res.data or []
        if not rows:
            return

        now_mm = datetime.now(MM_TZ)
        groups = await asyncio.to_thread(get_available_groups)

        for item in rows:
            item_id = item.get("id")
            user_id = item.get("user_id") or ADMIN_USER_ID
            f_type = item.get("followup_type")
            question = item.get("question", "")
            target_group = item.get("target_group")
            attempts = int(item.get("attempts") or 0)

            try:
                created_dt = datetime.fromisoformat(str(item.get("created_at")).replace("Z", "+00:00")).astimezone(MM_TZ)
                if (now_mm - created_dt).total_seconds() > 86400:
                    supabase.table("pending_followups").update({"status": "expired"}).eq("id", item_id).execute()
                    continue
            except Exception:
                pass

            if f_type == "group_query":
                has_group_data = (
                    any(target_group.lower() in g.lower() for g in groups)
                    if target_group else len(groups) > 0
                )
                if has_group_data:
                    ans = await asyncio.to_thread(
                        query_specific_group_logs, question, target_group, "recent", ""
                    )
                    if ans and "မရှိသေးပါ" not in ans:
                        msg = f"ဟိုင်းရှင် — ခုဏက စောင့်ခိုင်းထားတဲ့ Group အချက်အလက် ရလာပါပြီနော်:\n\n{ans}"
                        await context.bot.send_message(chat_id=user_id, text=msg[:3500])
                        save_chat_message(user_id, "assistant", msg)
                        supabase.table("pending_followups").update({"status": "completed"}).eq("id", item_id).execute()

            elif f_type == "web_search":
                try:
                    retry_dt = datetime.fromisoformat(str(item.get("retry_at")).replace("Z", "+00:00")).astimezone(MM_TZ)
                    if now_mm < retry_dt:
                        continue
                except Exception:
                    pass

                if attempts >= 6:
                    supabase.table("pending_followups").update({"status": "expired"}).eq("id", item_id).execute()
                    continue

                ans = await asyncio.to_thread(answer_with_universal_search, user_id, question)
                if ans and "ခဏအလုပ်များနေပါသည်" not in ans and "429" not in ans:
                    msg = f"ဟိုင်းရှင် — ခုဏက ဆွဲလို့မရသေးတဲ့ အချက်အလက် ရလာလို့ ပြန်ပြောပေးတာပါနော်:\n\n{ans}"
                    await context.bot.send_message(chat_id=user_id, text=msg[:3500])
                    save_chat_message(user_id, "assistant", msg)
                    supabase.table("pending_followups").update({
                        "status": "completed",
                        "attempts": attempts + 1
                    }).eq("id", item_id).execute()
                else:
                    next_retry = (now_mm + timedelta(seconds=180)).isoformat()
                    supabase.table("pending_followups").update({
                        "attempts": attempts + 1,
                        "retry_at": next_retry
                    }).eq("id", item_id).execute()

    except Exception as e:
        print(f"Error in check_pending_followups_job: {e}")


# --- Focused Web Search Helpers (Tavily + Google News RSS) ---
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
                "include_answer": "advanced",
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
    now_mm = datetime.now(MM_TZ)
    now_str = now_mm.strftime("%Y-%m-%d %H:%M MMT")
    today_date = now_mm.strftime("%Y-%m-%d")
    cal_info = get_myanmar_calendar_context(now_mm)

    web_context = fetch_combined_web_context(
        tavily_query=f"Myanmar latest news {today_date} Yangon",
        rss_queries=["Myanmar news", "မြန်မာ သတင်း", "Yangon news"],
        max_results=4,
    )

    prompt = f"""
    လက်ရှိအချိန်: {now_str} (မြန်မာစံတော်ချိန်)
    ပြက္ခဒိန် အချက်အလက်:
    {cal_info}

    Google Search နှင့် အောက်ပါ Web Search / RSS အချက်အလက်များကို ပေါင်းစပ်၍ ယနေ့ မြန်မာပြည်နှင့် ပတ်သက်သည့်
    နောက်ဆုံးရ အရေးကြီး သတင်းအစုံအလင်ကို မြန်မာဘာသာဖြင့် ရှင်းလင်းပြတ်သားသော Bullet point များဖြင့်
    စာလုံးရေ ၁,၅၀၀ အတွင်း တိုတိုရှင်းရှင်း အနှစ်ချုပ် တင်ပြပေးပါ။

    {web_context}
    """
    summary = call_ai_with_3tier_fallback(prompt, use_google_search=True)
    return f"📰 Myanmar News Summary ({now_str})\n\n{summary}"


# --- Google AI Overview Style Web Search (Focused on User's Search Query) ---
def answer_with_universal_search(user_id: int, user_message: str) -> str:
    now_mm = datetime.now(MM_TZ)
    now_str = now_mm.strftime("%Y-%m-%d %H:%M MMT")
    cal_info = get_myanmar_calendar_context(now_mm)
    profile_text = get_user_profile(user_id)

    clean_q = re.sub(r"[?？!]", "", user_message).strip()
    web_context = fetch_combined_web_context(clean_q, rss_queries=[clean_q], max_results=5)

    profile_block = f"--- Learned User Profile & Preferences ---\n{profile_text}\n\n" if profile_text else ""

    system_prompt = (
        f"သင်သည် 'မာယာ (Maya)' အမည်ရှိ ကိုယ်ပိုင် AI Assistant ဖြစ်သည်။\n"
        f"လက်ရှိအချိန်: {now_str} (မြန်မာစံတော်ချိန်)\n"
        f"မြန်မာပြက္ခဒိန် အချက်အလက်:\n{cal_info}\n\n"
        f"အသုံးပြုသူ၏ ရှာဖွေမေးမြန်းချက်အပေါ်သာ တိုက်ရိုက် Focus ထားပြီး Google AI Overview ကဲ့သို့ "
        f"ရင်းနှီးဖော်ရွေစွာ၊ ရှင်းလင်းပြတ်သားစွာ မြန်မာဘာသာဖြင့် ဖြေကြားပါ (Telegram Group မှတ်တမ်းများကို မရောပါနှင့်):\n"
        f"1. **တိုက်ရိုက် အဖြေချုပ် (Direct Answer):** မေးခွန်း၏ အဓိကအဖြေကို စာကြောင်း ၁–၂ ကြောင်းဖြင့် အရင်ဆုံး ရှင်းရှင်းလင်းလင်း ဖြေပါ။\n"
        f"2. **အဓိက အချက်အလက်များ (Key Points):** ရှာဖွေမှုရလဒ်နှင့် Google Search ထဲမှ သက်ဆိုင်ရာ အချက် (၃–၅ ချက်) ကို Bullet point များဖြင့် တိုတိုရှင်းရှင်း ဖော်ပြပါ။\n"
        f"3. မေးခွန်းနှင့် မသက်ဆိုင်သော အချက်အလက်များကို လုံးဝ မထည့်ပါနှင့်။ သေချာမသိပါက မသိကြောင်း ရိုးသားစွာ ဖြေပါ။\n"
    )

    full_prompt = (
        f"{system_prompt}\n"
        f"{profile_block}"
        f"{web_context or '--- Web Search Results ---\nNone'}\n\n"
        f"ရှာဖွေမေးမြန်းချက်: {user_message}"
    )

    reply = call_ai_with_3tier_fallback(full_prompt, use_google_search=True)
    if "ခဏအလုပ်များနေပါသည်" in reply:
        queue_pending_followup(user_id, "web_search", user_message, target_group=None, delay_seconds=120)
        return "ဟိုင်းရှင် — ခုဏက ဆာဗာ ခဏအလုပ်များနေလို့ အဖြေမထွက်သေးဘူးရှင်။ ပြန်ရတာနဲ့ ချက်ချင်း ပြန်ပြောပေးပါ့မယ်နော်။"
    return reply


# --- Scheduled Jobs (9:00 AM News, Due Reminders, 6:00 PM Pending Digest, 30s Follow-up Watcher) ---
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


async def check_due_task_reminders_job(context: ContextTypes.DEFAULT_TYPE):
    """အချိန်ကျရောက်လာသော Task Reminder များကို Quick & Friendly ဟန်ဖြင့် သတိပေးသည်။"""
    if not ADMIN_USER_ID:
        return
    try:
        now_mm = datetime.now(MM_TZ)
        res = supabase.table("tasks").select("*").eq("status", "pending").execute()
        tasks = res.data or []

        for t in tasks:
            if t.get("reminded") is True:
                continue
            r_at = t.get("remind_at")
            if not r_at:
                continue
            try:
                r_dt = datetime.fromisoformat(str(r_at).replace("Z", "+00:00")).astimezone(MM_TZ)
                if r_dt <= now_mm:
                    title = t.get("title", "")
                    msg = f"ဟိုင်းရှင် — \"{title}\" လုပ်ဖို့ အချိန်ရောက်ပါပြီနော်။"
                    await context.bot.send_message(chat_id=ADMIN_USER_ID, text=msg)
                    save_chat_message(ADMIN_USER_ID, "assistant", msg)
                    try:
                        supabase.table("tasks").update({"reminded": True}).eq("id", t["id"]).execute()
                    except Exception:
                        pass
            except Exception:
                continue
    except Exception as e:
        print(f"Error in check_due_task_reminders_job: {e}")


async def scheduled_evening_task_digest_job(context: ContextTypes.DEFAULT_TYPE):
    """ညနေ ၆ နာရီတိုင်း ပြီးဖို့ ကျန်နေသေးတဲ့ Pending Task များကို Quick & Friendly သတိပေးသည်။"""
    if not ADMIN_USER_ID:
        return
    try:
        res = supabase.table("tasks").select("*").eq("status", "pending").order("id").execute()
        pending = res.data or []
        if not pending:
            return

        lines = [f"• {t['title']}" for t in pending[:10]]
        msg = "ဟိုင်းရှင် — ဒီနေ့အတွက် ပြီးဖို့ ကျန်နေသေးတဲ့ Task လေးတွေပါနော်:\n" + "\n".join(lines)
        await context.bot.send_message(chat_id=ADMIN_USER_ID, text=msg)
        save_chat_message(ADMIN_USER_ID, "assistant", msg)
    except Exception as e:
        print(f"Error in scheduled_evening_task_digest_job: {e}")


# --- 1. Personal Tasks & Reminder Helpers ---
def parse_reminder_datetime_mmt(text: str) -> datetime | None:
    """စာသားထဲက အချိန်/ရက်စွဲ ကို ဖတ်ပြီး MMT datetime ပြန်ပေးသည်။"""
    now_mm = datetime.now(MM_TZ)
    target_date = now_mm.date()

    if "မနက်ဖြန်" in text or "tomorrow" in text.lower():
        target_date = (now_mm + timedelta(days=1)).date()

    hm_match = re.search(r"\b(\d{1,2}):(\d{2})\b", text)
    hour = None
    minute = 0

    if hm_match:
        hour = int(hm_match.group(1))
        minute = int(hm_match.group(2))
    else:
        mm_digits = str.maketrans("၀၁၂၃၄၅၆၇၈၉", "0123456789")
        norm_text = text.translate(mm_digits)
        hr_match = re.search(r"(\d{1,2})\s*(?:နာရီ|hour|hr)", norm_text, re.IGNORECASE)
        if hr_match:
            hour = int(hr_match.group(1))
            if ("ညနေ" in text or "ည" in text or "pm" in text.lower()) and hour < 12:
                hour += 12
            elif ("မနက်" in text or "am" in text.lower()) and hour == 12:
                hour = 0

    if hour is None or not (0 <= hour <= 23) or not (0 <= minute <= 59):
        return None

    candidate = datetime(target_date.year, target_date.month, target_date.day, hour, minute, 0, tzinfo=MM_TZ)
    if candidate <= now_mm and "မနက်ဖြန်" not in text and "tomorrow" not in text.lower():
        candidate += timedelta(days=1)
    return candidate


def add_task(title: str, remind_dt: datetime | None = None) -> dict:
    payload = {"title": title, "status": "pending"}
    if remind_dt:
        payload["remind_at"] = remind_dt.isoformat()
        payload["reminded"] = False
    try:
        supabase.table("tasks").insert(payload).execute()
    except Exception:
        supabase.table("tasks").insert({"title": title, "status": "pending"}).execute()
    return {"status": "success", "message": f"Task added: {title}"}


def list_tasks() -> list:
    res = supabase.table("tasks").select("*").order("id").execute()
    return res.data


def get_formatted_tasks() -> str:
    tasks = list_tasks()
    if not tasks:
        return "လက်ရှိ Task မရှိသေးပါဘူးရှင်။"
    lines = []
    for t in tasks:
        mark = "✓" if t.get("status") == "done" else "•"
        r_str = ""
        if t.get("remind_at") and t.get("status") != "done":
            try:
                r_dt = datetime.fromisoformat(str(t["remind_at"]).replace("Z", "+00:00")).astimezone(MM_TZ)
                r_str = f" (⏰ {r_dt.strftime('%m-%d %H:%M')})"
            except Exception:
                pass
        lines.append(f"{mark} [{t['id']}] {t['title']}{r_str}")
    return "လက်ရှိ Task စာရင်းလေးပါရှင်:\n" + "\n".join(lines)


def handle_task_intent(user_message: str, route: dict) -> str:
    action = (route.get("task_action") or "").lower().strip()
    task_title = (route.get("task_title") or "").strip()
    task_id = route.get("task_id")

    if action == "list":
        return get_formatted_tasks()

    if action == "done":
        try:
            if task_id:
                supabase.table("tasks").update({"status": "done"}).eq("id", int(task_id)).execute()
                return f"ပြီးသွားပြီနော် — Task #{task_id} ကို Done မှတ်လိုက်ပါပြီရှင်။"

            search_key = (task_title or user_message).strip()
            pending_res = (
                supabase.table("tasks")
                .select("*")
                .eq("status", "pending")
                .order("id", desc=True)
                .limit(20)
                .execute()
            )
            pending_tasks = pending_res.data or []
            matched = next(
                (t for t in pending_tasks if search_key.lower() in str(t.get("title", "")).lower()),
                None
            )
            if matched:
                supabase.table("tasks").update({"status": "done"}).eq("id", matched["id"]).execute()
                return f"ပြီးသွားပြီနော် — \"{matched['title']}\" ကို Done မှတ်လိုက်ပါပြီရှင်။"

            return "ပြီးစီးကြောင်း မှတ်ရမယ့် Pending Task မတွေ့သေးဘူးရှင်။ `/tasks` နဲ့ ကြည့်ပြီး ထပ်ပြောပေးပါနော်။"
        except Exception as e:
            print(f"Task update error: {e}")
            return "Task အခြေအနေ ပြောင်းရာမှာ ခဏအဆင်မပြေ ဖြစ်သွားပါတယ်ရှင်။"

    clean_title = task_title or user_message.strip()
    remind_dt = parse_reminder_datetime_mmt(user_message)
    add_task(clean_title, remind_dt)

    if remind_dt:
        return f"မှတ်ထားပါပြီရှင် — \"{clean_title}\" (⏰ {remind_dt.strftime('%Y-%m-%d %H:%M')} မှာ သတိပေးပါ့မယ်နော်)"
    return f"မှတ်ထားပါပြီရှင် — \"{clean_title}\""


# --- 2. Group Summary, Report & Keyword-Free Natural Group Q&A Tools ---
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


def query_specific_group_logs(
    user_message: str,
    router_group: str = None,
    router_period: str = "recent",
    recent_ctx: str = "",
) -> str:
    """Keyword filter မသုံးဘဲ Group logs များကို တိုက်ရိုက်ဖတ်ပြီး မေးသည့်အချက်ကိုသာ လူတစ်ယောက်လို သဘာဝကျကျ ဖြေပေးသည်။"""
    cleanup_old_raw_logs()

    thirty_days_ago_iso = (datetime.now(MM_TZ) - timedelta(days=RETENTION_DAYS)).isoformat()
    query = supabase.table("raw_logs").select("*").gte("created_at", thirty_days_ago_iso).order("id", desc=True)

    date_label = None
    if router_period in ["today", "yesterday"]:
        start_iso, end_iso, date_label = get_date_bounds(router_period)
        query = query.gte("created_at", start_iso).lt("created_at", end_iso)
    else:
        s_iso, e_iso, d_lbl = parse_date_range_from_text(f"{recent_ctx}\n{user_message}")
        if s_iso and e_iso:
            start_iso, end_iso, date_label = s_iso, e_iso, d_lbl
            query = query.gte("created_at", start_iso).lt("created_at", end_iso)

    logs_res = query.limit(300).execute()
    all_logs = list(reversed(logs_res.data or []))

    if not all_logs:
        scope_d = f" ({date_label})" if date_label else ""
        return f"သက်ဆိုင်ရာ ကာလ{scope_d} အတွင်း Group မှတ်တမ်းများ မရှိသေးပါရှင်။"

    matched_group = router_group
    if not matched_group:
        combined_q = f"{recent_ctx}\n{user_message}".lower()
        available_groups = list({l.get("group_name") for l in all_logs if l.get("group_name")})
        best_score = 0
        for g_name in available_groups:
            g_lower = g_name.lower()
            score = 100 if g_lower in combined_q else 0
            for word in re.findall(r"[a-z0-9\u1000-\u109F]+", g_lower):
                if len(word) >= 2 and word in combined_q:
                    score += len(word) * 2
            if score > best_score:
                best_score = score
                matched_group = g_name

    selected_logs = [l for l in all_logs if l.get("group_name") == matched_group] if matched_group else all_logs

    if not selected_logs:
        return f"{matched_group or 'သက်ဆိုင်ရာ Group'} ထဲမှာ မှတ်တမ်း မရှိသေးပါရှင်။"

    raw_text = "\n".join(
        [
            f"[{_format_mm_timestamp(l.get('created_at'))} | {l.get('group_name', '')} | {l.get('sender_name', 'Unknown')}]: {l.get('content_summary', '')}"
            for l in selected_logs[-200:]
        ]
    )

    now_str = datetime.now(MM_TZ).strftime("%Y-%m-%d %H:%M MMT")

    prompt = f"""
    လက်ရှိအချိန်: {now_str}

    အောက်ပါတို့မှာ Telegram Group ထဲမှ စကားပြောဆိုမှု မှတ်တမ်းများ ဖြစ်သည်။
    အရှေ့နောက် စကားပြောဆိုမှု (Recent Context) နှင့် လက်ရှိ မေးခွန်းကို နားလည်ပြီး အသုံးပြုသူ မေးထားသည့်
    အချက် (သို့မဟုတ် သက်ဆိုင်ရာ လူပြောထားသည့် အကြောင်းအရာ) တစ်ခုတည်းကိုသာ Focus ထား၍ ပြန်ပြောပြပေးပါ။

    ဖြေဆိုရမည့် ပုံစံစည်းမျဉ်းများ:
    1. စက်ရုပ်လို တစ်ကြောင်းချင်းစီ၏ အချိန် (Timestamp) တွေ ထည့်ရေးစရာ မလိုပါ (အသုံးပြုသူက ဘယ်အချိန်လဲဟု မေးမှသာ အချိန်ကို ထည့်ပြောပါ)။
    2. လူတစ်ယောက်က လုပ်ဖော်ကိုင်ဖက်ကို ရင်းနှီးဖော်ရွေစွာ ပြန်ရှင်းပြနေသလို သဘာဝကျကျ၊ ချောချောမွေ့မွေ့နှင့် လိုရင်းတိုရှင်း မြန်မာဘာသာဖြင့် ပြောပြပါ။
    3. အရှေ့မှာ ပြောခဲ့သည့် လူ/အကြောင်းအရာကို ဆက်မေးခြင်း (ဥပမာ - "သူပြောတဲ့ key points အကျဉ်းချုပ်ပေးပါ") ဖြစ်ပါက ထိုသူ၏ ပြောစကားများထဲမှ အဓိက Key Points များကိုသာ Bullet point များဖြင့် ရှင်းရှင်းလင်းလင်း ထုတ်ပေးပါ။
    4. မေးထားသည့် အချက်/လူနှင့် မသက်ဆိုင်သော အခြားလူများ၏ စကားများကို လုံးဝ ထည့်မပြောပါနှင့်။ မှတ်တမ်းထဲတွင် မပါဝင်ပါက မတွေ့ရှိကြောင်း ရိုးသားစွာ ဖြေပါ။

    Recent Context:
    {recent_ctx or "None"}

    လက်ရှိ မေးခွန်း: {user_message}

    Group မှတ်တမ်းများ:
    {raw_text}
    """

    return call_ai_with_3tier_fallback(prompt, use_google_search=False)


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


# --- Dedicated Bucket Handlers ---
def handle_task_and_group(user_id: int, user_message: str, route: dict, recent_ctx: str = "") -> str:
    """Task/Reminder နှင့် Telegram Group မှတ်တမ်းများကို သီးသန့် ကိုင်တွယ်သည်။"""
    sub_type = (route.get("sub_type") or "group_query").lower()
    target_group = route.get("group_name")
    period = route.get("period", "recent")

    if sub_type == "task":
        return handle_task_intent(user_message, route)

    if sub_type == "group_summary":
        day_m = period if period in ["today", "yesterday"] else None
        return get_group_report(user_message, target_group, day_m)

    ans = query_specific_group_logs(user_message, target_group, period, recent_ctx)
    if ans and "မရှိသေးပါ" in ans:
        queue_pending_followup(user_id, "group_query", user_message, target_group=target_group, delay_seconds=30)
        g_label = f"\"{target_group}\"" if target_group else "သက်ဆိုင်ရာ Group"
        return f"ဟိုင်းရှင် — {g_label} ရဲ့ မက်ဆေ့ခ်ျအချက်အလက်တွေ အခုထိ မရောက်သေးဘူးရှင်။ ရောက်လာတာနဲ့ ချက်ချင်း ပြန်ပြောပေးပါ့မယ်နော်။"
    return ans


def handle_advice_and_general(user_id: int, user_message: str, route: dict) -> str:
    """Advice နှင့် General Task/Chat ကို သီးသန့် ကိုင်တွယ်သည်။"""
    sub_type = (route.get("sub_type") or "general").lower()
    cal_info = get_myanmar_calendar_context()
    memory_context = recall_relevant_history(user_id, user_message, recent_limit=8, search_limit=10)

    if sub_type == "advice":
        prompt = (
            f"မြန်မာပြက္ခဒိန် အချက်အလက်:\n{cal_info}\n\n"
            f"အသုံးပြုသူ၏ Profile၊ နှစ်သက်သည့်ဟန် (Style Preferences)၊ အတိတ်မှတ်ဉာဏ်နှင့် မေးခွန်းကို သုံးသပ်ပြီး "
            f"အလိုက်သိသိ မြန်မာဘာသာဖြင့် အကြံပြုပါ:\n"
            f"1. အခြေအနေ သုံးသပ်ချက်\n"
            f"2. အကြံပြု လုပ်ဆောင်သင့်သော နည်းလမ်းများ\n"
            f"3. ချက်ချင်း စတင်နိုင်မည့် Next Step\n\n"
            f"{memory_context}\n\n"
            f"အကြံတောင်းသည့် အကြောင်းအရာ: {user_message}"
        )
    else:
        prompt = (
            f"မြန်မာပြက္ခဒိန် အချက်အလက်:\n{cal_info}\n\n"
            f"အသုံးပြုသူ၏ Profile၊ နှစ်သက်သည့်ဟန် (Style Preferences)၊ အတိတ်စကားပြောဆိုမှုများနှင့် ခိုင်းစေချက်ကို သေချာမှတ်မိပြီး "
            f"အလိုက်သိသိနှင့် မြန်မာစာ သန့်သန့်ရှင်းရှင်းဖြင့် ကူညီဆောင်ရွက်ပေးပါ။\n\n"
            f"{memory_context}\n\n"
            f"ခိုင်းစေချက်: {user_message}"
        )

    return call_ai_with_3tier_fallback(prompt, None, None, False)


def handle_web_search(user_id: int, user_message: str) -> str:
    """Web Search အတွက် သီးသန့် ရှာဖွေဖြေကြားသည်။"""
    return answer_with_universal_search(user_id, user_message)


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
        remind_dt = parse_reminder_datetime_mmt(arg)
        add_task(arg, remind_dt)
        await message.reply_text(f"မှတ်ထားပါပြီရှင် — \"{arg}\"")
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


# --- 4. General Telegram Message Handler (Dedicated Buckets + Self-Improve + Calendar) ---
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
                    f"အသုံးပြုသူက နှုတ်ဆက်/ခေါ်နေပါသည်။ လက်ရှိ လေ့လာမှတ်သားထားသော Profile/Preferences/Conversation ရှိလျှင် အလိုက်သိသိ "
                    f"ရင်းနှီးဖော်ရွေစွာ နှုတ်ဆက်ပြီး ဘာကူညီပေးနိုင်မလဲ တိုတိုရှင်းရှင်း မြန်မာဘာသာဖြင့် ဖြေပါ။\n\n{memory_ctx}"
                )
                reply_text = await asyncio.to_thread(call_ai_with_3tier_fallback, greet_prompt, None, None, False)
                save_chat_message(message.from_user.id, "assistant", reply_text)
                await send_long_message(message, reply_text)
                return

            available_groups = await asyncio.to_thread(get_available_groups)
            recent_ctx = await asyncio.to_thread(get_recent_history_for_router, message.from_user.id, 6)
            user_pref_ctx = await asyncio.to_thread(get_user_profile, message.from_user.id)
            route = await asyncio.to_thread(
                classify_dedicated_bucket, user_message, available_groups, recent_ctx, user_pref_ctx
            )

            bucket = route.get("bucket", "ADVICE_GENERAL")
            print(f"[Dedicated Route] bucket={bucket}, sub_type={route.get('sub_type')}, group={route.get('group_name')}, period={route.get('period')}")

            if bucket == "WEB_SEARCH":
                reply_text = await asyncio.to_thread(handle_web_search, message.from_user.id, user_message)
            elif bucket == "TASK_GROUP":
                reply_text = await asyncio.to_thread(
                    handle_task_and_group, message.from_user.id, user_message, route, recent_ctx
                )
            else:
                reply_text = await asyncio.to_thread(
                    handle_advice_and_general, message.from_user.id, user_message, route
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

    # Scheduled Jobs (9:00 AM News, 1-min Due Reminder Watcher, 6:00 PM Pending Digest, 30s Follow-up Watcher)
    if application.job_queue:
        morning_9_mmt = dt_time(hour=9, minute=0, second=0, tzinfo=MM_TZ)
        application.job_queue.run_daily(
            scheduled_morning_news_job,
            time=morning_9_mmt,
            name="daily_9am_myanmar_news"
        )
        application.job_queue.run_repeating(
            check_due_task_reminders_job,
            interval=60,
            first=15,
            name="due_task_reminders_watcher"
        )
        evening_6_mmt = dt_time(hour=18, minute=0, second=0, tzinfo=MM_TZ)
        application.job_queue.run_daily(
            scheduled_evening_task_digest_job,
            time=evening_6_mmt,
            name="daily_6pm_pending_task_digest"
        )
        application.job_queue.run_repeating(
            check_pending_followups_job,
            interval=30,
            first=20,
            name="pending_followups_watcher"
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

    print("🤖 Dedicated-Bucket Self-Improving AI Agent (Maya) started...")

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