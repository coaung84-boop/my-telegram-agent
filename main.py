import os
import logging
import threading
import asyncio
import re
import urllib.parse
import requests
import xml.etree.ElementTree as ET
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

# AI Clients Initialization
gemini_client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
groq_client = Groq(api_key=os.getenv("GROQ_API_KEY"))
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")

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
        self.wfile.write(b"Telegram AI Agent with Scheduler & News Summary is running!")


def run_health_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    server.serve_forever()


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
        for i in range(0, min(len(compact), 40), 4):
            chunk = compact[i:i + 5]
            if len(chunk) >= 3:
                terms.append(chunk)

    unique_terms = []
    for t in terms:
        if t not in unique_terms:
            unique_terms.append(t)
    return unique_terms[:8]


def recall_relevant_history(user_id: int, query_text: str, recent_limit: int = 12, search_limit: int = 10) -> str:
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
            matched_text = "\n".join([f"[{m['role']}]: {m['content']}" for m in matched_rows[-15:]])
            sections.append(f"--- Relevant Past History ---\n{matched_text}")

        if recent_rows:
            recent_text = "\n".join([f"[{r['role']}]: {r['content']}" for r in recent_rows])
            sections.append(f"--- Recent Conversation ---\n{recent_text}")

        return "\n\n".join(sections)
    except Exception as e:
        print(f"Error recalling chat history: {e}")
        return ""


# --- 3-Tier AI Fallback Helper (Gemini 3.5 Flash-Lite -> Groq -> OpenRouter) ---
def call_ai_with_3tier_fallback(prompt_text, image_bytes=None, mime_type=None):
    errors = []

    # 1st Attempt: Gemini 3.5 Flash-Lite
    try:
        if image_bytes:
            contents = [
                types.Part.from_bytes(data=bytes(image_bytes), mime_type=mime_type or 'image/jpeg'),
                prompt_text
            ]
        else:
            contents = prompt_text

        response = gemini_client.models.generate_content(
            model="gemini-3.5-flash-lite",
            contents=contents
        )
        if response and response.text:
            return response.text
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
        if completion and completion.choices[0].message.content:
            return completion.choices[0].message.content
    except Exception as e:
        errors.append(f"2. Groq Error: {str(e)}")

    # 3rd Attempt: OpenRouter Auto-Free Router
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
        response = requests.post("https://openrouter.ai/api/v1/chat/completions", headers=headers, json=data, timeout=20)
        res_json = response.json()
        if "choices" in res_json and len(res_json["choices"]) > 0:
            return res_json["choices"][0]["message"]["content"]
        else:
            errors.append(f"3. OpenRouter Response: {res_json}")
    except Exception as e:
        errors.append(f"3. OpenRouter Error: {str(e)}")

    return "API ၃ ခုစလုံး အလုပ်မလုပ်ပါ:\n\n" + "\n\n".join(errors)


# --- Myanmar News Fetcher & Summarizer (via Google News RSS + Built-in ElementTree) ---
def get_myanmar_news_summary() -> str:
    """Google News RSS XML မှ မြန်မာပြည် လတ်တလော သတင်းခေါင်းစဉ်များကို ElementTree ဖြင့် ဆွဲယူပြီး အနှစ်ချုပ်ပေးသည်။"""
    queries = ["Myanmar news", "မြန်မာ သတင်း", "Yangon news"]
    collected_items = []
    seen_titles = set()

    for q in queries:
        try:
            encoded_q = urllib.parse.quote(q)
            url = f"https://news.google.com/rss/search?q={encoded_q}&hl=en-US&gl=MM&ceid=MM:en"
            resp = requests.get(url, timeout=15)
            resp.raise_for_status()

            root = ET.fromstring(resp.content)
            for item in root.findall(".//channel/item")[:8]:
                title_el = item.find("title")
                pub_el = item.find("pubDate")

                title = (title_el.text or "").strip() if title_el is not None and title_el.text else ""
                published = (pub_el.text or "").strip() if pub_el is not None and pub_el.text else ""

                if title and title not in seen_titles:
                    seen_titles.add(title)
                    collected_items.append(f"- {title} ({published})")
        except Exception as e:
            print(f"News fetch error for '{q}': {e}")

    if not collected_items:
        return "လတ်တလော အွန်လိုင်းသတင်းများကို ဆွဲယူ၍ မရသေးပါ။ နောက်မှ ထပ်မံကြိုးစားကြည့်ပါ။"

    news_raw = "\n".join(collected_items[:20])
    now_str = datetime.now(MM_TZ).strftime("%Y-%m-%d %H:%M MMT")

    prompt = f"""
    အောက်ပါတို့မှာ အွန်လိုင်းသတင်းများမှ ရရှိထားသော မြန်မာပြည်နှင့် သက်ဆိုင်သည့် လတ်တလော သတင်းခေါင်းစဉ်များ ဖြစ်သည်။ ({now_str})
    ထပ်နေသော သတင်းများကို ပေါင်းစပ်ပြီး အရေးပါပြီး စိတ်ဝင်စားဖွယ် အကောင်းဆုံး သတင်းအချက်အလက်များကို
    မြန်မာဘာသာဖြင့် ရှင်းလင်းပြတ်သားသော Bullet point များဖြင့် အနှစ်ချုပ် တင်ပြပေးပါ။
    (အချက်အလက် မှန်ကန်မှုကိုသာ အခြေခံပြီး ထပ်မံမချဲ့ကားပါနှင့်။)

    သတင်းစာရင်း:
    {news_raw}
    """

    summary = call_ai_with_3tier_fallback(prompt)
    return f"📰 Myanmar News Summary ({now_str})\n\n{summary}"


