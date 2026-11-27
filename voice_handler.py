import os
import re
import io
import json
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

# Per-user voice tone/intent profile & pending voice clarification state
_VOICE_PROFILES: dict[int, list[dict]] = {}
_PENDING_VOICE_CLARIFICATION: dict[int, dict] = {}


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
    text = re.sub(r"\b(19|20)\d{2}-\d{1,2}-\d{1,2}\b", "", text)
    text = re.sub(r"·\s*မြန်မာ\s*\d+\s*ခု.*$", "", text, flags=re.MULTILINE)
    text = re.sub(
        r"\(\s*(?:တနင်္လာနေ့|အင်္ဂါနေ့|ဗုဒ္ဓဟူးနေ့|ကြာသပတေးနေ့|သောကြာနေ့|စနေနေ့|တနင်္ဂနွေနေ့)\s*\)",
        "",
        text,
    )
    return text


def _to_spoken_chat_prose(raw_text: str) -> str:
    text = re.sub(r"https?://\S+", "", raw_text)
    text = re.sub(r"\[[^\]]+\]\([^\)]+\)", "", text)
    text = re.sub(r"\[(Verified|Deduced|Unverified)\]", "", text, flags=re.IGNORECASE)
    text = re.sub(
        r"[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF]",
        " ",
        text,
    )
    text = _humanize_dates_and_times_for_speech(text)

    text = re.sub(r"(?im)^\s*[-•*]+\s*key\s*point\s*:\s*", "အဓိက ကတော့ ", text)
    text = re.sub(r"(?im)^\s*အကြံပြုချက်\s*:\s*", "သင်ဇာ့အနေနဲ့ ပြောရရင်တော့ ", text)
    text = re.sub(r"(?im)^\s*\d+[\.\)]\s*", "", text)
    text = re.sub(r"(?m)^\s*[-•*]+\s*", "", text)
    text = re.sub(r"[*_`~#>\-]+", " ", text)

    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    spoken_lines: list[str] = []
    for ln in lines:
        ln = re.sub(r"\s*:\s*", " ဆိုတာကတော့ ", ln)
        ln = re.sub(r"\s+", " ", ln).strip()
        if not ln:
            continue
        if not ln.endswith(("။", "၊", "!", "?")):
            ln = f"{ln}။"
        spoken_lines.append(ln)

    combined = " ".join(spoken_lines)
    combined = re.sub(r"\s+", " ", combined).strip()

    if len(combined) > 380:
        cut = combined[:380]
        last_p = max(cut.rfind("။"), cut.rfind("၊"), cut.rfind(" "))
        if last_p > 70:
            cut = cut[: last_p + 1]
        combined = f"{cut} ကျန်တဲ့ အသေးစိတ်ကိုတော့ စာထဲမှာ အပြည့်အစုံ ရေးပေးထားတယ်နော်။"

    return combined


def _build_conversational_speech(reply_text: str) -> str:
    if not reply_text:
        return ""

    raw = reply_text.strip()

    if "မှတ်ထားလိုက်ပါပြီ" in raw:
        m_title = re.search(r'"([^"]+)"\s*မှတ်ထားလိုက်ပါပြီ', raw)
        title_clean = (m_title.group(1) if m_title else "").strip()
        if not title_clean or title_clean.lower() in {"er ပေးရန်", "reminder ပေးရန်", "remind ပေးရန်"}:
            return (
                "အင်း၊ မှတ်ထားလိုက်ပြီနော်။ "
                "အချိန်ရောက်တာနဲ့ သင်ဇာ လှမ်းသတိပေးလိုက်မယ်။ "
                "တခြား ဘာရှိသေးလဲ ပြောပါဦး။"
            )
        return (
            f"အင်း၊ {title_clean} ကို မှတ်ထားလိုက်ပြီနော်။ "
            "အချိန်ရောက်ရင် သေချာ လှမ်းသတိပေးလိုက်မယ်။ "
            "နောက်ထပ် ဘာလုပ်စရာ ရှိသေးလဲ ပြောပါဦး။"
        )

    return _to_spoken_chat_prose(raw)


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


