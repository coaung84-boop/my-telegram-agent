import os
import re
import io
import asyncio
import tempfile
from datetime import datetime, timedelta, timezone
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

MM_TZ = timezone(timedelta(hours=6, minutes=30))


def _number_to_mm_words(n: int) -> str:
    units = {
        0: "သုည", 1: "တစ်", 2: "နှစ်", 3: "သုံး", 4: "လေး",
        5: "ငါး", 6: "ခြောက်", 7: "ခုနစ်", 8: "ရှစ်", 9: "ကိုး",
    }
    if n < 10:
        return units[n]
    if n == 10:
        return "တစ်ဆယ်"
    if n < 20:
        return f"ဆယ့်{units[n % 10]}"
    tens = {
        2: "နှစ်ဆယ်", 3: "သုံးဆယ်", 4: "လေးဆယ်", 5: "ငါးဆယ်",
        6: "ခြောက်ဆယ်", 7: "ခုနစ်ဆယ်", 8: "ရှစ်ဆယ်", 9: "ကိုးဆယ်",
    }
    rem = n % 10
    return f"{tens[n // 10]}{units[rem]}" if rem else tens[n // 10]


def _humanize_time_phrase(h: int, m: int) -> str:
    if 5 <= h < 12:
        period = "မနက် "
        disp_h = h
    elif h == 12:
        period = "နေ့လယ် "
        disp_h = 12
    elif 12 < h < 19:
        period = "ညနေ "
        disp_h = h - 12
    else:
        period = "ည "
        disp_h = h - 12 if h > 12 else h

    h_w = _number_to_mm_words(disp_h)
    if m == 0:
        return f"{period}{h_w}နာရီ"
    if m == 30:
        return f"{period}{h_w}နာရီခွဲ"
    return f"{period}{h_w}နာရီ {_number_to_mm_words(m)}မိနစ်"


def _humanize_dates_and_times_for_speech(text: str) -> str:
    """ISO date/time နဲ့ မြန်မာပြက္ခဒိန် အရှည်ကြီးတွေကို အရပ်သုံးစကားပြောပုံစံ ပြောင်းသည်။"""
    now_mm = datetime.now(MM_TZ)
    today_d = now_mm.date()
    tmr_d = (now_mm + timedelta(days=1)).date()

    def _repl_dt(match):
        y_str, m_str, d_str, h_str, min_str = match.groups()
        h, m = int(h_str), int(min_str)
        time_sp = _humanize_time_phrase(h, m)
        if y_str and m_str and d_str:
            try:
                d_obj = datetime(int(y_str), int(m_str), int(d_str)).date()
                if d_obj == today_d:
                    return f"ဒီနေ့ {time_sp}"
                if d_obj == tmr_d:
                    return f"မနက်ဖြန် {time_sp}"
                return f"{_number_to_mm_words(d_obj.day)}ရက်နေ့ {time_sp}"
            except Exception:
                return time_sp
        return time_sp

    text = re.sub(
        r"(?:\b(\d{4})-(\d{1,2})-(\d{1,2})\b\s+)?\b(\d{1,2}):(\d{2})\b",
        _repl_dt,
        text,
    )
    text = re.sub(
        r"\b(19|20)\d{2}-\d{1,2}-\d{1,2}\b",
        "",
        text,
    )
    text = re.sub(r"·\s*မြန်မာ\s*\d+\s*ခု.*$", "", text, flags=re.MULTILINE)
    text = re.sub(r"\(\s*(?:တနင်္လာနေ့|အင်္ဂါနေ့|ဗုဒ္ဓဟူးနေ့|ကြာသပတေးနေ့|သောကြာနေ့|စနေနေ့|တနင်္ဂနွေနေ့)\s*\)", "", text)
    return text


def _build_conversational_speech(reply_text: str) -> str:
    """စာသားအတိုင်း ခြောက်ကပ်ကပ် ရွတ်မယ့်အစား ရင်းရင်းနှီးနှီး ပွင့်ပွင့်လင်းလင်း စကားပြောသလို ပြင်သည်။"""
    if not reply_text:
        return ""

    raw = reply_text.strip()

    # Task မှတ်တဲ့ confirmation ဆိုရင် စာရှည်ကြီး မရွတ်ဘဲ သဘာဝကျကျ ပြောပြမယ်
    if "မှတ်ထားလိုက်ပါပြီ" in raw:
        m_title = re.search(r'"([^"]+)"\s*မှတ်ထားလိုက်ပါပြီ', raw)
        title_clean = (m_title.group(1) if m_title else "").strip()
        # အသံဖိုင် transcript မှားဖတ်ထားတဲ့ "er ပေးရန်" လို စာလုံးမျိုးကို သဘာဝကျကျ ညှိပေးမယ်
        if not title_clean or title_clean.lower() in {"er ပေးရန်", "reminder ပေးရန်", "remind ပေးရန်"}:
            return (
                "ဟုတ်ကဲ့၊ မှတ်ထားလိုက်ပါပြီနော်။ "
                "အချိန်ရောက်တာနဲ့ သင်ဇာ ပြန်သတိပေးလိုက်မယ်။ "
                "တခြား ဘာထပ်လုပ်စရာ ရှိသေးလဲ ပြောပါဦး။"
            )
        return (
            f"ဟုတ်ကဲ့၊ {title_clean} အတွက် မှတ်ထားလိုက်ပါပြီနော်။ "
            "အချိန်ရောက်ရင် သေချာ သတိပေးလိုက်မယ်။ "
            "နောက်ထပ် ဘာကူညီပေးရဦးမလဲရှင်။"
        )

    cleaned = re.sub(r"https?://\S+", "", raw)
    cleaned = re.sub(r"\[[^\]]+\]\([^\)]+\)", "", cleaned)
    cleaned = re.sub(r"\[(Verified|Deduced|Unverified)\]", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(
        r"[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF]",
        " ",
        cleaned,
    )
    cleaned = _humanize_dates_and_times_for_speech(cleaned)
    cleaned = re.sub(r"[*_`~#>\-]+", " ", cleaned)
    cleaned = re.sub(r"\n+", "။ ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()

    if len(cleaned) > 420:
        cut = cleaned[:420]
        last_p = max(cut.rfind("။"), cut.rfind("၊"), cut.rfind(" "))
        if last_p > 80:
            cut = cut[:last_p + 1]
        cleaned = f"{cut} ကျန်တဲ့ အသေးစိတ်ကိုတော့ စာနဲ့ ရေးပို့ပေးထားပါတယ်နော်။"

    return cleaned


def _resolve_audio_extension_and_mime(voice_obj, is_voice_note: bool) -> tuple[str, str]:
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
    """Gemini audio understanding ကို ဦးစားပေးသုံးပြီး Groq Whisper fallback ထားသည်။"""
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


async def _render_expressive_edge_tts(spoken_text: str, out_path: str) -> bool:
    """ဝါကျအပိုင်းလိုက် rate/pitch အနိမ့်အမြင့်နဲ့ pause တွေ ညှိပြီး လူပြောသလို ထုတ်ပေးသည်။"""
    # ။ နှင့် ၊ နေရာများတွင် အဖြတ်အတောက် ပါစေရန် ခွဲထုတ်သည်
    chunks = [c.strip() for c in re.split(r"(?<=[။၊,])\s*", spoken_text) if c.strip()]
    if not chunks:
        chunks = [spoken_text]

    out_buf = io.BytesIO()
    for idx, ch in enumerate(chunks):
        # ဝါကျအဆုံး (။) မှာ နည်းနည်းပိုနှေးပြီး ရပ်နား၊ ၊ မှာ အသံနည်းနည်းမြှင့်ပြီး ဆက်ပြော
        if ch.endswith("။"):
            rate = "+8%"
            pitch = "+2Hz"
            pause_sec = 0.22
        elif ch.endswith(("၊", ",")):
            rate = "+14%"
            pitch = "+6Hz"
            pause_sec = 0.10
        else:
            rate = "+16%"
            pitch = "+5Hz"
            pause_sec = 0.05

        communicate = edge_tts.Communicate(
            text=ch,
            voice="my-MM-NilarNeural",
            rate=rate,
            pitch=pitch,
        )
        async for ev in communicate.stream():
            if ev.get("type") == "audio" and ev.get("data"):
                out_buf.write(ev["data"])

        if idx < len(chunks) - 1 and pause_sec > 0:
            await asyncio.sleep(pause_sec)

    audio_data = out_buf.getvalue()
    if not audio_data:
        return False

    with open(out_path, "wb") as f:
        f.write(audio_data)
    return os.path.exists(out_path) and os.path.getsize(out_path) > 0


async def send_voice_reply(update: Update, context: ContextTypes.DEFAULT_TYPE, reply_text: str):
    """ရင်းရင်းနှီးနှီး လူပြောသလို အသံအနိမ့်အမြင့် အဖြတ်အတောက်နဲ့ Telegram voice note ပို့ပေးသည်။"""
    spoken = _build_conversational_speech(reply_text)
    if not spoken:
        return

    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".mp3") as tmp:
            tmp_path = tmp.name

        saved_ok = False
        for _ in range(2):
            try:
                saved_ok = await asyncio.wait_for(
                    _render_expressive_edge_tts(spoken, tmp_path),
                    timeout=18.0,
                )
                if saved_ok:
                    break
            except Exception:
                await asyncio.sleep(0.5)

        if not saved_ok:
            def _make_gtts():
                tts = gTTS(text=spoken.replace("။", " ").replace("၊", " "), lang="my", slow=False)
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
- သင်ဇာ (Thinzar) အမည်ရှိ မြန်မာမိန်းကလေးဟန် AI Assistant အဖြစ် ရင်းရင်းနှီးနှီး၊ ပွင့်ပွင့်လင်းလင်းနဲ့ လူတစ်ယောက် စကားပြောသလို သဘာဝကျကျ ဖြေဆိုပါ။
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
