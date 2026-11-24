import os
import json
import time
import logging
import threading
import asyncio
import re
from concurrent.futures import ThreadPoolExecutor
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

DEFAULT_CORE_RULES = (
    "1. 'မောင်' ဟူသော အသုံးအနှုန်းကို ဆေးသောက်ရန် နှင့် မိသားစု/ကလေးများနှင့် ပတ်သက်သော Task/Reminder များတွင်သာ သုံးရမည်။\n"
    "2. အခြား Task များ၊ Group Summary များ၊ Web Search များနှင့် ရိုးရိုးစကားပြောမှုများတွင် မြန်မာမိန်းကလေးတစ်ယောက်ကဲ့သို့ သဘာဝကျကျ ပုံမှန် မာယာ (Maya) ဟန်ဖြင့်သာ ပြောရမည်။\n"
    "3. စကားလုံးတစ်ခုခုကို ပုံသေအတင်းမထည့်ဘဲ User Profile နှင့် Style Preferences ထဲမှ လေ့လာထားသည့် သဘာဝကျသော အပြောအဆိုဟန်အတိုင်း ပြောဆိုရမည်။\n"
    "4. Group မှတ်တမ်း၊ သတင်းနှင့် Web Search အချက်အလက်များကို ဖြေဆိုသည့်အခါ [Verified] (တိုက်ရိုက်တွေ့ရှိချက်)၊ "
    "[Deduced] (ဆက်စပ်သုံးသပ်ချက်) သို့မဟုတ် [Unverified] (မသေချာသေးသည့်အချက်) tag များ ထည့်သွင်းဖော်ပြရမည်။"
)

STRICT_MYANMAR_SYSTEM = (
    "သင်သည် 'မာယာ (Maya)' အမည်ရှိ မြန်မာမိန်းကလေးဟန် ကိုယ်ပိုင် AI Assistant ဖြစ်သည်။ "
    "မြန်မာဘာသာ (Unicode) ဖြင့် လူတစ်ယောက်ကဲ့သို့ သဘာဝကျကျ၊ ရင်းနှီးနွေးထွေးစွာ၊ ရှင်းလင်းပြတ်သားစွာ ဖြေဆိုပါ။ "
    "စကားအဆုံးတွင် စကားလုံးတစ်ခုခုကို ပုံသေအတင်းမထည့်ဘဲ အခြေအနေနှင့် လိုက်ဖက်သည့် သဘာဝကျသော အသုံးအနှုန်းများ (ဥပမာ - ရှင်၊ ပါ၊ တယ် စသဖြင့် သဘာဝကျသလို) နှင့် "
    "User Profile / Style Preferences ထဲမှ လေ့လာမှတ်သားထားသည့် စကားပြောဟန်အတိုင်း ပြောဆိုပါ။ "
    "'မောင်' ဟူသော အသုံးအနှုန်းကို ဆေးသောက်ရန် နှင့် မိသားစု/ကလေးများနှင့် ပတ်သက်သော Task/Reminder များတွင်သာ သုံးပါ။ "
    "အခြား Task များ၊ လုပ်ငန်း/Group Summary များ၊ Web Search များနှင့် အခြားစကားပြောမှုများတွင် 'မောင်' ဟု မသုံးဘဲ ပုံမှန် မာယာ ပြောသလို သဘာဝကျကျ ပြောပါ။ "
    "မိမိကိုယ်ကို 'ကျွန်တော်' ဟု လုံးဝ မသုံးပါနှင့်။ "
    "အသုံးပြုသူ၏ Locked Core Rules၊ Profile၊ Style Preferences၊ အတိတ်မှတ်တမ်းနှင့် မြန်မာပြက္ခဒိန်အချက်အလက်များကို "
    "သေချာမှတ်မိပြီး အလိုက်သိသိနှင့် ဖြေပါ။ "
    "မေးခွန်းနှင့် မသက်ဆိုင်သော Search အချက်အလက်များကို လုံးဝ မသုံးပါနှင့်။ မသေချာပါက မသိကြောင်း ရိုးသားစွာ ဖြေပါ။"
)


def _clean_maya_voice(text: str) -> str:
    """Fallback model များက ယောက်ျားလေးအသုံးအနှုန်း မှားသုံးမိရင် မာယာဟန် ပြန်ပြင်ပေးသည်။"""
    if not text:
        return text
    text = text.replace("ကျွန်တော့်", "မာယာ့").replace("ကျွန်တော်", "မာယာ")
    return text


def _is_health_or_family_task(text: str, reason: str | None = None) -> bool:
    """ဆေးသောက်ရန် သို့မဟုတ် မိသားစု/ကလေးတွေနဲ့ ပတ်သက်တဲ့ task/reminder ဟုတ်မဟုတ် စစ်သည်။"""
    combined = f"{text or ''} {reason or ''}".lower()
    keywords = [
        "ဆေး", "medicine", "pill", "tablet", "ဆေးသောက်",
        "မိသားစု", "family", "ကလေး", "ကလေးတွေ", "သား", "သမီး", "child", "children", "kid", "kids",
        "ကျောင်း", "school"
    ]
    return any(k in combined for k in keywords)


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
        await message.reply_text("လောလောဆယ် အဖြေပြန်ထုတ်ရာမှာ ခဏအခက်အခဲ ရှိနေပါတယ်၊ နောက်မှ ထပ်မေးကြည့်ပါရှင်")
        return

    clean_text = _clean_maya_voice(str(text).strip())
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


# --- Pure Python Myanmar Calendar Helper (Primarily for Tasks & Calendar Queries) ---
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

    wd = dt_mm.weekday()
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


# --- Timestamp, Chat Memory, Skill Versioning & Protected Core Rules Helpers ---
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


def _sanitize_core_rules(raw_rules: str) -> str:
    """အရင် DB ထဲမှာ ပုံသေစကားလုံးစည်းမျဉ်းဟောင်း ကျန်နေသေးရင် ဖယ်ရှားပေးသည်။"""
    text = (raw_rules or "").strip()
    if not text:
        return DEFAULT_CORE_RULES
    lines = []
    for line in text.splitlines():
        if "စကားအဆုံးတွင် 'နော်'" in line or "ပါနော်" in line:
            continue
        lines.append(line)
    cleaned = "\n".join(lines).strip()
    return cleaned or DEFAULT_CORE_RULES


def get_user_profile_record(user_id: int) -> dict:
    """user_profile row တစ်ခုလုံး (core_rules, version, profile_summary, style_preferences) ကို ဖတ်သည်။"""
    try:
        res = (
            supabase.table("user_profile")
            .select("*")
            .eq("user_id", user_id)
            .limit(1)
            .execute()
        )
        rows = res.data or []
        if rows:
            row = rows[0]
            row["core_rules"] = _sanitize_core_rules(row.get("core_rules") or "")
            if not row.get("version"):
                row["version"] = 1
            return row
    except Exception as e:
        print(f"Error reading user_profile record: {e}")

    return {
        "user_id": user_id,
        "core_rules": DEFAULT_CORE_RULES,
        "version": 1,
        "profile_summary": "",
        "style_preferences": "",
    }


def get_user_profile(user_id: int) -> str:
    row = get_user_profile_record(user_id)
    core_rules = (row.get("core_rules") or DEFAULT_CORE_RULES).strip()
    version = row.get("version") or 1
    summary = (row.get("profile_summary") or "").strip()
    prefs = (row.get("style_preferences") or "").strip()

    parts = [f"Locked Core Rules (v{version}):\n{core_rules}"]
    if summary:
        parts.append(f"Profile Notes:\n{summary}")
    if prefs:
        parts.append(f"Style & Routing Preferences:\n{prefs}")
    return "\n\n".join(parts)


