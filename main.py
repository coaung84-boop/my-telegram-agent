import os
import logging
import threading
import asyncio
import re
import requests
from datetime import datetime, timedelta, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes
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

# မြန်မာစံတော်ချိန် (UTC+6:30) သတ်မှတ်ခြင်း
MM_TZ = timezone(timedelta(hours=6, minutes=30))
RETENTION_DAYS = 30  # raw_logs ကို သိမ်းဆည်းမည့် ရက်အရေအတွက် (တစ်လစာ)


# --- Render Web Service အတွက် Dummy Health Check Server ---
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Telegram AI Agent with Memory, Group Reports & 3-Tier Fallback is running!")


def run_health_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    server.serve_forever()


# --- Database Auto-Cleanup (ရက် ၃၀ ကျော်သော raw_logs များကို အော်တိုဖျက်ခြင်း) ---
def cleanup_old_raw_logs():
    """တစ်လ (ရက် ၃၀) ထက် ကျော်လွန်နေသော raw_logs မှတ်တမ်းအဟောင်းများကို ဖျက်ပေးသည်။"""
    try:
        cutoff_dt = datetime.now(MM_TZ) - timedelta(days=RETENTION_DAYS)
        supabase.table("raw_logs").delete().lt("created_at", cutoff_dt.isoformat()).execute()
    except Exception as e:
        print(f"Error cleaning up old raw_logs: {e}")


# --- Supabase Chat Memory Helpers (All History Saved + Smart Recall) ---
def save_chat_message(user_id: int, role: str, content: str):
    """ပြောခဲ့သမျှ စကားအားလုံးကို Supabase chat_messages table တွင် သိမ်းဆည်းသည်။"""
    try:
        supabase.table("chat_messages").insert({
            "user_id": user_id,
            "role": role,
            "content": content
        }).execute()
    except Exception as e:
        print(f"Error saving chat message: {e}")


def extract_search_terms(query_text: str) -> list:
    """Space ခြားထားသော စကားလုံးများအပြင် ရှည်လျားသော စာကြောင်းများမှလည်း အပိုင်းအစများ ထုတ်ယူသည်။"""
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
    """လတ်တလော မက်ဆေ့ခ်ျများအပြင် မေးခွန်းနှင့် သက်ဆိုင်သည့် အတိတ်မှတ်တမ်းများကို ပြန်ရှာပေးသည်။"""
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
            sections.append(f"--- သက်ဆိုင်ရာ အတိတ်မှတ်တမ်းဟောင်းများ ---\n{matched_text}")

        if recent_rows:
            recent_text = "\n".join([f"[{r['role']}]: {r['content']}" for r in recent_rows])
            sections.append(f"--- လတ်တလော ပြောဆိုမှုများ ---\n{recent_text}")

        return "\n\n".join(sections)
    except Exception as e:
        print(f"Error recalling chat history: {e}")
        return ""


# --- 3-Tier AI Fallback Helper (Gemini Flash-Lite -> Groq Instant -> OpenRouter Auto-Free) ---
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


# --- 1. Supabase Tools (Functions) for Personal Tasks ---
def add_task(title: str) -> dict:
    """Supabase database ထဲသို့ Task အသစ်တစ်ခု ထည့်သွင်းပေးသည်။"""
    supabase.table("tasks").insert({"title": title, "status": "pending"}).execute()
    return {"status": "success", "message": f"Task အသစ် ထည့်သွင်းပြီးပါပြီ: {title}"}


def list_tasks() -> list:
    """Supabase database ထဲတွင် ရှိနေသော Task အားလုံးကို စာရင်းထုတ်ကြည့်သည်။"""
    res = supabase.table("tasks").select("*").order("id").execute()
    return res.data


def update_task_status(task_id: int, new_status: str) -> dict:
    """Task တစ်ခု၏ အခြေအနေကို ပြင်ဆင်ပေးသည်။ ('completed', 'pending')"""
    supabase.table("tasks").update({"status": new_status}).eq("id", task_id).execute()
    return {"status": "success", "message": f"Task ID {task_id} ကို {new_status} သို့ ပြောင်းလဲပြီးပါပြီ။"}