def transcribe_and_analyze_voice(
    audio_bytes: bytes,
    mime_type: str = "audio/ogg",
    filename: str = "voice.ogg",
    voice_profile_ctx: str = "",
) -> dict:
    transcript = ""
    tone_label = "သဘာဝကျကျ/ပုံမှန်"
    intent_hint = "GENERAL"
    ambiguous = False
    clarification_q = ""
    interpreted_meaning = ""

    if audio_bytes:
        audio_part = types.Part.from_bytes(data=bytes(audio_bytes), mime_type=mime_type or "audio/ogg")
        prompt = f"""
နားထောင်ပြီး ဒီအသံဖိုင်ကို ခွဲခြမ်းစိတ်ဖြာပေးပါ။
အရင် Voice Tone & Meaning Profile:
{voice_profile_ctx or "မှတ်တမ်းမရှိသေးပါ"}

Return ONLY valid JSON:
{{
  "transcript": "ပြောထားတဲ့စကားကို မြန်မာ (Unicode) / English မှန်ကန်အောင် စာပြန်ရေးထားချက်",
  "tone": "အသံနေအသံထား (ဥပမာ — အလောတကြီး/စိတ်စောနေတာ၊ အေးအေးဆေးဆေး/စဉ်းစားနေတာ၊ သေချာမေးမြန်းတာ၊ တိုတိုတုတ်တုတ် ခိုင်းတာ)",
  "intent_hint": "TASK | GROUP | WEB_SEARCH | GENERAL",
  "interpreted_meaning": "ဒီလေသံနဲ့ ဒီစကားရဲ့ အဓိက ဆိုလိုရင်းကို တိုတိုရှင်းရှင်း ဖော်ပြချက်",
  "ambiguous": true or false,
  "clarification_question": "ambiguous ဖြစ်ရင် 'ဒါကို ဆိုလိုတာလားရှင်' လို့ သဘာဝကျကျ မေးတဲ့ မေးခွန်းတို"
}}

Rules:
- transcript မှာ ကြားသည့်အတိုင်း တိတိကျကျ ရေးပါ။
- အသံပြတ်တောက်တာ၊ စကားမပြည့်စုံတာ၊ ဒါမှမဟုတ် အဓိပ္ပာယ် နှစ်မျိုးဖြစ်နိုင်ရင် ambiguous=true ထားပြီး clarification_question မှာ 'ဒါကို ဆိုလိုတာလားရှင်' ဆိုတဲ့ ဟန်နဲ့ တိုတိုရှင်းရှင်း မေးပါ။
- အရင်က အလားတူ လေသံ/အခြေအနေမျိုးမှာ ဘာကို ဆိုလိုလေ့ရှိလဲ ဆိုတာကိုလည်း ထည့်စဉ်းစားပါ။
"""
        for model_name in ("gemini-3.5-flash-lite", "gemini-3.8-flash"):
            try:
                resp = gemini_client.models.generate_content(
                    model=model_name,
                    contents=[audio_part, prompt],
                    config=types.GenerateContentConfig(temperature=0.1),
                )
                raw = (resp.text or "").strip() if resp else ""
                m = re.search(r"\{.*\}", raw, re.DOTALL)
                if m:
                    parsed = json.loads(m.group(0))
                    if isinstance(parsed, dict) and parsed.get("transcript"):
                        return {
                            "transcript": str(parsed["transcript"]).strip(),
                            "tone": str(parsed.get("tone") or tone_label).strip(),
                            "intent_hint": str(parsed.get("intent_hint") or intent_hint).strip().upper(),
                            "interpreted_meaning": str(parsed.get("interpreted_meaning") or "").strip(),
                            "ambiguous": bool(parsed.get("ambiguous", False)),
                            "clarification_question": str(parsed.get("clarification_question") or "").strip(),
                        }
                if raw and not raw.startswith("{"):
                    transcript = raw
                    break
            except Exception as e:
                print(f"[Voice Analysis {model_name} Error]: {e}")

    if not transcript and audio_bytes:
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
                    transcript = txt
                    break
            except Exception as e:
                print(f"[Voice Whisper {model_name} Error]: {e}")

    return {
        "transcript": transcript,
        "tone": tone_label,
        "intent_hint": intent_hint,
        "interpreted_meaning": interpreted_meaning or transcript,
        "ambiguous": ambiguous,
        "clarification_question": clarification_q,
    }


