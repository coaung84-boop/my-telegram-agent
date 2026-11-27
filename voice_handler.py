import os
import re
import tempfile
import edge_tts
from telegram import Update
from telegram.ext import ContextTypes

from config import ADMIN_USER_ID
from ai_engine import groq_client, call_ai_with_3tier_fallback
from memory_manager import save_chat_message, recall_relevant_history, evolve_user_profile
from task_manager import (
    get_myanmar_calendar_context,
    smart_route_and_parse,
    handle_task_intent,
)
from group_news_search import get_available_group_names, handle_group_query, perform_hybrid_web_search


def _clean_text_for_tts(text: str) -> str:
    """Voice message အဖြစ် ဖတ်ပြရန် emoji၊ markdown symbols နဲ့ link တွေကို ရှင်းလင်းပေးသည်။"""
    if not text:
        return ""
    cleaned = re.sub(r"https?://\S+", "", text)
    cleaned = re.sub(r"\[[^\]]+\]\([^\)]+\)", "", cleaned)
    cleaned = re.sub(r"[*_`~#>\-]+", " ", cleaned)
    cleaned = re.sub(r"\[(Verified|Deduced|Unverified)\]", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(
        r"[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF]",
        "",
        cleaned,
    )
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned[:900]


def _resolve_audio_extension_and_mime(voice_obj) -> tuple[str, str]:
    """Telegram voice/audio object မှ file extension နှင့် mime_type ကို ခန့်မှန်းပေးသည်။"""
    fname = (getattr(voice_obj, "file_name", None) or "").lower()
    mime = (getattr(voice_obj, "mime_type", None) or "").lower()

    ext_map = {
        ".ogg": (".ogg", "audio/ogg"),
        ".oga": (".oga", "audio/ogg"),
        ".mp3": (".mp3", "audio/mpeg"),
        ".m4a": (".m4a", "audio/mp4"),
        ".mp4": (".mp4", "audio/mp4"),
        ".wav": (".wav", "audio/wav"),
        ".webm": (".webm", "audio/webm"),
    }
    for k, v in ext_map.items():
        if fname.endswith(k):
            return v

    if "mpeg" in mime or "mp3" in mime:
        return ".mp3", "audio/mpeg"
    if "mp4" in mime or "m4a" in mime or "aac" in mime:
        return ".m4a", "audio/mp4"
    if "wav" in mime:
        return ".wav", "audio/wav"
    if "webm" in mime:
        return ".webm", "audio/webm"
    return ".ogg", "audio/ogg"


def _transcribe_audio_file(audio_path: str, filename: str, mime_type: str) -> str:
    """Groq Whisper (primary + fallback model) ဖြင့် အသံဖိုင်ကို စာသားပြောင်းသည်။"""
    for model_name in ("whisper-large-v3-turbo", "whisper-large-v3"):
        try:
            with open(audio_path, "rb") as audio_file:
                transcription = groq_client.with_options(timeout=25.0).audio.transcriptions.create(
                    file=(filename, audio_file, mime_type),
                    model=model_name,
                    language="my",
                    response_format="json",
                )
            txt = (
                transcription
                if isinstance(transcription, str)
                else getattr(transcription, "text", "") or ""
            ).strip()
            if txt:
                return txt
        except Exception as e:
            print(f"Whisper transcription error ({model_name}): {e}")

    try:
        with open(audio_path, "rb") as f_raw:
            raw_bytes = f_raw.read()
        if raw_bytes:
            ai_txt = call_ai_with_3tier_fallback(
                "ဤအသံဖိုင်ထဲတွင် ပြောထားသော စကားကို စာသားအဖြစ် တိကျစွာ ဖတ်ပြပါ။ စာသားသက်သက်ကိုသာ ပြန်ဖြေပါ။",
                audio_bytes=raw_bytes,
                mime_type=mime_type,
                use_google_search=False,
            )
            if ai_txt and "ခဏအလုပ်များနေပါတယ်" not in ai_txt:
                return ai_txt.strip()
    except Exception as e:
        print(f"Fallback audio transcription error: {e}")

    return ""


async def send_voice_reply(update: Update, context: ContextTypes.DEFAULT_TYPE, reply_text: str):
    """မြန်မာမိန်းကလေးအသံ (my-MM-NilarNeural) ဖြင့် Telegram voice note ပို့ပေးသည်။"""
    spoken = _clean_text_for_tts(reply_text)
    if not spoken:
        return
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".mp3") as tmp:
            tmp_path = tmp.name
        communicate = edge_tts.Communicate(text=spoken, voice="my-MM-NilarNeural")
        await communicate.save(tmp_path)
        with open(tmp_path, "rb") as audio_f:
            await update.message.reply_voice(voice=audio_f)
    except Exception as e:
        print(f"TTS Voice error: {e}")
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass


async def handle_voice_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """User ပို့လိုက်သော voice message ကို စာသားပြောင်းပြီး စာရော အသံပါ ပြန်ဖြေသည်။"""
    user_id = update.effective_user.id
    if user_id != ADMIN_USER_ID:
        return

    audio_path = None
    try:
        voice = update.message.voice or update.message.audio
        if not voice:
            return

        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
        ext, mime_type = _resolve_audio_extension_and_mime(voice)
        tg_file = await context.bot.get_file(voice.file_id)
        with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as tmp:
            audio_path = tmp.name
        await tg_file.download_to_drive(audio_path)

        transcript_text = _transcribe_audio_file(audio_path, f"voice{ext}", mime_type)
        if not transcript_text:
            await update.message.reply_text("အသံဖိုင်ကို ရှင်းရှင်းလင်းလင်း ဖမ်းယူလို့ မရသေးပါဘူးရှင်၊ ထပ်ပြောပေးပါ")
            return

        save_chat_message(user_id, "user", f"[Voice]: {transcript_text}")

        history_ctx = recall_relevant_history(user_id, transcript_text, recent_limit=8, search_limit=8)
        available_groups = get_available_group_names()
        route = smart_route_and_parse(
            transcript_text,
            available_groups,
            recent_ctx=history_ctx[-1200:],
            user_pref_ctx=history_ctx[:900],
        )
        intent = route.get("intent", "GENERAL")

        if intent == "TASK":
            reply = handle_task_intent(transcript_text, route)
        elif intent == "GROUP":
            reply = handle_group_query(transcript_text, route)
        elif intent == "WEB_SEARCH":
            search_q = route.get("search_query") or transcript_text
            eng_q = route.get("english_search_query")
            reply = perform_hybrid_web_search(search_q, english_query=eng_q, history_ctx=history_ctx)
        else:
            cal_ctx = get_myanmar_calendar_context()
            prompt = f"""
အသုံးပြုသူက Voice message ဖြင့် ပြောကြားလာသည်ကို စာသားပြောင်းထားခြင်း ဖြစ်သည်။
Current Myanmar Calendar & Time:
{cal_ctx}

Memory & User Context:
{history_ctx}

User Voice Transcript: "{transcript_text}"

ညွှန်ကြားချက်များ:
- သင်ဇာ (Thinzar) အမည်ရှိ မြန်မာမိန်းကလေးဟန် AI Assistant အဖြစ် သဘာဝကျကျ၊ နွေးနွေးထွေးထွေး ဖြေဆိုပါ။
- အကြံဉာဏ်/ရွေးချယ်စရာ မေးလျှင် အကျိုးကျေးဇူး၊ အားနည်းချက်နှင့် လက်တွေ့အသုံးဝင်မည့် အကြံပြုချက်များ ရှင်းပြပါ။
- 'မောင်' ဟု ဆေးသောက်ရန်နှင့် မိသားစု/ကလေး Task များမှလွဲ၍ မသုံးပါနှင့်။
"""
            reply = call_ai_with_3tier_fallback(prompt)

        save_chat_message(user_id, "assistant", reply)
        evolve_user_profile(user_id, transcript_text, reply)

        await update.message.reply_text(reply)
        await send_voice_reply(update, context, reply)

    except Exception as e:
        print(f"Error handling voice message: {e}")
        await update.message.reply_text("Voice message ကို ဖြေဆိုရာမှာ ခဏအဆင်မပြေ ဖြစ်သွားပါတယ်ရှင်")
    finally:
        if audio_path and os.path.exists(audio_path):
            try:
                os.remove(audio_path)
            except Exception:
                pass