# --- 2. Group Summary & Report Tools (By Group Name & Date within 30 Days) ---
def parse_date_range_from_text(text: str):
    """မေးခွန်းထဲမှ ဒီနေ့၊ မနေ့က သို့မဟုတ် YYYY-MM-DD ရက်စွဲကို ရှာဖွေပေးသည်။"""
    text_lower = text.lower()
    now_mm = datetime.now(MM_TZ)

    target_date = None
    label = None

    if "ဒီနေ့" in text_lower or "today" in text_lower:
        target_date = now_mm.date()
        label = f"ဒီနေ့ ({target_date})"
    elif "မနေ့" in text_lower or "yesterday" in text_lower:
        target_date = (now_mm - timedelta(days=1)).date()
        label = f"မနေ့က ({target_date})"
    else:
        match = re.search(r"\b(\d{4}-\d{2}-\d{2})\b", text_lower)
        if match:
            try:
                target_date = datetime.strptime(match.group(1), "%Y-%m-%d").date()
                label = f"{target_date}"
            except ValueError:
                pass

    if target_date:
        start_dt = datetime(target_date.year, target_date.month, target_date.day, 0, 0, 0, tzinfo=MM_TZ)
        end_dt = start_dt + timedelta(days=1)
        return start_dt.isoformat(), end_dt.isoformat(), label

    return None, None, None


def get_group_report(user_query: str) -> str:
    """Group နာမည်နှင့် ရက်စွဲ (သို့မဟုတ် မဖတ်ရသေးသော စာများ) အလိုက် Report ထုတ်ပေးသည်။"""
    # Report မထုတ်မီ ရက် ၃၀ ကျော်နေသော မှတ်တမ်းအဟောင်းများကို အရင်ရှင်းလင်းမည်
    cleanup_old_raw_logs()

    start_iso, end_iso, date_label = parse_date_range_from_text(user_query)

    # လွန်ခဲ့သော ရက် ၃၀ အတွင်း ဒေတာများကိုသာ ကန့်သတ်ဆွဲယူမည်
    thirty_days_ago_iso = (datetime.now(MM_TZ) - timedelta(days=RETENTION_DAYS)).isoformat()
    query = supabase.table("raw_logs").select("*").gte("created_at", thirty_days_ago_iso).order("id")

    if start_iso and end_iso:
        query = query.gte("created_at", start_iso).lt("created_at", end_iso)
    else:
        query = query.eq("is_read", False)

    logs_res = query.execute()
    logs = logs_res.data or []

    # Group နာမည်အလိုက် အော်တို စစ်ထုတ်ခြင်း
    matched_group_name = None
    if logs:
        all_groups = list({l.get("group_name") for l in logs if l.get("group_name")})
        q_lower = user_query.lower()
        for g_name in all_groups:
            g_words = [w for w in g_name.lower().split() if len(w) >= 3]
            if g_name.lower() in q_lower or any(w in q_lower for w in g_words):
                matched_group_name = g_name
                logs = [l for l in logs if l.get("group_name") == g_name]
                break

    if not logs:
        scope_desc = f" ({matched_group_name})" if matched_group_name else ""
        if date_label:
            return f"{date_label}{scope_desc} အတွက် သိမ်းဆည်းထားသော Group မက်ဆေ့ခ်ျ မှတ်တမ်း မရှိပါ။ (မှတ်ချက် - ရက် ၃၀ အတွင်း ဒေတာများကိုသာ ထိန်းသိမ်းထားပါသည်။)"
        return f"လက်ရှိ{scope_desc} တွင် မဖတ်ရသေးသော မက်ဆေ့ခ်ျအသစ် မရှိသေးပါ။"

    raw_text = "\n".join(
        [f"[{l['group_name']} | {l['sender_name']} | {str(l.get('created_at', ''))[:16]}]: {l['content_summary']}" for l in logs]
    )

    header_info = f"Group: {matched_group_name or 'All Groups'} | ကာလ: {date_label or 'မဖတ်ရသေးသော မက်ဆေ့ခ်ျအသစ်များ'}"

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

    # ရက်စွဲမပါဘဲ မဖတ်ရသေးတာကို ထုတ်ခဲ့ခြင်းဖြစ်လျှင် is_read = True ပြောင်းမှတ်ထားမည် (ရက် ၃၀ ပြည့်မှ အော်တိုဖျက်မည်)
    if not date_label:
        for l in logs:
            supabase.table("raw_logs").update({"is_read": True}).eq("id", l["id"]).execute()

    return f"📊 {header_info}\n\n{report_text}"