def update_core_rule_version(user_id: int, new_rule_line: str, note: str = "User rule update") -> str:
    """Core rule အသစ် ထည့်/ပြင်တိုင်း အရင် version ကို မဖျက်ဘဲ skill_versions ထဲ သိမ်းပြီး version တိုးပေးသည်။"""
    try:
        rec = get_user_profile_record(user_id)
        old_version = int(rec.get("version") or 1)
        old_rules = (rec.get("core_rules") or DEFAULT_CORE_RULES).strip()
        prefs = (rec.get("style_preferences") or "").strip()

        try:
            supabase.table("skill_versions").insert({
                "user_id": int(user_id),
                "version": old_version,
                "core_rules": old_rules,
                "style_preferences": prefs,
                "note": note,
            }).execute()
        except Exception as ver_err:
            print(f"[Skill Version Archive Notice]: {ver_err}")

        new_version = old_version + 1
        updated_rules = f"{old_rules}\n- {new_rule_line.strip()}"

        payload = {
            "user_id": int(user_id),
            "core_rules": updated_rules,
            "version": new_version,
            "profile_summary": rec.get("profile_summary") or "",
            "style_preferences": prefs,
            "updated_at": datetime.now(MM_TZ).isoformat(),
        }
        try:
            supabase.table("user_profile").upsert(payload, on_conflict="user_id").execute()
        except Exception:
            supabase.table("user_profile").upsert({
                "user_id": int(user_id),
                "profile_summary": rec.get("profile_summary") or "",
                "style_preferences": f"{prefs}\n[Core Rule v{new_version}]: {new_rule_line.strip()}".strip(),
                "updated_at": datetime.now(MM_TZ).isoformat(),
            }, on_conflict="user_id").execute()

        return f"Core Rule အသစ်ကို Version v{new_version} အဖြစ် မှတ်သားပြီး အရင် v{old_version} ကိုလည်း သိမ်းပေးထားပါပြီရှင်"
    except Exception as e:
        print(f"Error updating core rule version: {e}")
        return "Rule version သိမ်းရာမှာ ခဏအဆင်မပြေ ဖြစ်သွားပါတယ်၊ ထပ်စမ်းကြည့်ပါရှင်"


def get_skill_versions_summary(user_id: int) -> str:
    rec = get_user_profile_record(user_id)
    cur_v = rec.get("version") or 1
    cur_rules = (rec.get("core_rules") or DEFAULT_CORE_RULES).strip()
    lines = [f"📌 လက်ရှိ Active Core Rules (v{cur_v}):\n{cur_rules}"]

    try:
        hist_res = (
            supabase.table("skill_versions")
            .select("version, note, created_at")
            .eq("user_id", user_id)
            .order("version", desc=True)
            .limit(5)
            .execute()
        )
        rows = hist_res.data or []
        if rows:
            hist_lines = [
                f"• v{r.get('version')} ({_format_mm_timestamp(r.get('created_at'))}) — {r.get('note') or 'Saved'}"
                for r in rows
            ]
            lines.append("\n🗂️ သိမ်းထားပြီးသား Version မှတ်တမ်းများ:\n" + "\n".join(hist_lines))
    except Exception:
        pass

    lines.append("\n💡 Rule အသစ်ထည့်ချင်ရင် `/setrule <rule>` လို့ ရေးထည့်နိုင်ပါတယ်ရှင်")
    return "\n".join(lines)


def _extract_json_from_text(raw_text: str) -> dict | None:
    if not raw_text:
        return None
    cleaned = re.sub(r"```(?:json)?", "", raw_text, flags=re.IGNORECASE).replace("```", "").strip()
    match = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _call_json_intelligence(prompt: str) -> dict | None:
    """စကားပြော system prompt မရောဘဲ သန့်ရှင်းတဲ့ JSON ကိုသာ ထုတ်ယူသည့် Intelligence helper။"""
    json_sys = "You are a strict JSON intent & entity parser. Return ONLY valid JSON with no markdown or extra text."

    for model_name in ("gemini-3.5-flash-lite", "gemini-3.8-flash"):
        try:
            resp = gemini_client.models.generate_content(
                model=model_name,
                contents=prompt,
                config=types.GenerateContentConfig(
                    system_instruction=json_sys,
                    response_mime_type="application/json",
                    temperature=0.0,
                ),
            )
            if resp and resp.text:
                parsed = _extract_json_from_text(resp.text)
                if parsed is not None:
                    return parsed
        except Exception as e:
            print(f"[JSON Intel {model_name} Error]: {e}")

    try:
        completion = groq_client.with_options(timeout=5.0).chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[
                {"role": "system", "content": json_sys},
                {"role": "user", "content": prompt},
            ],
            temperature=0.0,
            max_tokens=320,
        )
        raw_groq = (completion.choices[0].message.content or "").strip()
        return _extract_json_from_text(raw_groq)
    except Exception as e:
        print(f"[JSON Intel Groq Fallback Error]: {e}")

    return None


def evolve_user_profile(user_id: int, user_msg: str, assistant_reply: str):
    """Locked core_rules ကို မထိဘဲ dynamic profile_summary နဲ့ style_preferences ကိုသာ Self-improve လုပ်သည်။"""
    try:
        rec = get_user_profile_record(user_id)
        current_profile = get_user_profile(user_id)
        update_prompt = f"""
You are updating the dynamic memory notes of the user while preserving their Locked Core Rules untouched.

Existing Profile & Locked Rules:
{current_profile}

Latest Exchange:
User: {user_msg}
Assistant: {assistant_reply[:400]}

Return ONLY valid JSON with two concise fields in Burmese (max ~150 words each):
1. "profile_summary": Key facts, work context, recurring topics, and routing clarifications. Drop outdated/contradictory trivia.
2. "style_preferences": Natural female Burmese assistant tone/style preferences learned from conversation (e.g. 'ရှင်', warm/natural phrasing, no forced sentence-ending words).

JSON format:
{{"profile_summary":"...","style_preferences":"..."}}
"""
        parsed = _call_json_intelligence(update_prompt)
        if not parsed:
            return

        summary = str(parsed.get("profile_summary") or "").strip()
        prefs = str(parsed.get("style_preferences") or "").strip()
        if not summary and not prefs:
            return

        payload = {
            "user_id": int(user_id),
            "core_rules": rec.get("core_rules") or DEFAULT_CORE_RULES,
            "version": int(rec.get("version") or 1),
            "profile_summary": summary,
            "style_preferences": prefs,
            "updated_at": datetime.now(MM_TZ).isoformat(),
        }
        try:
            supabase.table("user_profile").upsert(payload, on_conflict="user_id").execute()
        except Exception:
            supabase.table("user_profile").upsert({
                "user_id": int(user_id),
                "profile_summary": summary,
                "style_preferences": prefs,
                "updated_at": datetime.now(MM_TZ).isoformat(),
            }, on_conflict="user_id").execute()
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
        sections.append(f"--- Learned User Profile & Locked Rules ---\n{profile_text}")

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


