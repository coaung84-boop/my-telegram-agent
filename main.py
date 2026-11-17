import os
import logging
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes
from google import genai
from google.genai import types
from database import supabase
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)

gemini_client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ADMIN_USER_ID = int(os.getenv("MY_TELEGRAM_USER_ID", "0"))


# --- Render Web Service အတွက် Dummy Health Check Server ---
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Telegram AI Agent is running!")


def run_health_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    server.serve_forever()


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

    response = gemini_client.models.generate_content(
        model="gemini-3.8-flash",
        contents=prompt
    )

    report_text = response.text
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
            img_res = gemini_client.models.generate_content(
                model='gemini-3.8-flash',
                contents=[
                    types.Part.from_bytes(data=bytes(photo_bytes), mime_type='image/jpeg'),
                    "ဒီပုံထဲမှာ ပါဝင်တဲ့ အဓိက အချက်အလက်များ၊ စာသားများ သို့မဟုတ် လုပ်ငန်းဆိုင်ရာ အခြေအနေများကို အတိုချုံး ဖော်ပြပေးပါ။"
                ]
            )
            content_summary = f"[ပုံပါဖိုင်] {img_res.text}"

        elif message.document:
            doc_file = await message.document.get_file()
            doc_bytes = await doc_file.download_as_bytearray()
            doc_res = gemini_client.models.generate_content(
                model='gemini-3.8-flash',
                contents=[
                    types.Part.from_bytes(data=bytes(doc_bytes),
                                          mime_type=message.document.mime_type or 'application/pdf'),
                    "ဒီဖိုင်ထဲပါတဲ့ အဓိက အကြောင်းအရာနဲ့ အရေးကြီးတဲ့ အချက်တွေကို အနှစ်ချုပ်ပေးပါ။"
                ]
            )
            content_summary = f"[စာရွက်စာတမ်း: {message.document.file_name}] {doc_res.text}"

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
            agent_tools = [add_task, list_tasks, update_task_status, get_group_report]
            system_instruction = """
            သင်သည် ကိုယ်ပိုင် အလုပ်အမှုဆောင်နှင့် လုပ်ငန်းသုံး Group Report များကို စီမံပေးသော AI Agent ဖြစ်သည်။
            - Personal Task များနှင့် ပတ်သက်လျှင် add_task, list_tasks, update_task_status များကို သုံးပါ။
            - Group Report တောင်းပါက get_group_report tool ကို ခေါ်သုံးပါ။
            - အဖြေများကို မြန်မာဘာသာဖြင့် တိုတိုရှင်းရှင်းနှင့် ထိရောက်စွာ ဖြေကြားပါ။
            """

            response = gemini_client.models.generate_content(
                model="gemini-3.8-flash",
                contents=user_message,
                config=types.GenerateContentConfig(
                    system_instruction=system_instruction,
                    tools=agent_tools,
                    temperature=0.1,
                ),
            )

            reply_text = response.text if response.text else "လုပ်ဆောင်မှု ပြီးစီးပါပြီ။"
            await message.reply_text(reply_text)

        except Exception as e:
            await message.reply_text(f"မှားယွင်းမှု ဖြစ်ပေါ်သွားပါသည်: {str(e)}")


def main():
    # Render အတွက် Background မှာ Health Check Server ကို ဖွင့်ထားခြင်း
    threading.Thread(target=run_health_server, daemon=True).start()

    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    application.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_message))

    print("🤖 All-in-One Telegram AI Agent စတင် အလုပ်လုပ်နေပါပြီ...")
    application.run_polling()


if __name__ == "__main__":
    main()