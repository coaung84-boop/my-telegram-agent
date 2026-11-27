import os
import re
import io
import asyncio
import tempfile
import edge_tts
from gtts import gTTS
from google.genai import types
from telegram import Update
from telegram.ext import ContextTypes

from config import ADMIN_USER_ID
from ai_engine import gemini_client, groq_client, call_ai_with_3tier_fallback
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
    return cleaned[:700]


def _resolve_audio_extension_and_mime(voice_obj, is_voice_note: bool) -> tuple[str, str]:
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
    return (".ogg", "audio/ogg") if is_voice_note else (".mp3", "audio/mpeg")


def transcribe_voice_bytes(audio_bytes: bytes, mime_type: str = "audio/ogg", filename: str = "voice.ogg") -> str:
    """Maya version ကဲ့သို့ Gemini audio understanding ကို ဦးစားပေးသုံးပြီး Groq Whisper fallback ထားသည်။"""
    if not audio_bytes:
        return ""

    audio_part = types.Part.from_bytes(data=bytes(audio_bytes), mime_type=mime_type or "audio/ogg")
    prompt = (
        "ဒီအသံဖိုင်ထဲမှာ ပြောထားတဲ့ စကားကို ကြားသည့်အတိုင်း တိတိကျကျ စာသား (Transcript) ပြန်ရေးပေးပါ။ "
        "မြန်မာစကားဆိုရင် မြန်မာစာ (Unicode) နဲ့ ရေးပြီး၊ အင်္ဂလိပ်စကားလုံး/ကိန်းဂဏန်း ပါရင်လည်း မှန်ကန်အောင် ထည့်ရေးပါ။ "
        "အပိုရှင်းလင်းချက် မထည့်ဘဲ ပြောတဲ့စကားကိုသာ တိုက်ရိုက် ထုတ်ပေးပါ။"
    )
    for model_name in ("gemini-3.5-flash-lite", "gemini-3.8-flash"):
        try:
            resp = gemini_client.models.generate_content(
                model=model_name,
                contents=[audio_part, prompt],
                config=types.GenerateContentConfig(temperature=0.0),
            )
            if resp and resp.text and resp.text.strip():
                return resp.text.strip()
        except Exception as e:
            print(f"[Voice STT {model_name} Error]: {e}")

    for model_name in ("whisper-large-v3-turbo", "whisper-large-v3"):
        try:
            bio = io.BytesIO(bytes(audio_bytes))
            bio.name = filename
            transcription = groq_client.with_options(timeout=25.0).audio.transcriptions.create(
                file=(filename, bio, mime_type or "audio/ogg"),
                model=model_name,
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
            print(f"[Voice Whisper {model_name} Error]: {e}")

    return ""


async def send_voice_reply(update: Update, context: ContextTypes.DEFAULT_TYPE, reply_text: str):
    """မြန်မာမိန်းကလေးအသံ (my-MM-NilarNeural) ဖြင့် Telegram voice note ပို့ပေးပြီး gTTS fallback ထားသည်။"""
    spoken = _clean_text_for_tts(reply_text)
    if not spoken:
        return

    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".mp3") as tmp:
            tmp_path = tmp.name

        saved_ok = False
        for _ in range(2):
            try:
                communicate = edge_tts.Communicate(text=spoken[:500], voice="my-MM-NilarNeural")
                await asyncio.wait_for(communicate.save(tmp_path), timeout=15.0)
                if os.path.exists(tmp_path) and os.path.getsize(tmp_path) > 0:
                    saved_ok = True
                    break
            except Exception:
                await asyncio.sleep(0.5)

        if not saved_ok:
            def _make_gtts():
                tts = gTTS(text=spoken[:500], lang="my", slow=False)
                tts.save(tmp_path)

            await asyncio.to_thread(_make_gtts)
            saved_ok = os.path.exists(tmp_path) and os.path.getsize(tmp_path) > 0

        if saved_ok:
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
    """User ပို့လိုက်သော voice/audio message ကို စာသားပြောင်းပြီး စာရော အသံပါ ပြန်ဖြေသည်။"""
    user_id = update.effective_user.id
    if user_id != ADMIN_USER_ID:
        return

    try:
        voice = update.message.voice or update.message.audio
        if not voice:
            return

        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="record_voice")
        ext, mime_type = _resolve_audio_extension_and_mime(voice, bool(update.message.voice))
        tg_file = await context.bot.get_file(voice.file_id)
        audio_bytes = bytes(await tg_file.download_as_bytearray())

        transcript_text = await asyncio.to_thread(
            transcribe_voice_bytes, audio_bytes, mime_type, f"voice{ext}"
        )
        if not transcript_text:
            await update.message.reply_text("အသံကို နားထောင်ရာမှာ ခဏပြတ်တောက်သွားပါတယ်ရှင်၊ ထပ်ပြောကြည့်ပေးပါ")
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

        await update.message.reply_text(f"🎙️ \"{transcript_text}\"\n\n{reply}")
        await send_voice_reply(update, context, reply)

    except Exception as e:
        print(f"Error handling voice message: {e}")
        await update.message.reply_text("အသံနဲ့ ပြန်ဖြေရာမှာ ခဏအခက်အခဲ ရှိနေပါတယ်ရှင်၊ နောက်မှ ထပ်စမ်းကြည့်ပါ")