# --- Resilient Multi-Tier AI Fallback Helper (Gemini -> OpenRouter + Qwen 3.8 27B -> Groq + Qwen 3.8 27B) ---
def call_ai_with_3tier_fallback(prompt_text, image_bytes=None, mime_type=None, use_google_search=False):
    errors = []
    contents = (
        [types.Part.from_bytes(data=bytes(image_bytes), mime_type=mime_type or 'image/jpeg'), prompt_text]
        if image_bytes else prompt_text
    )
    tools = [types.Tool(google_search=types.GoogleSearch())] if use_google_search else None
    config = types.GenerateContentConfig(
        system_instruction=STRICT_MYANMAR_SYSTEM,
        tools=tools,
    )

    # Tier 1: Gemini 3.5 Flash-Lite
    for attempt in range(2):
        try:
            response = gemini_client.models.generate_content(
                model="gemini-3.5-flash-lite",
                contents=contents,
                config=config
            )
            if response and response.text and response.text.strip():
                return _clean_maya_voice(response.text.strip())
        except Exception as e:
            err_msg = f"T1({attempt+1}): {type(e).__name__} - {str(e)[:90]}"
            print(err_msg)
            errors.append(err_msg)
            if "429" in str(e) or "RESOURCE_EXHAUSTED" in str(e):
                break
            if attempt == 0:
                time.sleep(1.0)

    # Tier 2: Gemini 3.8 Flash
    try:
        response = gemini_client.models.generate_content(
            model="gemini-3.8-flash",
            contents=contents,
            config=config
        )
        if response and response.text and response.text.strip():
            return _clean_maya_voice(response.text.strip())
    except Exception as e:
        err_msg = f"T2: {type(e).__name__} - {str(e)[:90]}"
        print(err_msg)
        errors.append(err_msg)

    # Tier 3A: OpenRouter (Qwen 3.8 27B + Free Fallbacks)
    if OPENROUTER_API_KEY:
        openrouter_models = [
            "qwen/qwen3.8-27b",
            "google/gemma-4-31b-it:free",
            "meta-llama/llama-3.3-70b-instruct:free",
        ]
        headers = {
            "Authorization": f"Bearer {OPENROUTER_API_KEY}",
            "Content-Type": "application/json",
        }
        for or_model in openrouter_models:
            try:
                data = {
                    "model": or_model,
                    "messages": [
                        {"role": "system", "content": STRICT_MYANMAR_SYSTEM},
                        {"role": "user", "content": prompt_text}
                    ],
                    "temperature": 0.4,
                }
                response = requests.post(
                    "https://openrouter.ai/api/v1/chat/completions",
                    headers=headers,
                    json=data,
                    timeout=12,
                )
                res_json = response.json()
                if "choices" in res_json and len(res_json["choices"]) > 0:
                    txt = (res_json["choices"][0]["message"].get("content") or "").strip()
                    if txt:
                        return _clean_maya_voice(txt)
                else:
                    errors.append(f"T3A({or_model}): {str(res_json)[:90]}")
            except Exception as e:
                err_msg = f"T3A({or_model}): {type(e).__name__} - {str(e)[:90]}"
                print(err_msg)
                errors.append(err_msg)

    # Tier 3B: Groq (Qwen 3.8 27B + Active Fallback)
    groq_models = ["qwen/qwen3.8-27b", "llama-3.1-8b-instant"]
    for g_model in groq_models:
        try:
            completion = groq_client.with_options(timeout=12.0).chat.completions.create(
                model=g_model,
                messages=[
                    {"role": "system", "content": STRICT_MYANMAR_SYSTEM},
                    {"role": "user", "content": prompt_text}
                ],
                temperature=0.4,
            )
            if completion and completion.choices and completion.choices[0].message.content:
                txt = completion.choices[0].message.content.strip()
                if txt:
                    return _clean_maya_voice(txt)
        except Exception as e:
            err_msg = f"T3B({g_model}): {type(e).__name__} - {str(e)[:90]}"
            print(err_msg)
            errors.append(err_msg)

    debug_tail = "\n".join(errors[-3:])
    return f"လောလောဆယ် AI ဆာဗာ ခဏအလုပ်များနေပါတယ်ရှင်၊ ခဏနေပြီး ထပ်စမ်းကြည့်ပေးပါ:\n{debug_tail}"


# --- Single Smart Intelligence Classifier (TASK / GROUP / ADVICE / GENERAL / WEB_SEARCH) ---
def _extract_weekday_target(text: str) -> int | None:
    t = text.lower()
    mapping = {
        0: ["တနင်္လာ", "monday", "mon"],
        1: ["အင်္ဂါ", "tuesday", "tue"],
        2: ["ဗုဒ္ဓဟူး", "wednesday", "wed"],
        3: ["ကြာသပတေး", "thursday", "thu"],
        4: ["သောကြာ", "friday", "fri"],
        5: ["စနေ", "saturday", "sat"],
        6: ["တနင်္ဂနွေ", "sunday", "sun"],
    }
    for idx, keys in mapping.items():
        if any(k in t for k in keys):
            return idx
    return None


def _parse_time_parts_mmt(text: str) -> tuple[int, int] | None:
    mm_digits = str.maketrans("၀၁၂၃၄၅၆၇၈၉", "0123456789")
    norm = text.translate(mm_digits)
    lower = text.lower()

    hm = re.search(r"\b(\d{1,2}):(\d{2})\b", norm)
    if hm:
        h, m = int(hm.group(1)), int(hm.group(2))
        if 0 <= h <= 23 and 0 <= m <= 59:
            return h, m

    hr = re.search(r"(\d{1,2})\s*(?:နာရီ|hour|hr)", norm, re.IGNORECASE)
    if not hr:
        return None

    h = int(hr.group(1))
    after_hr = norm[hr.end():hr.end() + 12]
    m = 0
    if "ခွဲ" in after_hr or "half" in after_hr.lower():
        m = 30
    else:
        min_m = re.search(r"(\d{1,2})\s*(?:မိနစ်|min)", after_hr, re.IGNORECASE)
        if min_m:
            m = int(min_m.group(1))

    if ("ညနေ" in text or "ည" in text or "pm" in lower) and h < 12:
        h += 12
    elif ("မနက်" in text or "am" in lower) and h == 12:
        h = 0

    if 0 <= h <= 23 and 0 <= m <= 59:
        return h, m
    return None


def parse_reminder_datetime_mmt(text: str) -> datetime | None:
    now_mm = datetime.now(MM_TZ)
    mm_digits = str.maketrans("၀၁၂၃၄၅၆၇၈၉", "0123456789")
    norm_text = text.translate(mm_digits)

    rel_min = re.search(r"(?:နောက်\s*)?(\d{1,3})\s*(?:မိနစ်|minute|min)s?", norm_text, re.IGNORECASE)
    if rel_min and "နာရီ" not in norm_text:
        mins = int(rel_min.group(1))
        if 1 <= mins <= 1440:
            return now_mm + timedelta(minutes=mins)

    parts = _parse_time_parts_mmt(text)
    if not parts:
        return None
    hour, minute = parts

    target_date = now_mm.date()
    target_wd = _extract_weekday_target(text)

    if target_wd is not None:
        days_ahead = (target_wd - now_mm.weekday()) % 7
        candidate_check = datetime(now_mm.year, now_mm.month, now_mm.day, hour, minute, 0, tzinfo=MM_TZ) + timedelta(days=days_ahead)
        if days_ahead == 0 and candidate_check <= now_mm:
            days_ahead = 7
        target_date = (now_mm + timedelta(days=days_ahead)).date()
    elif "မနက်ဖြန်" in text or "tomorrow" in text.lower():
        target_date = (now_mm + timedelta(days=1)).date()

    candidate = datetime(target_date.year, target_date.month, target_date.day, hour, minute, 0, tzinfo=MM_TZ)
    if target_wd is None and candidate < (now_mm - timedelta(minutes=1)) and "မနက်ဖြန်" not in text and "tomorrow" not in text.lower():
        candidate += timedelta(days=1)
    return candidate


def _clean_task_title_fallback(raw_text: str) -> str:
    t = raw_text.strip()
    t = re.sub(r"^(?:မဟုတ်ဘူးလေ|မဟုတ်ဘူး|Task\s*[-–—:]?)", "", t, flags=re.IGNORECASE)
    t = re.sub(r"(?:အပတ်စဉ်|နေ့စဉ်|ကျောင်းဖွင့်ရက်|မြန်မာ\s*calendar|တိုင်း)", "", t, flags=re.IGNORECASE)
    t = re.sub(r"(?:sunday|monday|tuesday|wednesday|thursday|friday|saturday|တနင်္လာ|အင်္ဂါ|ဗုဒ္ဓဟူး|ကြာသပတေး|သောကြာ|စနေ|တနင်္ဂနွေ)", "", t, flags=re.IGNORECASE)
    t = re.sub(r"(?:မနက်|ညနေ|ည|နောက်\s*)?[၀-၉\d]+\s*(?:နာရီ|hour|hr)(?:\s*(?:ခွဲ|[၀-၉\d]+\s*မိနစ်))?", "", t, flags=re.IGNORECASE)
    t = re.sub(r"(?:နောက်\s*)?[၀-၉\d]+\s*(?:မိနစ်|minute|min)s?\s*(?:မှာ|နေရင်|အကြာမှာ)?", "", t, flags=re.IGNORECASE)
    t = re.sub(r"\b\d{1,2}:\d{2}\b", "", t)
    t = re.sub(r"(?:remind\s*လုပ်ပေးပါ|remind\s*ပေးပါ|သတိပေးပါ|သတိပေး|remind|ပြောတယ်|မှာ)", "", t, flags=re.IGNORECASE)
    t = re.sub(r"\s+", " ", t).strip(" -–—၊။")
    return t or raw_text.strip()