def _record_voice_profile(user_id: int, transcript: str, tone: str, meaning: str):
    now_str = datetime.now(MM_TZ).strftime("%m-%d %H:%M")
    entry = {
        "time": now_str,
        "tone": tone,
        "meaning": meaning or transcript[:80],
        "sample": transcript[:80],
    }
    hist = _VOICE_PROFILES.setdefault(user_id, [])
    hist.append(entry)
    if len(hist) > 12:
        del hist[:-12]

    try:
        evolve_user_profile(
            user_id,
            f"[Voice Tone: {tone}] {transcript}",
            f"[Voice Meaning Profile]: လေသံ '{tone}' ဖြင့် ပြောခဲ့သော ဆိုလိုရင်းမှာ '{meaning or transcript[:80]}' ဖြစ်သည်။",
        )
    except Exception:
        pass


def _get_voice_profile_summary(user_id: int) -> str:
    hist = _VOICE_PROFILES.get(user_id) or []
    if not hist:
        return ""
    lines = [
        f"- [{item['time']}] လေသံ: {item['tone']} | ဆိုလိုရင်း: {item['meaning']} (စကား: \"{item['sample']}\")"
        for item in hist[-6:]
    ]
    return "\n".join(lines)
  
def _split_into_expressive_clauses(spoken_text: str) -> list[tuple[str, str]]:
    raw_parts = [p.strip() for p in re.split(r"(?<=[။၊,!?])\s*", spoken_text) if p.strip()]
    clauses: list[tuple[str, str]] = []

    for part in raw_parts:
        ends_period = part.endswith(("။", "!", "?"))
        ends_comma = part.endswith(("၊", ","))
        core = re.sub(r"[။၊,!?]+$", "", part).strip()
        if not core:
            continue

        words = core.split()
        if len(words) >= 7:
            mid = len(words) // 2
            left = " ".join(words[:mid]).strip()
            right = " ".join(words[mid:]).strip()
            if left:
                clauses.append((left, "lift"))
            if right:
                clauses.append((right, "settle" if ends_period else "lift"))
        else:
            clauses.append((core, "settle" if ends_period else ("lift" if ends_comma else "mid")))

    return clauses or [(spoken_text, "mid")]


def _split_burmese_and_english_segments(text: str) -> list[tuple[str, str]]:
    parts = re.findall(r"[A-Za-z0-9][A-Za-z0-9\s\.\-\/]*[A-Za-z0-9]|[A-Za-z]+|[^A-Za-z]+", text)
    segments: list[tuple[str, str]] = []
    for p in parts:
        cleaned = p.strip()
        if not cleaned:
            continue
        if re.fullmatch(r"[A-Za-z0-9\s\.\-\/]+", cleaned) and re.search(r"[A-Za-z]", cleaned):
            segments.append(("en", cleaned))
        else:
            segments.append(("mm", cleaned))
    return segments