# --- 3. Telegram Message Handler ---
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message:
        return

    chat_type = message.chat.type
    sender_name = message.from_user.first_name if message.from_user else "Unknown"
    group_name = message.chat.title if chat_type in ["group", "supergroup"] else "Private"

    # CASE A: Group ထဲမှ မက်ဆေ့ခ်ျများ
    if chat_type in ["group", "supergroup"]:
        content_summary = ""

        if message.text:
            content_summary = message.text

        elif message.photo:
            photo_file = await message.photo[-1].get_file()
            photo_bytes = await photo_file.download_as_bytearray()
            img_prompt = "ဒီပုံထဲမှာ ပါဝင်တဲ့ အဓိက အချက်အလက်များ၊ စာသားများ သို့မဟုတ် လုပ်ငန်းဆိုင်ရာ အခြေအနေများကို အတိုချုံး ဖော်ပြပေးပါ။"
            img_summary = call_ai_with_3tier_fallback(img_prompt, image_bytes=photo_bytes, mime_type='image/jpeg')
            content_summary = f"[ပုံပါဖိုင်] {img_summary}"

        elif message.document:
            doc_file = await message.document.get_file()
            doc_bytes = await doc_file.download_as_bytearray()
            doc_prompt = "ဒီဖိုင်ထဲပါတဲ့ အဓိက အကြောင်းအရာနဲ့ အရေးကြီးတဲ့ အချက်တွေကို အနှစ်ချုပ်ပေးပါ။"
            doc_summary = call_ai_with_3tier_fallback(doc_prompt, image_bytes=doc_bytes,
                                                      mime_type=message.document.mime_type or 'application/pdf')
            content_summary = f"[စာရွက်စာတမ်း: {message.document.file_name}] {doc_summary}"

        if content_summary:
            supabase.table("raw_logs").insert({
                "group_name": group_name,
                "sender_name": sender_name,
                "content_summary": content_summary,
                "is_read": False
            }).execute()
            cleanup_old_raw_logs()

        return

    # CASE B: Private Chat မှ ခိုင်းစေချက်များ
    if chat_type == "private":
        if message.from_user.id != ADMIN_USER_ID:
            await message.reply_text("ခွင့်ပြုချက်မရှိပါ။ ဤ Bot သည် ကိုယ်ပိုင်အသုံးအတွက်သာ ဖြစ်ပါသည်။")
            return

        user_message = message.text
        if not user_message:
            return

        await context.bot.send_chat_action(chat_id=message.chat_id, action="typing")

        try:
            save_chat_message(message.from_user.id, "user", user_message)

            msg_lower = user_message.lower()
            if any(k in msg_lower for k in ["report", "summary", "အနှစ်ချုပ်", "အကျဉ်းချုပ်"]):
                reply_text = get_group_report(user_message)
            elif any(k in msg_lower for k in ["task", "လုပ်စရာ", "ထည့်"]):
                add_task(user_message)
                reply_text = f"Task အသစ် ထည့်သွင်းပြီးပါပြီ: {user_message}"
            elif any(k in msg_lower for k in ["စာရင်း", "list"]):
                tasks = list_tasks()
                reply_text = "လက်ရှိ Task များ:\n" + "\n".join(
                    [f"- ID {t['id']}: {t['title']} ({t['status']})" for t in tasks]) if tasks else "Task မရှိသေးပါ။"
            else:
                memory_context = recall_relevant_history(message.from_user.id, user_message)
                system_prompt = (
                    "သင်သည် ကိုယ်ပိုင် အလုပ်အမှုဆောင် AI Assistant ဖြစ်သည်။ "
                    "ပေးထားသော အတိတ်မှတ်တမ်းများနှင့် လတ်တလော ပြောဆိုမှုများကို ကိုးကားပြီး "
                    "မြန်မာဘာသာဖြင့် တိုတိုရှင်းရှင်းနှင့် ထိရောက်စွာ ဖြေကြားပါ။\n"
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
            await message.reply_text(f"မှားယွင်းမှု ဖြစ်ပေါ်သွားပါသည်: {str(e)}")


async def main_async():
    threading.Thread(target=run_health_server, daemon=True).start()

    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    application.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_message))

    print("🤖 AI Agent with Memory, Group Reports (30-Day Retention) & 3-Tier Fallback စတင် အလုပ်လုပ်နေပါပြီ...")

    await application.initialize()
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