def smart_route_and_parse(
    user_message: str,
    available_groups: list,
    recent_ctx: str = "",
    user_pref_ctx: str = "",
) -> dict:
    now_mm = datetime.now(MM_TZ)
    cal_info = get_myanmar_calendar_context(now_mm)
    groups_str = ", ".join(available_groups[:25]) if available_groups else "None"

    prompt = f"""
Understand the user's intent naturally from meaning and conversation context (not rigid keywords).
Calendar Context (especially useful for tasks/reminders):
{cal_info}

Available Telegram Groups: [{groups_str}]

User Preferences & Locked Rules:
{user_pref_ctx[:450] or "None"}

Recent Conversation:
{recent_ctx or "None"}

Latest User Message: "{user_message}"

Classify into ONE primary intent:
- "TASK": Setting/updating/completing/listing personal reminders or to-dos, or correcting a task entry.
- "GROUP": Asking about anything discussed/reported/shared in Telegram groups, asking for a group summary/report, or correcting a misrouted group query.
- "ADVICE": Seeking recommendations, options, how-to guidance, or decision support.
- "GENERAL": Translating, drafting, explaining concepts, recalling personal chat/profile, or casual chat.
- "WEB_SEARCH": Looking up external/live/current facts, news, sports/scores, weather, prices, or web info not from private/group logs.

If intent is "WEB_SEARCH", also output:
- "search_query": A self-contained search query combining Recent Conversation context if the user says a follow-up like "Yes, ရှာပေးပါ" or "အသေးစိတ် ရှာပေးပါ" (e.g. "Chelsea match result yesterday").

If intent is "TASK", also extract:
- "task_action": "add" | "done" | "list" | "cancel_last"
- "clean_title": pure action in Burmese without time/schedule/reason/filler words
- "reason": short reason if stated, else null
- "repeat_type": "once" | "daily" | "weekly" | "school_days"
- "remind_iso": ISO-8601 datetime with +06:30 offset for the next occurrence in MMT, or null

If intent is "GROUP", also extract:
- "group_name": exact matching group from Available Groups if clear, else null
- "group_mode": "summary" | "query"
- "period": "today" | "yesterday" | "recent"

Return ONLY valid JSON:
{{
  "intent": "TASK|GROUP|ADVICE|GENERAL|WEB_SEARCH",
  "search_query": null,
  "task_action": "add",
  "clean_title": null,
  "reason": null,
  "repeat_type": "once",
  "remind_iso": null,
  "group_name": null,
  "group_mode": "query",
  "period": "recent"
}}
"""
    parsed = _call_json_intelligence(prompt)
    if not parsed:
        return {
            "intent": "GENERAL",
            "search_query": None,
            "task_action": "add",
            "clean_title": _clean_task_title_fallback(user_message),
            "reason": None,
            "repeat_type": "once",
            "remind_dt": None,
            "group_name": None,
            "group_mode": "query",
            "period": "recent",
        }

    intent = str(parsed.get("intent") or "GENERAL").upper().strip()
    if intent not in {"TASK", "GROUP", "ADVICE", "GENERAL", "WEB_SEARCH"}:
        intent = "GENERAL"

    target_wd = _extract_weekday_target(user_message)
    repeat_type = str(parsed.get("repeat_type") or "once").strip()
    if repeat_type not in {"once", "daily", "weekly", "school_days"}:
        repeat_type = "once"

    rem_dt = None
    if parsed.get("remind_iso"):
        try:
            dt_obj = datetime.fromisoformat(str(parsed["remind_iso"]))
            rem_dt = dt_obj.replace(tzinfo=MM_TZ) if dt_obj.tzinfo is None else dt_obj.astimezone(MM_TZ)
        except Exception:
            rem_dt = parse_reminder_datetime_mmt(user_message)
    elif intent == "TASK":
        rem_dt = parse_reminder_datetime_mmt(user_message)

    if rem_dt:
        if repeat_type == "school_days":
            while rem_dt.weekday() >= 5:
                rem_dt += timedelta(days=1)
        elif repeat_type == "weekly" and target_wd is not None:
            days_ahead = (target_wd - rem_dt.weekday()) % 7
            if days_ahead == 0 and rem_dt <= now_mm:
                days_ahead = 7
            rem_dt += timedelta(days=days_ahead)

    raw_title = parsed.get("clean_title")
    clean_title = (str(raw_title).strip() if raw_title else "") or _clean_task_title_fallback(user_message)
    clean_title = _clean_task_title_fallback(clean_title) or _clean_task_title_fallback(user_message)

    raw_reason = parsed.get("reason")
    reason = str(raw_reason).strip() if raw_reason else None

    raw_sq = parsed.get("search_query")
    search_query = str(raw_sq).strip() if raw_sq else None

    return {
        "intent": intent,
        "search_query": search_query,
        "task_action": str(parsed.get("task_action") or "add").lower().strip(),
        "clean_title": clean_title,
        "reason": reason,
        "repeat_type": repeat_type,
        "remind_dt": rem_dt,
        "group_name": parsed.get("group_name") or None,
        "group_mode": str(parsed.get("group_mode") or "query").lower().strip(),
        "period": str(parsed.get("period") or "recent").lower().strip(),
    }


def add_task(
    title: str,
    remind_dt: datetime | None = None,
    reason: str | None = None,
    repeat_type: str = "once",
) -> dict:
    payload = {
        "title": title,
        "status": "pending",
        "repeat_type": repeat_type or "once",
    }
    if reason:
        payload["reason"] = reason
    if remind_dt:
        payload["remind_at"] = remind_dt.isoformat()
        payload["reminded"] = False
    try:
        supabase.table("tasks").insert(payload).execute()
    except Exception:
        fallback_payload = {"title": title, "status": "pending"}
        if remind_dt:
            fallback_payload["remind_at"] = remind_dt.isoformat()
            fallback_payload["reminded"] = False
        supabase.table("tasks").insert(fallback_payload).execute()
    return {"status": "success", "message": f"Task added: {title}"}


def list_tasks() -> list:
    res = supabase.table("tasks").select("*").order("id").execute()
    return res.data


def get_formatted_tasks() -> str:
    tasks = list_tasks()
    if not tasks:
        return "လက်ရှိ Task မရှိသေးပါဘူးရှင်"
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
        lines.append(f"{mark} {t['title']}{r_str}")
    return "လက်ရှိ Task စာရင်းလေးပါရှင်\n" + "\n".join(lines)


def delete_latest_pending_task() -> str | None:
    try:
        last_res = (
            supabase.table("tasks")
            .select("id, title")
            .eq("status", "pending")
            .order("id", desc=True)
            .limit(1)
            .execute()
        )
        rows = last_res.data or []
        if rows:
            t_id = rows[0]["id"]
            t_title = rows[0].get("title", "")
            supabase.table("tasks").delete().eq("id", t_id).execute()
            return t_title
    except Exception as e:
        print(f"Error deleting latest pending task: {e}")
    return None


