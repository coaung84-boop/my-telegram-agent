import os
import logging
import threading
import asyncio
import requests
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


# --- Render Web Service အတွက် Dummy Health Check Server ---
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Telegram AI Agent with Gemini -> Groq -> OpenRouter Fallback is running!")


def run_health_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    server.serve_forever()


# --- 3-Tier AI Fallback Helper (Gemini Lite -> Groq Instant -> OpenRouter Auto-Free) ---
def call_ai_with_3tier_fallback(prompt_text, image_bytes=None, mime_type=None):
    errors = []

    # 1st Attempt: Gemini 3.5 Flash-Lite (503 High Demand အကင်းဆုံးနှင့် အမြန်ဆုံး)
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

    # 2nd Attempt: Groq (Llama 3.1 8B Instant - လက်ရှိ အလုပ်လုပ်နေသော Model)
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

    # 3rd Attempt: OpenRouter Auto-Free Router (Free ရသမျှ Model ကို အော်တိုရွေးပေးသည်)
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


# --- 2. Group Summary & Report Tools ---
def get_group_report() -> str:
    """Supabase ၏ raw_logs ထဲမှ မဖတ်ရသေးသော စာများကို စုစည်းပြီး အရေးကြီးသော အချက်များသာပါဝင်သော Report ကို ထုတ်ပေးသည်။"""
    logs_res = supabase.table("raw_logs").select("*").order("id").execute()
    logs = logs_res.data

    if not logs:
        return "လက်လွတ်စရာ မရှိပါ၊ Group ထဲတွင် အသစ်ဝင်ရောက်ထားသော အချက်အလက် မရှိသေးပါ။"

    raw_text = "\n".join([f"[{l['group_name']} | {l['sender_name']}]: {l['content_summary']}" for l in logs])

    prompt = f"""
    အောက်ပါတို့မှာ Telegram Group များမှ ရရှိထားသော မက်ဆေ့ခ်ျများနှင့် ပုံ/ဖိုင် အနှစ်ချုပ်များ ဖြစ်သည်။
    နေ့စဉ် တွေ့နေကျ ရိုးရိုးနှုတ်ဆက်စကားများ၊ အရေးမပါသော စကားပြောဆိုမှုများကို လုံးဝ ဖယ်ထုတ်ပါ။
    အောက်ပါ အချက်များကိုသာ ဦးစားပေး၍ အနှစ်ချုပ် Report တိုတိုရှင်းရှင်းနှင့် ထိရောက်စွာ ထုတ်ပေးပါ:
    1. ပုံမှန်မဟုတ်သော ပြဿနာများ (Issues / Blockers)
    2. ဆုံးဖြတ်ချက် အသစ်များနှင့် ညွှန်ကြားချက်များ
    3. အရေးကြီးသော လုပ်ငန်းတာဝန်များ (Action Items)

    ဒေတာများ:
    {raw_text}
    """

    report_text = call_ai_with_3tier_fallback(prompt)

    supabase.table("daily_reports").insert({"report_content": report_text}).execute()

    for l in logs:
        supabase.table("raw_logs").delete().eq("id", l["id"]).execute()

    return report_text


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
                "content_summary": content_summary
            }).execute()

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
            if "report" in user_message.lower() or "အနှစ်ချုပ်" in user_message:
                reply_text = get_group_report()
            elif "task" in user_message.lower() or "လုပ်စရာ" in user_message or "ထည့်" in user_message:
                add_task(user_message)
                reply_text = f"Task အသစ် ထည့်သွင်းပြီးပါပြီ: {user_message}"
            elif "စာရင်း" in user_message or "list" in user_message.lower():
                tasks = list_tasks()
                reply_text = "လက်ရှိ Task များ:\n" + "\n".join(
                    [f"- ID {t['id']}: {t['title']} ({t['status']})" for t in tasks]) if tasks else "Task မရှိသေးပါ။"
            else:
                system_prompt = "သင်သည် ကိုယ်ပိုင် အလုပ်အမှုဆောင် AI Assistant ဖြစ်သည်။ မြန်မာဘာသာဖြင့် တိုတိုရှင်းရှင်းနှင့် ထိရောက်စွာ ဖြေကြားပါ။\nသုံးစွဲသူ၏ မေးခွန်း: "
                reply_text = call_ai_with_3tier_fallback(system_prompt + user_message)

            await message.reply_text(reply_text)

        except Exception as e:
            await message.reply_text(f"မှားယွင်းမှု ဖြစ်ပေါ်သွားပါသည်: {str(e)}")


async def main_async():
    threading.Thread(target=run_health_server, daemon=True).start()

    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    application.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_message))

    print("🤖 Gemini -> Groq -> OpenRouter Telegram Bot စတင် အလုပ်လုပ်နေပါပြီ...")

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