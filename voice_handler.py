import asyncio
import os
import tempfile

import edge_tts
from telegram import Update
from telegram.ext import ContextTypes

from ai_engine import call_ai_with_3tier_fallback, gemini_client, groq_client
from config import ADMIN_USER_ID
from memory_manager import evolve_user_profile, recall_relevant_history, save_chat_message


async def transcribe_telegram_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
    voice = update.message.voice or update.message.audio
    if not voice:
        return ""

    tg_file = await context.bot.get_file(voice.file_id)
    suffix = ".ogg" if update.message.voice else ".mp3"

    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp_path = tmp.name

    try:
        await tg_file.download_to_drive(custom_path=tmp_path)

        try:
            with open(tmp_path, "rb") as audio_f:
                tr = groq_client.with_options(timeout=20.0).audio.transcriptions.create(
                    file=(os.path.basename(tmp_path), audio_f.read()),
                    model="whisper-large-v3-turbo",
                )
            txt = (getattr(tr, "text", "") or "").strip()
            if txt:
                return txt
        except Exception as g_err:
            print(f"[Groq Whisper Error]: {g_err}")

        try:
            with open(tmp_path, "rb") as f:
                audio_bytes = f.read()
            mime = "audio/ogg" if suffix == ".ogg" else "audio/mpeg"
            from google.genai import types
            resp = gemini_client.models.generate_content(
                model="gemini-3.5-flash-lite",
                contents=[
                    types.Part.from_bytes(data=audio_bytes, mime_type=mime),
                    "Transcribe this audio accurately into text (Burmese or English as spoken). Return ONLY the transcript.",
                ],
            )
            if resp and resp.text:
                return resp.text.strip()
        except Exception as gem_err:
            print(f"[Gemini Audio Transcribe Error]: {gem_err}")

        return ""
    finally:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass


async def synthesize_burmese_voice_mp3(text: str) -> str | None:
    clean_for_tts = (text or "").replace("*", "").replace("`", "").replace("#", "").strip()
    if not clean_for_tts:
        return None
    if len(clean_for_tts) > 700:
        clean_for_tts = clean_for_tts[:700]

    with tempfile.NamedTemporaryFile(delete=False, suffix=".mp3") as tmp:
        out_path = tmp.name

    for voice_name in ("my-MM-NilarNeural", "my-MM-ThihaNeural"):
        try:
            communicate = edge_tts.Communicate(text=clean_for_tts, voice=voice_name)
            await asyncio.wait_for(communicate.save(out_path), timeout=15.0)
            if os.path.exists(out_path) and os.path.getsize(out_path) > 200:
                return out_path
        except Exception as e:
            print(f"[Edge TTS {voice_name} Error]: {e}")

    if os.path.exists(out_path):
        try:
            os.remove(out_path)
        except Exception:
            pass
    return None


async def handle_voice_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or user.id != ADMIN_USER_ID:
        return

    status_msg = await update.message.reply_text("🎙️ အသံဖိုင်ကို နားထောင်ပြီး စာပြောင်းနေပါတယ်ရှင်...")
    transcript = await transcribe_telegram_voice(update, context)
    if not transcript:
        await status_msg.edit_text("အသံဖိုင်ကို ရှင်းရှင်းလင်းလင်း မကြားရလို့ ပြန်ပြောပေးပါဦးရှင်။")
        return

    save_chat_message(user.id, "user", f"[Voice]: {transcript}")
    memory_context = recall_relevant_history(user.id, transcript)
    prompt = f"""{memory_context}

User spoke via voice message: "{transcript}"

အထက်ပါ မှတ်တမ်းများနှင့်အညီ သင်ဇာ (Thinzar) ဟန်ဖြင့် သဘာဝကျကျ၊ ရှင်းရှင်းလင်းလင်း ဖြေဆိုပေးပါ။"""
    reply_text = call_ai_with_3tier_fallback(prompt, use_google_search=True)
    save_chat_message(user.id, "assistant", reply_text)
    evolve_user_profile(user.id, transcript, reply_text)

    await status_msg.edit_text(f"🗣️ **ကြားသိချက်:** _{transcript}_\n\n{reply_text}", parse_mode="Markdown")

    mp3_path = await synthesize_burmese_voice_mp3(reply_text)
    if mp3_path:
        try:
            with open(mp3_path, "rb") as audio_file:
                await update.message.reply_voice(voice=audio_file)
        except Exception as send_err:
            print(f"[Send Voice Error]: {send_err}")
        finally:
            if os.path.exists(mp3_path):
                try:
                    os.remove(mp3_path)
                except Exception:
                    pass