def handle_task_intent(user_message: str, route: dict) -> str:
    action = (route.get("task_action") or "add").lower().strip()
    task_title = (route.get("clean_title") or "").strip()
    reason = route.get("reason")
    use_maung = _is_health_or_family_task(f"{task_title} {user_message}", reason)
    prefix = "မောင်ရေ — " if use_maung else ""
    ending = "နော်" if use_maung else "ရှင်"

    if action == "list":
        return get_formatted_tasks()

    if action == "cancel_last":
        removed = delete_latest_pending_task()
        if removed:
            is_hf = _is_health_or_family_task(removed)
            p = "မောင်ရေ — " if is_hf else ""
            e = "နော်" if is_hf else "ရှင်"
            return f"{p}\"{removed}\" ကို Task ထဲက ပြန်ဖျက်ပေးလိုက်ပါပြီ{e}"
        return f"{prefix}ပြန်ဖျက်စရာ Task မတွေ့ပါဘူး{ending}"

    if action == "done":
        try:
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
                pending_tasks[0] if pending_tasks else None
            )
            if matched:
                m_title = matched["title"]
                is_hf = _is_health_or_family_task(m_title, matched.get("reason"))
                p = "မောင်ရေ — " if is_hf else ""
                e = "နော်" if is_hf else "ရှင်"
                supabase.table("tasks").update({"status": "done"}).eq("id", matched["id"]).execute()
                return f"{p}\"{m_title}\" ပြီးသွားပြီမို့ Done မှတ်လိုက်ပါပြီ{e}"

            return f"{prefix}ပြီးစီးကြောင်း မှတ်ရမယ့် Pending Task မတွေ့သေးပါဘူး{ending}"
        except Exception as e:
            print(f"Task update error: {e}")
            return f"{prefix}Task အခြေအနေ ပြောင်းရာမှာ ခဏအဆင်မပြေ ဖြစ်သွားပါတယ်၊ ထပ်စမ်းကြည့်ပါ{ending}"

    clean_title = task_title or _clean_task_title_fallback(user_message)
    remind_dt = route.get("remind_dt")
    repeat_mode = route.get("repeat_type", "once")
    use_maung = _is_health_or_family_task(f"{clean_title} {user_message}", reason)
    prefix = "မောင်ရေ — " if use_maung else ""
    ending = "နော်" if use_maung else "ရှင်"

    is_correction = any(w in user_message for w in ["မဟုတ်ဘူး", "ပြောတယ်", "ပြင်"])
    if is_correction:
        try:
            latest_res = (
                supabase.table("tasks")
                .select("*")
                .eq("status", "pending")
                .order("id", desc=True)
                .limit(1)
                .execute()
            )
            latest_rows = latest_res.data or []
            if latest_rows:
                last_id = latest_rows[0]["id"]
                upd = {"title": clean_title, "repeat_type": repeat_mode or "once"}
                if reason:
                    upd["reason"] = reason
                if remind_dt:
                    upd["remind_at"] = remind_dt.isoformat()
                    upd["reminded"] = False
                try:
                    supabase.table("tasks").update(upd).eq("id", last_id).execute()
                except Exception:
                    supabase.table("tasks").update({"title": clean_title}).eq("id", last_id).execute()
            else:
                add_task(clean_title, remind_dt, reason=reason, repeat_type=repeat_mode)
        except Exception:
            add_task(clean_title, remind_dt, reason=reason, repeat_type=repeat_mode)
    else:
        add_task(clean_title, remind_dt, reason=reason, repeat_type=repeat_mode)

    reply_line = f"{prefix}\"{clean_title}\" မှတ်ထားလိုက်ပါပြီ{ending}"
    if remind_dt:
        reply_line += f"\n⏰ {remind_dt.strftime('%Y-%m-%d %H:%M')} ({MYANMAR_WEEKDAYS[remind_dt.weekday()]})"
    if repeat_mode == "school_days":
        reply_line += " · ကျောင်းဖွင့်ရက်"
    elif repeat_mode == "weekly":
        reply_line += " · အပတ်စဉ်"
    elif repeat_mode == "daily":
        reply_line += " · နေ့စဉ်"
    if reason:
        reply_line += f"\n📝 {reason}"
    return reply_line