async def _render_expressive_edge_tts(spoken_text: str, out_path: str) -> bool:
    clauses = _split_into_expressive_clauses(spoken_text)

    lift_cycle = [
        ("+12Hz", "+24%", 0.06),
        ("+14Hz", "+27%", 0.05),
        ("+11Hz", "+23%", 0.09),
    ]

    out_buf = io.BytesIO()
    for idx, (clause_txt, role) in enumerate(clauses):
        if role == "settle":
            mm_pitch, mm_rate, mm_pause = "+10Hz", "+18%", 0.20
        elif role == "lift":
            mm_pitch, mm_rate, mm_pause = "+14Hz", "+25%", 0.10
        else:
            mm_pitch, mm_rate, mm_pause = lift_cycle[idx % len(lift_cycle)]

        sub_segs = _split_burmese_and_english_segments(clause_txt)
        for s_idx, (kind, seg_txt) in enumerate(sub_segs):
            if kind == "en":
                if s_idx > 0:
                    await asyncio.sleep(0.14)
                communicate = edge_tts.Communicate(
                    text=seg_txt,
                    voice="my-MM-NilarNeural",
                    rate="+2%",
                    pitch="+11Hz",
                )
                async for ev in communicate.stream():
                    if ev.get("type") == "audio" and ev.get("data"):
                        out_buf.write(ev["data"])
                await asyncio.sleep(0.14)
            else:
                communicate = edge_tts.Communicate(
                    text=seg_txt,
                    voice="my-MM-NilarNeural",
                    rate=mm_rate,
                    pitch=mm_pitch,
                )
                async for ev in communicate.stream():
                    if ev.get("type") == "audio" and ev.get("data"):
                        out_buf.write(ev["data"])

        if idx < len(clauses) - 1 and mm_pause > 0:
            await asyncio.sleep(mm_pause)

    audio_data = out_buf.getvalue()
    if not audio_data:
        return False

    with open(out_path, "wb") as f:
        f.write(audio_data)
    return os.path.exists(out_path) and os.path.getsize(out_path) > 0


async def send_voice_reply(update: Update, context: ContextTypes.DEFAULT_TYPE, reply_text: str):
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
                    timeout=20.0,
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


def _resolve_pending_clarification(user_id: int, transcript_text: str) -> str | None:
    pending = _PENDING_VOICE_CLARIFICATION.get(user_id)
    if not pending:
        return None

    low = transcript_text.strip().lower()
    yes_tokens = ["ဟုတ်", "ဟုတ်တယ်", "ဟုတ်ကဲ့", "အင်း", "အဲဒါပဲ", "မှန်တယ်", "yes", "yeah", "yep", "ok", "okay"]
    no_tokens = ["မဟုတ်", "မဟုတ်ဘူး", "မှားတယ်", "no", "nope", "wrong"]

    if any(tok in low for tok in yes_tokens):
        _PENDING_VOICE_CLARIFICATION.pop(user_id, None)
        orig_text = pending.get("original_transcript") or pending.get("suggested_meaning") or ""
        orig_route = pending.get("suggested_route") or {}
        if orig_route.get("intent") == "TASK":
            return handle_task_intent(orig_text, orig_route)
        return orig_text or None

    if any(tok in low for tok in no_tokens):
        _PENDING_VOICE_CLARIFICATION.pop(user_id, None)
        return "ဟုတ်ကဲ့ရှင်၊ ဒါဆို ဘယ်လို ဆိုလိုတာလဲ အသေးစိတ် ပြန်ပြောပြပေးပါဦးနော်။"

    _PENDING_VOICE_CLARIFICATION.pop(user_id, None)
    return None