# --- Scheduled Daily 9:00 AM MMT Job ---
async def scheduled_morning_news_job(context: ContextTypes.DEFAULT_TYPE):
    """နေ့စဉ် မနက် ၉:၀၀ နာရီ (မြန်မာစံတော်ချိန်) တွင် ADMIN_USER_ID ထံ သတင်းအချုပ် ပို့ပေးသည်။"""
    if not ADMIN_USER_ID:
        return
    try:
        news_text = await asyncio.to_thread(get_myanmar_news_summary)
        save_chat_message(ADMIN_USER_ID, "assistant", news_text)
        await context.bot.send_message(chat_id=ADMIN_USER_ID, text=news_text)
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
    """day_mode: 'today' or 'yesterday'"""
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

    report_text = call_ai_with_3tier_fallback(prompt)

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
        await message.reply_text(reply_text)
    elif cmd == "/today":
        reply_text = get_group_report("today summary", day_mode="today")
        await message.reply_text(reply_text)
    elif cmd == "/yesterday":
        reply_text = get_group_report("yesterday summary", day_mode="yesterday")
        await message.reply_text(reply_text)
    elif cmd == "/groups":
        text, markup = build_groups_keyboard()
        await message.reply_text(text, reply_markup=markup)
    elif cmd == "/news":
        reply_text = await asyncio.to_thread(get_myanmar_news_summary)
        await message.reply_text(reply_text)
    elif cmd == "/tasks":
        await message.reply_text(get_formatted_tasks())


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

    await query.message.reply_text(reply_text)


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
            elif not is_question and any(k in msg_lower for k in ["report", "summary", "အနှစ်ချုပ်", "အကျဉ်းချုပ်"]) and "သတင်း" not in msg_lower and "news" not in msg_lower:
                reply_text = get_group_report(user_message)
            elif not is_question and (msg_lower.startswith("task:") or msg_lower.startswith("လုပ်စရာ:") or "task မှတ်ထား" in msg_lower or "task ထည့်" in msg_lower):
                clean_task = re.sub(r"^(task:|လုပ်စရာ:)\s*", "", user_message, flags=re.IGNORECASE).strip()
                add_task(clean_task)
                reply_text = f"Task added: {clean_task}"
            elif msg_lower in ["tasks", "task list", "task စာရင်း", "လုပ်စရာစာရင်း"]:
                reply_text = get_formatted_tasks()
            elif not is_question and ("သတင်း" in msg_lower or "news" in msg_lower) and ("အချုပ်" in msg_lower or "summary" in msg_lower or "ရှာ" in msg_lower or "တင်" in msg_lower):
                reply_text = await asyncio.to_thread(get_myanmar_news_summary)
            else:
                memory_context = recall_relevant_history(message.from_user.id, user_message)
                system_prompt = (
                    "သင်သည် မာယာ (Maya) အမည်ရှိ ကိုယ်ပိုင် အလုပ်အမှုဆောင် AI Assistant ဖြစ်သည်။ "
                    "ပေးထားသော အတိတ်မှတ်တမ်းများနှင့် လတ်တလော ပြောဆိုမှုများကို ကိုးကားပြီး မြန်မာဘာသာဖြင့် တိုတိုရှင်းရှင်း ဖြေကြားပါ။ "
                    "အရေးကြီးချက် - နေ့စဉ် မနက် ၉:၀၀ နာရီ (မြန်မာစံတော်ချိန်) တွင် အွန်လိုင်းမြန်မာသတင်း အနှစ်ချုပ်ကို "
                    "အလိုအလျောက် ပို့ပေးမည့် Scheduler စနစ် ပါဝင်ပြီး ဖြစ်သော်လည်း သီးခြားစိတ်ကြိုက် အချိန်အလိုက် ခိုင်းစေချက်များ သို့မဟုတ် "
                    "Facebook Private Account များကို တိုက်ရိုက် ဝင်ကြည့်ခြင်းများကို မလုပ်ဆောင်နိုင်ပါ။ "
                    "မလုပ်နိုင်သေးသည့် အရာများကို မမှန်မကန် ကတိမပေးဘဲ ရိုးသားစွာ ရှင်းပြပါ။ "
                    "Task မှတ်လိုပါက /addtask ဖြင့် မှတ်နိုင်ကြောင်း အကြံပြုပါ။\n"
                )
                full_prompt = (
                    f"{system_prompt}\n"
                    f"{memory_context}\n\n"
                    f"လက်ရှိ မေးခွန်း/စကား: {user_message}"
                )
                reply_text = call_ai_with_3tier_fallback(full_prompt)

            save_chat_message(message.from_user.id, "assistant", reply_text)

            await message.reply_text(reply_text)

        except Exception as e:
            await message.reply_text(f"Error: {str(e)}")


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

    # နေ့စဉ် မနက် ၉:၀၀ နာရီ (UTC+6:30) တွင် သတင်းအချုပ် အလိုအလျောက် ပို့ပေးမည့် Scheduler
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

    print("🤖 AI Agent with Scheduler, News & Memory started...")

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