# --- 2. Group Summary, Report & Confidence-Tagged Natural Group Q&A Tools ---
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
    """Group logs များကို တိုက်ရိုက်ဖတ်ပြီး Confidence Tag ([Verified] / [Deduced] / [Unverified]) နှင့်တကွ ဖြေပေးသည်။"""
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
        return f"သက်ဆိုင်ရာ ကာလ{scope_d} အတွင်း Group မှတ်တမ်းတွေ မရှိသေးပါဘူးရှင်"

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
        return f"{matched_group or 'သက်ဆိုင်ရာ Group'} ထဲမှာ မှတ်တမ်း မရှိသေးပါဘူးရှင်"

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
    1. မြန်မာမိန်းကလေးတစ်ယောက်လို သဘာဝကျကျ ရင်းနှီးဖော်ရွေစွာ ပြောပါ ('မောင်' ဟု မသုံးပါနှင့်၊ စကားလုံးတစ်ခုခုကို ပုံသေအတင်းမထည့်ပါနှင့်)။
    2. Confidence Tagging: အဓိက အချက်အလက်တိုင်း၏ အနောက်တွင် အောက်ပါ Tag တစ်ခုခု တွဲထည့်ပေးပါ —
       - [Verified] = Group မှတ်တမ်းထဲတွင် တိုက်ရိုက် ပါဝင်သော အချက်
       - [Deduced] = မှတ်တမ်းများမှ ဆက်စပ်သုံးသပ်ထားသော အချက်
       - [Unverified] = မှတ်တမ်းထဲတွင် တိကျစွာ မပါသေးသော အချက်
    3. စက်ရုပ်လို တစ်ကြောင်းချင်းစီ၏ အချိန် (Timestamp) တွေ ထည့်ရေးစရာ မလိုပါ (အသုံးပြုသူက ဘယ်အချိန်လဲဟု မေးမှသာ အချိန်ကို ထည့်ပြောပါ)။
    4. မေးထားသည့် အချက်/လူနှင့် မသက်ဆိုင်သော အခြားလူများ၏ စကားများကို လုံးဝ ထည့်မပြောပါနှင့်။

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
            return f"{date_label}{scope_desc} အတွက် မှတ်တမ်း မရှိသေးပါဘူးရှင်"
        return f"ဖတ်ရမယ့် မက်ဆေ့ခ်ျအသစ် မရှိသေးပါဘူး{scope_desc} ရှင်"

    raw_text = "\n".join(
        [f"[{l['group_name']} | {l['sender_name']} | {str(l.get('created_at', ''))[:16]}]: {l['content_summary']}" for l in logs]
    )

    header_info = f"Group: {matched_group_name or 'All Groups'} | Period: {date_label or 'Unread'}"

    prompt = f"""
    အောက်ပါတို့မှာ Telegram Group များမှ ရရှိထားသော မက်ဆေ့ခ်ျများနှင့် ပုံ/ဖိုင် အနှစ်ချုပ်များ ဖြစ်သည်။
    ({header_info})
    မြန်မာမိန်းကလေးတစ်ယောက်လို သဘာဝကျကျ ရင်းနှီးဖော်ရွေစွာ ပြောပါ ('မောင်' ဟု မသုံးပါနှင့်၊ စကားလုံးတစ်ခုခုကို ပုံသေအတင်းမထည့်ပါနှင့်)။
    နေ့စဉ် တွေ့နေကျ ရိုးရိုးနှုတ်ဆက်စကားများ၊ အရေးမပါသော စကားပြောဆိုမှုများကို လုံးဝ ဖယ်ထုတ်ပါ။
    အချက်တစ်ခုချင်းစီ၏ အနောက်တွင် [Verified] (မှတ်တမ်းထဲ တိုက်ရိုက်ပါဝင်ချက်) သို့မဟုတ် [Deduced] (ဆက်စပ်သုံးသပ်ချက်) tag ထည့်ပေးပါ။
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


# --- Supabase Pending Follow-ups Queue ---
def queue_pending_followup(user_id: int, followup_type: str, question: str, target_group: str = None, delay_seconds: int = 30):
    try:
        if followup_type == "web_search":
            return
        now_mm = datetime.now(MM_TZ)
        retry_dt = now_mm + timedelta(seconds=delay_seconds)
        supabase.table("pending_followups").insert({
            "user_id": user_id,
            "followup_type": followup_type,
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
    try:
        res = (
            supabase.table("pending_followups")
            .select("*")
            .eq("status", "pending")
            .order("id")
            .limit(20)
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

            if f_type == "web_search":
                supabase.table("pending_followups").update({"status": "cancelled"}).eq("id", item_id).execute()
                continue

            try:
                created_dt = datetime.fromisoformat(str(item.get("created_at")).replace("Z", "+00:00")).astimezone(MM_TZ)
                if (now_mm - created_dt).total_seconds() > 3600 or attempts >= 2:
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
                    if ans and "မရှိသေးပါဘူး" not in ans:
                        msg = f"ခုဏက Group အချက်အလက်လေး ပြန်ပြောပေးတာပါရှင်\n\n{ans}"
                        await context.bot.send_message(chat_id=user_id, text=msg[:3500])
                        save_chat_message(user_id, "assistant", msg)
                        supabase.table("pending_followups").update({"status": "completed"}).eq("id", item_id).execute()
                    else:
                        supabase.table("pending_followups").update({
                            "attempts": attempts + 1
                        }).eq("id", item_id).execute()

    except Exception as e:
        print(f"Error in check_pending_followups_job: {e}")


# --- Direct Web Search Helper (Tavily + Google Search Grounding) ---
def _tavily_worker(search_query: str, max_results: int = 6, exclude_social: bool = True) -> str:
    if not TAVILY_API_KEY:
        return ""
    try:
        payload = {
            "api_key": TAVILY_API_KEY,
            "query": search_query,
            "search_depth": "advanced",
            "topic": "news",
            "days": 2,
            "max_results": max_results,
            "include_answer": "advanced",
        }
        if exclude_social:
            payload["exclude_domains"] = ["youtube.com", "m.youtube.com", "facebook.com", "tiktok.com"]

        resp = requests.post("https://api.tavily.com/search", json=payload, timeout=9)
        if resp.status_code != 200:
            return ""
        data = resp.json()
        lines = []
        if data.get("answer"):
            lines.append(f"[Summary]: {data['answer']}")
        for r in data.get("results", []):
            title = r.get("title", "")
            url = r.get("url", "")
            content = (r.get("content", "") or "")[:400]
            lines.append(f"- {title} ({url}): {content}")
        return "\n".join(lines)
    except Exception as e:
        print(f"Tavily search error: {e}")
        return ""


# --- Dedicated Myanmar News Summary (Local & Independent + International Coverage) ---
def get_myanmar_news_summary() -> str:
    """
    Web Search (Tavily + Google Search grounding) မှတစ်ဆင့်
    Local & Independent Outlets (Myanmar Now, The Irrawaddy, Mizzima, Frontier Myanmar, Khit Thit Media)
    နှင့် International Coverage (BBC Burmese, VOA Burmese, RFA Burmese, Reuters) သတင်းများကို
    တိုက်ရိုက် ရှာဖွေအနှစ်ချုပ်ပေးသည်။
    """
    now_mm = datetime.now(MM_TZ)
    now_str = now_mm.strftime("%Y-%m-%d %H:%M MMT")
    today_date = now_mm.strftime("%Y-%m-%d")

    local_query = (
        f"Myanmar news events {today_date} "
        f"Irrawaddy Myanmar Now Mizzima Frontier Khit Thit"
    )
    intl_query = (
        f"Myanmar news {today_date} "
        f"BBC Burmese VOA RFA Reuters"
    )

    local_web_raw = ""
    intl_web_raw = ""

    with ThreadPoolExecutor(max_workers=2) as executor:
        f_local = executor.submit(_tavily_worker, local_query, 6, True)
        f_intl = executor.submit(_tavily_worker, intl_query, 6, True)
        try:
            local_web_raw = f_local.result() or ""
        except Exception as e:
            print(f"Local news web search error: {e}")
        try:
            intl_web_raw = f_intl.result() or ""
        except Exception as e:
            print(f"Intl news web search error: {e}")

    has_web_data = bool((local_web_raw or "").strip() or (intl_web_raw or "").strip())

    prompt = f"""
    လက်ရှိအချိန်: {now_str} (မြန်မာစံတော်ချိန်)

    မြန်မာမိန်းကလေးတစ်ယောက်လို သဘာဝကျကျ ရင်းနှီးဖော်ရွေစွာ ပြောပါ ('မောင်' ဟု မသုံးပါနှင့်၊ စကားလုံးတစ်ခုခုကို ပုံသေအတင်းမထည့်ပါနှင့်၊ 'ကျွန်တော်' ဟု လုံးဝ မသုံးပါနှင့်)။
    အောက်ပါ သတင်းဆောင်းပါး ရလဒ်များနှင့် Google Search တို့မှ **တကယ့် သတင်းဖြစ်ရပ် အကြောင်းအရာများ (ဘယ်နေရာမှာ ဘာဖြစ်ပျက်ခဲ့သလဲ၊ ဘယ်သူတွေ ပါဝင်သလဲ)** ကိုသာ ထုတ်နုတ်ပြီး ကဏ္ဍ (၂) ခုခွဲ၍ မြန်မာဘာသာဖြင့် အနှစ်ချုပ်ပေးပါ:

    ၁။ **ပြည်တွင်းနှင့် လွတ်လပ်သော သတင်းဌာနများ (Myanmar Now, The Irrawaddy, Mizzima, Frontier Myanmar, Khit Thit Media)**
    ၂။ **နိုင်ငံတကာ သတင်းဌာနများ (BBC Burmese, VOA Burmese, RFA Burmese, Reuters)**

    အရေးကြီး စည်းမျဉ်းများ:
    - "သတင်းအစီအစဉ် ထုတ်လွှင့်ခဲ့သည် / ဗီဒီယို တွေ့ရှိရသည်" စသည့် အစီအစဉ်ကြေညာချက်များကို လုံးဝ မရေးပါနှင့်။
    - တကယ့် သတင်းဖြစ်ရပ် အကြောင်းအရာကိုသာ အချက်အလက် တိတိကျကျဖြင့် Bullet point ရေးပြီး အနောက်တွင် [သတင်းဌာနအမည်] [Verified] သို့မဟုတ် [Deduced] ထည့်ပေးပါ။

    --- Local & Independent Outlets Articles ---
    {local_web_raw or "None"}

    --- International Coverage Articles ---
    {intl_web_raw or "None"}
    """

    summary = call_ai_with_3tier_fallback(prompt, use_google_search=True)
    if not summary or "ခဏအလုပ်များနေပါတယ်" in summary:
        if has_web_data:
            summary = call_ai_with_3tier_fallback(prompt, use_google_search=False)

    summary = _clean_maya_voice(summary)
    return f"📰 Myanmar Daily News Summary ({now_str})\n\n{summary}"


# --- Web Search Helper (Context-Aware + Confidence Tagging) ---
def answer_with_universal_search(
    user_id: int,
    user_message: str,
    recent_ctx: str = "",
    effective_query: str | None = None,
) -> str:
    now_mm = datetime.now(MM_TZ)
    now_str = now_mm.strftime("%Y-%m-%d %H:%M MMT")
    profile_text = get_user_profile(user_id)

    raw_q = (effective_query or user_message).strip()
    clean_q = re.sub(r"[?？!]", "", raw_q).strip()
    if len(clean_q) <= 18 and recent_ctx:
        prev_user_lines = [
            line.split(":", 1)[1].strip()
            for line in recent_ctx.splitlines()
            if line.startswith("user:") and user_message.strip() not in line
        ]
        if prev_user_lines:
            clean_q = f"{prev_user_lines[-1]} {clean_q}".strip()

    tavily_raw = _tavily_worker(clean_q, max_results=6, exclude_social=True)
    web_context = f"--- Web Search (Tavily) ---\n{tavily_raw}" if tavily_raw else "--- Web Search Results ---\nNone"

    profile_block = f"--- Learned User Profile & Locked Rules ---\n{profile_text}\n\n" if profile_text else ""
    ctx_block = f"--- Recent Conversation Context ---\n{recent_ctx}\n\n" if recent_ctx else ""

    system_prompt = (
        f"သင်သည် 'မာယာ (Maya)' အမည်ရှိ ကိုယ်ပိုင် AI Assistant ဖြစ်သည်။\n"
        f"လက်ရှိအချိန်: {now_str} (မြန်မာစံတော်ချိန်)\n\n"
        f"မြန်မာမိန်းကလေးတစ်ယောက်လို သဘာဝကျကျ ရင်းနှီးဖော်ရွေစွာ ပြောပါ ('မောင်' ဟု မသုံးပါနှင့်၊ စကားလုံးတစ်ခုခုကို ပုံသေအတင်းမထည့်ပါနှင့်၊ 'ကျွန်တော်' ဟု လုံးဝ မသုံးပါနှင့်)။\n"
        f"အရှေ့နောက် စကားပြောဆိုမှု context နှင့် ရှာဖွေမေးမြန်းချက်အပေါ်သာ တိုက်ရိုက် Focus ထားပြီး ရှင်းလင်းပြတ်သားစွာ မြန်မာဘာသာဖြင့် ဖြေကြားပါ:\n"
        f"1. **တိုက်ရိုက် အဖြေချုပ် (Direct Answer):** မေးခွန်း၏ အဓိကအဖြေကို စာကြောင်း ၁–၂ ကြောင်းဖြင့် အရင်ဆုံး ဖြေပါ။\n"
        f"2. **အဓိက အချက်အလက်များ (Key Points):** သက်ဆိုင်ရာ အချက် (၃–၅ ချက်) ကို Bullet point များဖြင့် ဖော်ပြပြီး အချက်တိုင်း၏ အနောက်တွင် [Verified] (ရှာဖွေမှုရလဒ်တွင် တိုက်ရိုက်ပါဝင်ချက်)၊ [Deduced] (ဆက်စပ်သုံးသပ်ချက်) သို့မဟုတ် [Unverified] (မသေချာသေးသည့်အချက်) tag ထည့်ပေးပါ။\n"
        f"3. မေးခွန်းနှင့် မသက်ဆိုင်သော အချက်အလက်များကို လုံးဝ မထည့်ပါနှင့်။\n"
    )

    full_prompt = (
        f"{system_prompt}\n"
        f"{profile_block}"
        f"{ctx_block}"
        f"{web_context}\n\n"
        f"ရှာဖွေမေးမြန်းချက်: {user_message}\n"
        f"ရှာဖွေခဲ့သည့် Query: {clean_q}"
    )

    reply = call_ai_with_3tier_fallback(full_prompt, use_google_search=True)

    if not reply or "ခဏအလုပ်များနေပါတယ်" in reply:
        fallback_prompt = (
            f"{full_prompt}\n\n"
            f"မှတ်ချက်: Google Search tool ခဏမရသဖြင့် အထက်ပါ Web Search အချက်အလက်များကို အခြေခံ၍ "
            f"Confidence tag ([Verified] / [Deduced] / [Unverified]) များနှင့်တကွ ရှင်းလင်းစွာ ဖြေပေးပါ။"
        )
        reply_no_search = call_ai_with_3tier_fallback(fallback_prompt, use_google_search=False)
        if reply_no_search and "ခဏအလုပ်များနေပါတယ်" not in reply_no_search:
            return _clean_maya_voice(reply_no_search)

    if not reply or "ခဏအလုပ်များနေပါတယ်" in reply:
        return "လောလောဆယ် Web Search ဘက်မှာ ခဏအဆင်မပြေ ဖြစ်နေပါတယ်ရှင်၊ ခဏနေပြီး ထပ်မေးကြည့်ပါ"

    return _clean_maya_voice(reply)


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
    if not ADMIN_USER_ID:
        return
    try:
        now_mm = datetime.now(MM_TZ)
        res = supabase.table("tasks").select("*").execute()
        tasks = res.data or []

        for t in tasks:
            status_val = str(t.get("status") or "pending").strip().lower()
            if status_val != "pending":
                continue
            if t.get("reminded") is True:
                continue

            r_at = t.get("remind_at")
            if not r_at:
                continue

            try:
                r_dt = datetime.fromisoformat(str(r_at).replace("Z", "+00:00"))
                if r_dt.tzinfo is None:
                    r_dt = r_dt.replace(tzinfo=MM_TZ)
                else:
                    r_dt = r_dt.astimezone(MM_TZ)

                if r_dt <= now_mm:
                    title = (t.get("title") or "").strip()
                    use_maung = _is_health_or_family_task(title, t.get("reason"))
                    msg = f"မောင်ရေ — {title} အချိန်ရောက်ပြီနော်" if use_maung else f"{title} အချိန်ရောက်ပါပြီရှင်"
                    await context.bot.send_message(chat_id=ADMIN_USER_ID, text=msg)
                    save_chat_message(ADMIN_USER_ID, "assistant", msg)
                    try:
                        supabase.table("tasks").update({"reminded": True}).eq("id", t["id"]).execute()
                    except Exception:
                        pass
            except Exception as item_err:
                print(f"[Reminder Item Error] id={t.get('id')}: {item_err}")
    except Exception as e:
        print(f"Error in check_due_task_reminders_job: {e}")


async def scheduled_evening_task_digest_job(context: ContextTypes.DEFAULT_TYPE):
    if not ADMIN_USER_ID:
        return
    try:
        res = supabase.table("tasks").select("*").eq("status", "pending").order("id").execute()
        pending = res.data or []
        if not pending:
            return

        lines = [f"• {t['title']}" for t in pending[:10]]
        msg = "ဒီနေ့ ပြီးဖို့ ကျန်နေသေးတဲ့ Task လေးတွေပါရှင်\n" + "\n".join(lines)
        await context.bot.send_message(chat_id=ADMIN_USER_ID, text=msg)
        save_chat_message(ADMIN_USER_ID, "assistant", msg)
    except Exception as e:
        print(f"Error in scheduled_evening_task_digest_job: {e}")


# --- Dedicated 5-Intent Handlers (TASK / GROUP / ADVICE / GENERAL / WEB_SEARCH) ---
def handle_group_intent(user_id: int, user_message: str, route: dict, recent_ctx: str = "") -> str:
    target_group = route.get("group_name")
    period = route.get("period", "recent")
    mode = route.get("group_mode", "query")

    if mode == "summary":
        day_m = period if period in ["today", "yesterday"] else None
        return get_group_report(user_message, target_group, day_m)

    ans = query_specific_group_logs(user_message, target_group, period, recent_ctx)
    if ans and "မရှိသေးပါဘူး" in ans:
        queue_pending_followup(user_id, "group_query", user_message, target_group=target_group, delay_seconds=30)
        g_label = f"\"{target_group}\"" if target_group else "သက်ဆိုင်ရာ Group"
        return f"{g_label} ရဲ့ မက်ဆေ့ခ်ျအချက်အလက်တွေ အခုထိ မရောက်သေးပါဘူးရှင်၊ ရောက်လာတာနဲ့ ချက်ချင်း ပြန်ပြောပေးပါမယ်"
    return ans


def handle_advice_or_general_intent(user_id: int, user_message: str, intent_mode: str = "GENERAL") -> str:
    cal_info = get_myanmar_calendar_context()
    memory_context = recall_relevant_history(user_id, user_message, recent_limit=8, search_limit=10)

    if intent_mode == "ADVICE":
        prompt = (
            f"မြန်မာပြက္ခဒိန် အချက်အလက်:\n{cal_info}\n\n"
            f"မြန်မာမိန်းကလေးတစ်ယောက်လို သဘာဝကျကျ ရင်းနှီးဖော်ရွေစွာ ပြောပါ ('မောင်' ဟု မသုံးပါနှင့်၊ စကားလုံးတစ်ခုခုကို ပုံသေအတင်းမထည့်ပါနှင့်၊ 'ကျွန်တော်' ဟု လုံးဝ မသုံးပါနှင့်)။\n"
            f"အသုံးပြုသူ၏ Locked Core Rules၊ Profile၊ Style Preferences၊ အတိတ်မှတ်ဉာဏ်နှင့် မေးခွန်းကို သုံးသပ်ပြီး "
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
            f"မြန်မာမိန်းကလေးတစ်ယောက်လို သဘာဝကျကျ ရင်းနှီးဖော်ရွေစွာ ပြောပါ ('မောင်' ဟု မသုံးပါနှင့်၊ စကားလုံးတစ်ခုခုကို ပုံသေအတင်းမထည့်ပါနှင့်၊ 'ကျွန်တော်' ဟု လုံးဝ မသုံးပါနှင့်)။\n"
            f"အသုံးပြုသူ၏ Locked Core Rules၊ Profile၊ Style Preferences၊ အတိတ်စကားပြောဆိုမှုများနှင့် ခိုင်းစေချက်ကို သေချာမှတ်မိပြီး "
            f"အလိုက်သိသိနှင့် မြန်မာစာ သန့်သန့်ရှင်းရှင်းဖြင့် ကူညီဆောင်ရွက်ပေးပါ။\n\n"
            f"{memory_context}\n\n"
            f"ခိုင်းစေချက်: {user_message}"
        )

    return call_ai_with_3tier_fallback(prompt, None, None, False)


# --- Quick Menu & Command Handlers ---
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
        [
            InlineKeyboardButton("Core Rules & Versions", callback_data="cmd_view_rules"),
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
            "Quick Menu ပါရှင်",
            reply_markup=build_main_menu_keyboard()
        )
        return

    if cmd == "/rules":
        reply_text = await asyncio.to_thread(get_skill_versions_summary, message.from_user.id)
        await send_long_message(message, reply_text)
        return

    if cmd == "/setrule":
        if not arg:
            await message.reply_text("/setrule <ထပ်ထည့်လိုသည့် rule> လို့ ရေးပေးပါရှင်")
            return
        reply_text = await asyncio.to_thread(update_core_rule_version, message.from_user.id, arg, "Added via /setrule")
        await send_long_message(message, reply_text)
        return

    if cmd == "/addtask":
        if not arg:
            await message.reply_text("/addtask <task description> လို့ ရေးပေးပါရှင်")
            return
        parsed = smart_route_and_parse(arg, [])
        clean_title = parsed.get("clean_title") or _clean_task_title_fallback(arg)
        remind_dt = parsed.get("remind_dt")
        reason = parsed.get("reason")
        repeat_mode = parsed.get("repeat_type", "once")
        add_task(clean_title, remind_dt, reason=reason, repeat_type=repeat_mode)
        is_hf = _is_health_or_family_task(f"{clean_title} {arg}", reason)
        prefix = "မောင်ရေ — " if is_hf else ""
        ending = "နော်" if is_hf else "ရှင်"
        if remind_dt:
            await message.reply_text(f"{prefix}\"{clean_title}\" မှတ်ထားလိုက်ပါပြီ{ending}\n⏰ {remind_dt.strftime('%Y-%m-%d %H:%M')}")
        else:
            await message.reply_text(f"{prefix}\"{clean_title}\" မှတ်ထားလိုက်ပါပြီ{ending}")
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

    if data == "cmd_view_rules":
        reply_text = await asyncio.to_thread(get_skill_versions_summary, query.from_user.id)
        await send_long_message(query.message, reply_text)
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


# --- General Telegram Message Handler (Single Smart 5-Intent Intelligence) ---
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message:
        return

    chat_type = message.chat.type
    sender_name = message.from_user.first_name if message.from_user else "Unknown"
    group_name = message.chat.title if chat_type in ["group", "supergroup"] else "Private"

    # CASE A: Group Messages (Silent observer logger)
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

    # CASE B: Private Chat (Single-user ADMIN_USER_ID restricted)
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
                await message.reply_text("Quick Menu ပါရှင်", reply_markup=build_main_menu_keyboard())
                return

            if stripped_lower in ["မာယာ", "maya", "hi", "hello", "hey", "မင်္ဂလာပါ"]:
                memory_ctx = await asyncio.to_thread(recall_relevant_history, message.from_user.id, user_message, 4, 2)
                greet_prompt = (
                    "မြန်မာမိန်းကလေးတစ်ယောက်လို သဘာဝကျကျ ရင်းနှီးဖော်ရွေစွာ ပြောပါ ('မောင်' ဟု မသုံးပါနှင့်၊ စကားလုံးတစ်ခုခုကို ပုံသေအတင်းမထည့်ပါနှင့်၊ 'ကျွန်တော်' ဟု လုံးဝ မသုံးပါနှင့်)။ "
                    "နှုတ်ဆက်/ခေါ်နေပါသည်။ လက်ရှိ လေ့လာမှတ်သားထားသော Locked Core Rules/Profile/Preferences/Conversation ရှိလျှင် အလိုက်သိသိ "
                    f"ရင်းနှီးဖော်ရွေစွာ နှုတ်ဆက်ပြီး ဘာကူညီပေးနိုင်မလဲ တိုတိုရှင်းရှင်း မြန်မာဘာသာဖြင့် ဖြေပါ။\n\n{memory_ctx}"
                )
                reply_text = await asyncio.to_thread(call_ai_with_3tier_fallback, greet_prompt, None, None, False)
                save_chat_message(message.from_user.id, "assistant", reply_text)
                await send_long_message(message, reply_text)
                return

            # Dedicated news summary shortcut
            if any(k in stripped_lower for k in ["news summary", "daily news", "သတင်း အနှစ်ချုပ်", "သတင်းအနှစ်ချုပ်", "မြန်မာသတင်း အနှစ်ချုပ်"]):
                reply_text = await asyncio.to_thread(get_myanmar_news_summary)
                save_chat_message(message.from_user.id, "assistant", reply_text)
                await send_long_message(message, reply_text)
                return

            recent_ctx = await asyncio.to_thread(get_recent_history_for_router, message.from_user.id, 6)
            available_groups = await asyncio.to_thread(get_available_groups)
            user_pref_ctx = await asyncio.to_thread(get_user_profile, message.from_user.id)

            route = await asyncio.to_thread(
                smart_route_and_parse, user_message, available_groups, recent_ctx, user_pref_ctx
            )
            intent = route.get("intent", "GENERAL")
            print(f"[Smart Route] intent={intent}, mode={route.get('group_mode')}, action={route.get('task_action')}, group={route.get('group_name')}")

            if intent == "TASK":
                reply_text = await asyncio.to_thread(handle_task_intent, user_message, route)
            elif intent == "GROUP":
                if any(w in user_message for w in ["task မဟုတ်ဘူး", "မဟုတ်ဘူးလေ"]):
                    await asyncio.to_thread(delete_latest_pending_task)
                reply_text = await asyncio.to_thread(
                    handle_group_intent, message.from_user.id, user_message, route, recent_ctx
                )
            elif intent == "WEB_SEARCH":
                reply_text = await asyncio.to_thread(
                    answer_with_universal_search,
                    message.from_user.id,
                    user_message,
                    recent_ctx,
                    route.get("search_query"),
                )
            elif intent == "ADVICE":
                reply_text = await asyncio.to_thread(
                    handle_advice_or_general_intent, message.from_user.id, user_message, "ADVICE"
                )
            else:
                reply_text = await asyncio.to_thread(
                    handle_advice_or_general_intent, message.from_user.id, user_message, "GENERAL"
                )

            save_chat_message(message.from_user.id, "assistant", reply_text)
            asyncio.create_task(asyncio.to_thread(evolve_user_profile, message.from_user.id, user_message, reply_text))
            await send_long_message(message, reply_text)

        except Exception as e:
            await send_long_message(message, "လောလောဆယ် အဖြေပြန်ထုတ်ရာမှာ ခဏအခက်အခဲ ရှိနေပါတယ်ရှင်၊ နောက်မှ ထပ်မေးကြည့်ပါ")
            print(f"Error in handle_message: {e}")


async def main_async():
    threading.Thread(target=run_health_server, daemon=True).start()

    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    # Slash Commands
    application.add_handler(
        CommandHandler(
            ["start", "menu", "summary", "today", "yesterday", "groups", "news", "tasks", "addtask", "rules", "setrule"],
            handle_command,
        )
    )
    # Inline Button Clicks
    application.add_handler(CallbackQueryHandler(handle_button_callback))
    # Normal Messages
    application.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_message))

    # Scheduled Jobs (9:00 AM News, Due Reminders, 6:00 PM Pending Digest, 30s Follow-up Watcher)
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
        BotCommand("rules", "View locked core rules & versions"),
        BotCommand("setrule", "Add a new versioned core rule"),
    ])

    print("🤖 Maya AI Agent (Qwen 3.8 27B + Context Search + Confidence Tagging + Skill Versioning + Trusted News) started...")

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