async def handle_voice_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
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

        voice_profile_ctx = _get_voice_profile_summary(user_id)
        analysis = await asyncio.to_thread(
            transcribe_and_analyze_voice,
            audio_bytes,
            mime_type,
            f"voice{ext}",
            voice_profile_ctx,
        )

        transcript_text = (analysis.get("transcript") or "").strip()
        if not transcript_text:
            _PENDING_VOICE_CLARIFICATION[user_id] = {
                "original_transcript": "",
                "suggested_meaning": "အသံပြတ်တောက်သွားသော voice message",
                "suggested_route": {},
            }
            clarify_msg = "အသံကို နည်းနည်း မရှင်းလိုက်ဘူးရှင်၊ ဒါကို ဆိုလိုတာလားလို့ သေချာမဖမ်းမိလိုက်လို့ တစ်ခေါက်လောက် ပြန်ပြောပေးပါလားရှင်။"
            await update.message.reply_text(clarify_msg)
            await send_voice_reply(update, context, clarify_msg)
            return

        tone_label = analysis.get("tone") or "သဘာဝကျကျ"
        meaning_hint = analysis.get("interpreted_meaning") or transcript_text
        _record_voice_profile(user_id, transcript_text, tone_label, meaning_hint)
        save_chat_message(user_id, "user", f"[Voice | {tone_label}]: {transcript_text}")

        resolved_from_pending = _resolve_pending_clarification(user_id, transcript_text)
        if resolved_from_pending is not None:
            if resolved_from_pending.startswith("ဟုတ်ကဲ့ရှင်၊ ဒါဆို"):
                save_chat_message(user_id, "assistant", resolved_from_pending)
                await update.message.reply_text(f"🎙️ \"{transcript_text}\"\n\n{resolved_from_pending}")
                await send_voice_reply(update, context, resolved_from_pending)
                return
            if resolved_from_pending and "မှတ်ထားလိုက်ပါပြီ" in resolved_from_pending:
                save_chat_message(user_id, "assistant", resolved_from_pending)
                evolve_user_profile(user_id, transcript_text, resolved_from_pending)
                await update.message.reply_text(f"🎙️ \"{transcript_text}\"\n\n{resolved_from_pending}")
                await send_voice_reply(update, context, resolved_from_pending)
                return
            if resolved_from_pending:
                transcript_text = resolved_from_pending

        history_ctx = recall_relevant_history(user_id, transcript_text, recent_limit=8, search_limit=8)
        available_groups = get_available_group_names()
        route = smart_route_and_parse(
            transcript_text,
            available_groups,
            recent_ctx=history_ctx[-1200:],
            user_pref_ctx=history_ctx[:900],
        )
        intent = route.get("intent", "GENERAL")

        is_ambiguous = bool(analysis.get("ambiguous"))
        if not is_ambiguous and intent == "TASK" and (route.get("task_action") or "").upper() == "ADD":
            clean_t = (route.get("title") or "").strip().lower()
            if clean_t in {"", "task အသစ်", "reminder သတိပေးရန်", "er ပေးရန်"}:
                is_ambiguous = True

        if is_ambiguous:
            q_txt = (analysis.get("clarification_question") or "").strip()
            if not q_txt:
                q_txt = f"“{meaning_hint}” ကို ဆိုလိုတာလားရှင်၊ သေချာအောင် ထပ်ပြောပြပေးပါလားရှင်။"
            elif "ဆိုလိုတာလား" not in q_txt:
                q_txt = f"{q_txt} — ဒါကို ဆိုလိုတာလားရှင်။"

            _PENDING_VOICE_CLARIFICATION[user_id] = {
                "original_transcript": transcript_text,
                "suggested_meaning": meaning_hint,
                "suggested_route": route,
            }
            save_chat_message(user_id, "assistant", q_txt)
            await update.message.reply_text(f"🎙️ \"{transcript_text}\"\n\n{q_txt}")
            await send_voice_reply(update, context, q_txt)
            return

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

Voice Tone & Meaning Profile:
{voice_profile_ctx or "လက်ရှိ အသံလေသံ: " + tone_label}
လက်ရှိ Voice လေသံ: {tone_label}
ခန့်မှန်း ဆိုလိုရင်း: {meaning_hint}

Memory & User Context:
{history_ctx}

User Voice Transcript: "{transcript_text}"

ညွှန်ကြားချက်များ:
- သင်ဇာ (Thinzar) အမည်ရှိ မြန်မာမိန်းကလေးဟန် AI Assistant အဖြစ် ရင်းရင်းနှီးနှီး၊ ပွင့်ပွင့်လင်းလင်းနဲ့ လူတစ်ယောက် စကားပြောသလို သဘာဝကျကျ ဖြေဆိုပါ။
- အသံနေအသံထားနဲ့ အရင်ပြောခဲ့ဖူးတဲ့ ဆိုလိုရင်း profile ကို ဆက်စပ်မှတ်မိပြီး အလိုက်သိသိ ဖြေပါ။
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

