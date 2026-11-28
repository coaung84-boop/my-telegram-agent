import os
import re
import io
import json
import time
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
CLARIFICATION_TTL_SEC = 120.0

# Per-user voice tone/intent profile, pending clarification & duplicate guard
_VOICE_PROFILES: dict[int, list[dict]] = {}
_PENDING_VOICE_CLARIFICATION: dict[int, dict] = {}
_RECENT_VOICE_MSG_IDS: dict[int, list[int]] = {}

CONFIRM_WORDS = {
    "ဟုတ်", "ဟုတ်တယ်", "ဟုတ်ပါတယ်", "ဟုတ်ကဲ့", "မှန်တယ်", "မှန်ပါတယ်",
    "အင်း", "အေး", "အဲဒါပဲ", "yes", "yeah", "yep", "ok", "okay", "sure",
}
DENY_WORDS = {
    "မဟုတ်", "မဟုတ်ဘူး", "မဟုတ်ပါဘူး", "မဟုတ်သေးဘူး", "မှားတယ်", "မှားနေတယ်",
    "no", "nope", "not that", "wrong",
}

_LETTER_SPOKEN_MM: dict[str, str] = {
    "A": "အေ", "B": "ဘီ", "C": "စီ", "D": "ဒီ", "E": "အီး",
    "F": "အက်ဖ်", "G": "ဂျီ", "H": "အိတ်ချ်", "I": "အိုင်", "J": "ဂျေ",
    "K": "ကေ", "L": "အယ်လ်", "M": "အမ်", "N": "အန်", "O": "အို",
    "P": "ပီ", "Q": "ကျူ", "R": "အာ", "S": "အက်စ်", "T": "တီ",
    "U": "ယူ", "V": "ဗီ", "W": "ဒဗလျူ", "X": "အိတ်စ်", "Y": "ဝိုင်",
    "Z": "ဇက်",
}


def _classify_confirmation(text: str) -> str:
    """Return 'confirm', 'deny', or 'other'."""
    cleaned = (text or "").strip().lower().rstrip("။.!?")
    if not cleaned:
        return "other"
    if cleaned in CONFIRM_WORDS or any(cleaned.startswith(w + " ") for w in CONFIRM_WORDS):
        return "confirm"
    if cleaned in DENY_WORDS or any(cleaned.startswith(w + " ") for w in DENY_WORDS):
        return "deny"
    return "other"


def _is_followup_reference(text: str) -> bool:
    s = (text or "").strip().lower()
    markers = [
        "အဲဒါ", "အဲဒီ", "ခုနက", "နောက်ထပ်", "ဆက်ပြော", "ဘယ်ချိန်",
        "that", "it", "earlier", "more", "continue",
    ]
    return any(m in s for m in markers)


def _is_new_command_or_task(text: str) -> bool:
    s = (text or "").strip().lower()
    markers = [
        "remind", "reminder", "မှတ်ထား", "မှတ်ပေး", "task", "ဖုန်း", "call",
        "မနက်ဖြန်", "ဒီနေ့", "နောက်", "မိနစ်", "နာရီ",
    ]
    return any(m in s for m in markers)


def _limit_to_3_to_5_sentences(text: str, max_sentences: int = 5) -> str:
    """စာကြောင်း/အပိုင်းများကို အများဆုံး ၃–၅ ကြောင်းခန့်အတွင်း တိုတိုရှင်းရှင်း သဘာဝကျကျ ကန့်သတ်သည်။"""
    cleaned = re.sub(r"\s+", " ", (text or "").strip())
    if not cleaned:
        return ""

    parts = [p.strip() for p in re.split(r"(?<=[။!?])\s*", cleaned) if p.strip()]
    if len(parts) == 1 and len(parts[0]) > 180:
        sub_parts = [p.strip() for p in re.split(r"(?<=[၊,])\s*", parts[0]) if p.strip()]
        if len(sub_parts) > 1:
            parts = sub_parts

    selected = parts[:max_sentences]
    out: list[str] = []
    for item in selected:
        item = item.rstrip("၊,").strip()
        if not item:
            continue
        if not item.endswith(("။", "!", "?")):
            item = f"{item}။"
        out.append(item)

    result = " ".join(out).strip()
    if len(result) > 420:
        result = result[:415].rsplit(" ", 1)[0].rstrip("၊,။") + "။"
    return result


def _is_duplicate_voice_update(user_id: int, message_id: int | None) -> bool:
    if not message_id:
        return False
    seen = _RECENT_VOICE_MSG_IDS.setdefault(user_id, [])
    if message_id in seen:
        return True
    seen.append(message_id)
    if len(seen) > 40:
        del seen[:-40]
    return False


def _deduplicate_reply_text(text: str) -> str:
    """စာ reply ထဲမှာ မတော်တဆ ထပ်နေတဲ့ စာပိုဒ်/စာကြောင်းတူတွေ ပါလာရင် ရှင်းထုတ်ပေးသည်။"""
    if not text:
        return ""
    text = re.sub(r"\s*\[(?:Verified|Deduced|Unverified)\]\s*", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"[ \t]+([။၊,.!?])", r"\1", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    cleaned = re.sub(r"\n{3,}", "\n\n", text.strip())
    paragraphs = cleaned.split("\n\n")
    unique_paras: list[str] = []
    seen_para_keys: set[str] = set()

    for para in paragraphs:
        p_norm = re.sub(r"\s+", " ", para).strip()
        if not p_norm:
            continue
        key = re.sub(r"[^\w\u1000-\u109F]+", "", p_norm).lower()
        if key and key in seen_para_keys:
            continue
        if key:
            seen_para_keys.add(key)

        lines = para.splitlines()
        unique_lines: list[str] = []
        seen_line_keys: set[str] = set()
        for ln in lines:
            ln_norm = re.sub(r"\s+", " ", ln).strip()
            if not ln_norm:
                unique_lines.append(ln)
                continue
            l_key = re.sub(r"[^\w\u1000-\u109F]+", "", ln_norm).lower()
            if len(l_key) >= 12 and l_key in seen_line_keys:
                continue
            if len(l_key) >= 12:
                seen_line_keys.add(l_key)
            unique_lines.append(ln)

        unique_paras.append("\n".join(unique_lines).strip())

    return "\n\n".join([p for p in unique_paras if p]).strip()


def _expand_uppercase_acronyms_for_speech(text: str) -> str:
    if not text:
        return ""

    def _repl(match: re.Match) -> str:
        token = match.group(0)
        parts = [_LETTER_SPOKEN_MM.get(ch, ch) for ch in token]
        return " ".join(parts)

    return re.sub(r"\b[A-Z]{2,6}\b", _repl, text)


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


def _normalize_mm_spelling_and_voice(text: str) -> str:
    """မြန်မာစာလုံးပေါင်း/သတ်ပုံအမှားအချို့နဲ့ သင်ဇာ့အသုံးအနှုန်းကို ညှိပေးသည်။"""
    if not text:
        return ""
    s = text.strip()
    s = s.replace("ကျွန်တော့်", "သင်ဇာ့").replace("ကျွန်တော်", "သင်ဇာ")
    s = re.sub(r"[\u0E00-\u0E7F]+", "", s)
    s = re.sub(r"\[meta:[^\]]*\]", "", s)
    replacements = {
        "မಧပိုင်း": "မွန်းလွဲပိုင်း",
        "မွန်းတည့်ပိုင်း": "နေ့လယ်ပိုင်း",
        "ပြင်ဆင်ပေးပါ": "ပြင်ဆင်ပေးပါ",
        "အဆင်ပြေပါတယ်": "အဆင်ပြေပါတယ်",
    }
    for wrong, right in replacements.items():
        s = s.replace(wrong, right)
    return re.sub(r"\s+", " ", s).strip()


def _normalize_heard_transcript(text: str) -> str:
    """STT မှာ အသံထွက်ဆင်တူ မှားကြားနိုင်တာတွေကို ဆိုလိုရင်းအတိုင်း ညှိပေးသည်။"""
    if not text:
        return ""
    s = text.strip()
    s = re.sub(r"(?:တင်ဇာ|တင်စာ|သင်စာ|သင်ဇာ့)", lambda m: "သင်ဇာ" if m.group(0) != "သင်ဇာ့" else "သင်ဇာ့", s)
    s = re.sub(r"\bရီမိုင်ဒါ\b", "reminder", s, flags=re.IGNORECASE)
    s = re.sub(r"\bတပ်စ်\b", "task", s, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", s).strip()


def _to_spoken_chat_prose(raw_text: str) -> str:
    base_clean = re.sub(r"https?://\S+", "", raw_text)
    base_clean = re.sub(r"\[[^\]]+\]\([^\)]+\)", "", base_clean)
    base_clean = re.sub(r"\[(Verified|Deduced|Unverified)\]", "", base_clean, flags=re.IGNORECASE)
    base_clean = re.sub(
        r"[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF]",
        " ",
        base_clean,
    )
    base_clean = re.sub(r"[*_`~#>\-]+", " ", base_clean)

    text = _humanize_dates_and_times_for_speech(base_clean)
    text = re.sub(r"(?im)^\s*[-•*]+\s*key\s*point\s*:\s*", "အဓိက ကတော့ ", text)
    text = re.sub(r"(?im)^\s*အကြံပြုချက်\s*:\s*", "သင်ဇာ့အနေနဲ့ ပြောရရင်တော့ ", text)
    text = re.sub(r"(?im)^\s*\d+[\.\)]\s*", "", text)
    text = re.sub(r"(?m)^\s*[-•*]+\s*", "", text)

    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    spoken_lines: list[str] = []
    for idx, ln in enumerate(lines[:5]):
        ln = re.sub(r"\s*:\s*", "၊ ", ln)
        ln = re.sub(r"\s+", " ", ln).strip()
        if not ln:
            continue
        if idx > 0 and not ln.startswith(("အင်း", "ဟုတ်", "ဒါဆို", "ပြီးတော့", "အဓိက", "သင်ဇာ")):
            ln = f"ပြီးတော့ {ln}"
        if not ln.endswith(("။", "၊", "!", "?")):
            ln = f"{ln}။"
        spoken_lines.append(ln)

    combined = " ".join(spoken_lines)
    combined = re.sub(r"\s+", " ", combined).strip()
    return _limit_to_3_to_5_sentences(combined, max_sentences=5)


def _build_conversational_speech(reply_text: str) -> str:
    if not reply_text:
        return ""

    raw = reply_text.strip()

    if "မှတ်ထားလိုက်ပါပြီ" in raw:
        m_title = re.search(r'[“"]([^”"]+)[”"]\s*မှတ်ထားလိုက်ပါပြီ', raw)
        title_clean = (m_title.group(1) if m_title else "").strip()
        if title_clean and title_clean.lower() not in {"er ပေးရန်", "reminder ပေးရန်", "remind ပေးရန်"}:
            spoken = f"ဟုတ်ကဲ့ရှင်၊ {title_clean} ကို မှတ်ထားလိုက်ပါပြီနော်။ အချိန်ရောက်ရင် သင်ဇာ သတိပေးပါမယ်ရှင်။"
        else:
            spoken = "ဟုတ်ကဲ့ရှင်၊ သတိပေးဖို့ မှတ်ထားလိုက်ပါပြီနော်။ အချိန်ရောက်ရင် သင်ဇာ သတိပေးပါမယ်ရှင်။"
        return _expand_uppercase_acronyms_for_speech(_limit_to_3_to_5_sentences(spoken, max_sentences=5))

    prose = _to_spoken_chat_prose(raw)
    return _expand_uppercase_acronyms_for_speech(_limit_to_3_to_5_sentences(prose, max_sentences=5))


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


def _refine_transcript_with_context(
    raw_transcript: str,
    tone: str,
    recent_context: str = "",
    available_groups: list[str] | None = None,
) -> tuple[str, str, str]:
    """ကြားလိုက်တဲ့ စကားကို လိုအပ်မှသာ context နဲ့ ညှိပေးသည်။"""
    cleaned = _normalize_heard_transcript(raw_transcript)
    if not cleaned:
        return "", tone, ""

    ctx_for_prompt = (recent_context or "")[-400:] if _is_followup_reference(cleaned) else ""
    groups_str = ", ".join((available_groups or [])[:15]) or "မရှိသေးပါ"
    prompt = (
        "အောက်ပါ Voice Transcript ကို စစ်ဆေးပြီး အသံထွက်ဆင်တူ မှားကြားနိုင်တာရှိရင် ရှင်းအောင် ပြင်ပေးပါ။ "
        "အသစ်ပြောတဲ့ command/topic ဖြစ်ရင် အရှေ့က အကြောင်းအရာဟောင်းတွေနဲ့ ရောမပစ်ပါနဲ့။\n"
        f"Voice Tone: {tone}\n"
        f"Available Groups: {groups_str}\n"
        f"Follow-up Context:\n{ctx_for_prompt or 'မသုံးပါ'}\n"
        f"Raw Transcript: \"{cleaned}\"\n\n"
        "Return ONLY valid JSON:\n"
        "{\"resolved_text\": \"စကားသား\", "
        "\"intent_hint\": \"TASK | GROUP | WEB_SEARCH | GENERAL\", "
        "\"meaning\": \"ဆိုလိုရင်း အနှစ်ချုပ်\"}"
    )
    try:
        res = (call_ai_with_3tier_fallback(prompt) or "").strip()
        m = re.search(r"\{.*\}", res, re.DOTALL)
        if m:
            data = json.loads(m.group(0))
            if isinstance(data, dict):
                r_txt = _normalize_heard_transcript(str(data.get("resolved_text") or "").strip()) or cleaned
                i_hint = str(data.get("intent_hint") or "GENERAL").strip().upper()
                m_txt = _normalize_heard_transcript(str(data.get("meaning") or "").strip()) or r_txt
                return r_txt, i_hint, m_txt
    except Exception:
        pass

    return cleaned, "GENERAL", cleaned


def transcribe_and_analyze_voice(
    audio_bytes: bytes,
    mime_type: str = "audio/ogg",
    filename: str = "voice.ogg",
    voice_profile_ctx: str = "",
    recent_context_ctx: str = "",
    available_groups: list[str] | None = None,
) -> dict:
    transcript = ""
    tone_label = "သဘာဝကျကျ/ပုံမှန်"
    intent_hint = "GENERAL"
    ambiguous = False
    clarification_q = ""
    interpreted_meaning = ""
    groups_str = ", ".join((available_groups or [])[:15]) or "မရှိသေးပါ"

    if audio_bytes:
        audio_part = types.Part.from_bytes(data=bytes(audio_bytes), mime_type=mime_type or "audio/ogg")
        prompt = f"""
နားထောင်ပြီး ဒီအသံဖိုင်ကို ခွဲခြမ်းစိတ်ဖြာပေးပါ။
လက်ရှိ မြန်မာစံတော်ချိန်: {datetime.now(MM_TZ).strftime("%Y-%m-%d %H:%M (%A)")}
အသုံးပြုနိုင်သော Group များ: {groups_str}

အရင် Voice Tone & Meaning Profile:
{voice_profile_ctx or "မှတ်တမ်းမရှိသေးပါ"}

လတ်တလော မှတ်တမ်း Context (follow-up ဖြစ်မှသာ ချိတ်ဆက်ပါ):
{(recent_context_ctx or "")[-500:] or "မရှိသေးပါ"}

Return ONLY valid JSON:
{{
  "transcript": "ပြောထားတဲ့စကားကို မြန်မာ (Unicode) / English မှန်ကန်အောင် စာပြန်ရေးထားချက်",
  "tone": "အသံနေအသံထား",
  "intent_hint": "TASK | GROUP | WEB_SEARCH | GENERAL",
  "interpreted_meaning": "အဓိက ဆိုလိုရင်း",
  "ambiguous": false,
  "clarification_question": ""
}}

Rules:
- အခု ပြောလိုက်တဲ့ အသံကို အဓိက ဦးစားပေးပါ။ Command အသစ် (ဥပမာ 'ဖုန်းဆက်ဖို့ remind ပေးပါ') ဖြစ်ရင် အရှေ့ကဟာနဲ့ ရောမပစ်ပါနဲ့။
- အရှေ့က စကားဆက်နဲ့ ဆက်စပ်နေတဲ့ 'အဲဒါ'၊ 'ခုနကဟာ' စတာ ပါမှသာ context နဲ့ ချိတ်ပါ။
- အသံ မရှင်းတာ၊ ဘာဆိုလိုမှန်း မသေချာတာ ဖြစ်ရင် ambiguous=true ထားပြီး clarification_question မှာ သဘာဝကျကျ မေးခွန်းထုတ်ပါ။
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
                        t_clean = _normalize_heard_transcript(str(parsed["transcript"]).strip())
                        m_clean = _normalize_heard_transcript(str(parsed.get("interpreted_meaning") or "").strip())
                        tone_val = str(parsed.get("tone") or tone_label).strip()
                        intent_val = str(parsed.get("intent_hint") or intent_hint).strip().upper()
                        return {
                            "transcript": t_clean,
                            "tone": tone_val,
                            "intent_hint": intent_val,
                            "interpreted_meaning": m_clean or t_clean,
                            "ambiguous": bool(parsed.get("ambiguous", False)),
                            "clarification_question": str(parsed.get("clarification_question") or "").strip(),
                        }
                if raw and not raw.startswith("{"):
                    transcript = _normalize_heard_transcript(raw)
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
                    transcript = _normalize_heard_transcript(txt)
                    break
            except Exception as e:
                print(f"[Voice Whisper {model_name} Error]: {e}")

    if transcript:
        refined_txt, refined_intent, refined_meaning = _refine_transcript_with_context(
            transcript,
            tone_label,
            recent_context=recent_context_ctx,
            available_groups=available_groups,
        )
        transcript = refined_txt
        intent_hint = refined_intent
        interpreted_meaning = refined_meaning

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
        for item in hist[-4:]
    ]
    return "\n".join(lines)


def _add_burmese_intonation_cues(clause_txt: str, role: str) -> str:
    s = clause_txt.strip()
    if not s:
        return s
    if role == "lift" and not s.endswith(("၊", "။", "?", "!")):
        if re.search(r"(?:နော်|ရှင်|ပဲ|ပါ|လား|လဲ|တော့)$", s):
            return f"{s}၊"
    return s


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
        if len(words) >= 11:
            mid = len(words) // 2
            left = " ".join(words[:mid]).strip()
            right = " ".join(words[mid:]).strip()
            if left:
                clauses.append((_add_burmese_intonation_cues(left, "lift"), "lift"))
            if right:
                clauses.append((_add_burmese_intonation_cues(right, "settle" if ends_period else "mid"), "settle" if ends_period else "mid"))
        else:
            role = "settle" if ends_period else ("lift" if ends_comma else "mid")
            clauses.append((_add_burmese_intonation_cues(core, role), role))

    return clauses or [(spoken_text, "mid")]


async def _render_expressive_edge_tts(spoken_text: str, out_path: str) -> bool:
    clauses = _split_into_expressive_clauses(spoken_text)

    lift_cycle = [
        ("+12Hz", "+21%", 0.03),
        ("+14Hz", "+23%", 0.03),
        ("+11Hz", "+20%", 0.04),
    ]

    out_buf = io.BytesIO()
    for idx, (clause_txt, role) in enumerate(clauses):
        if role == "settle":
            mm_pitch, mm_rate, _ = "+9Hz", "+17%", 0.08
        elif role == "lift":
            mm_pitch, mm_rate, _ = "+14Hz", "+22%", 0.04
        else:
            mm_pitch, mm_rate, _ = lift_cycle[idx % len(lift_cycle)]

        paced_clause = re.sub(
            r"(?<![A-Za-z])([A-Za-z][A-Za-z0-9\-\.]*)(?![A-Za-z])",
            r"، \1 ،",
            clause_txt,
        )
        paced_clause = re.sub(r"(?:،\s*){2,}", "၊ ", paced_clause)
        paced_clause = re.sub(r"^\s*،\s*", "", paced_clause)
        paced_clause = re.sub(r"\s*،\s*([။!?])", r"\1", paced_clause)
        paced_clause = re.sub(r"\s+", " ", paced_clause).strip()
        if paced_clause:
            communicate = edge_tts.Communicate(
                text=paced_clause,
                voice="my-MM-NilarNeural",
                rate=mm_rate,
                pitch=mm_pitch,
            )
            async for ev in communicate.stream():
                if ev.get("type") == "audio" and ev.get("data"):
                    out_buf.write(ev["data"])

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
                    timeout=35.0,
                )
                if saved_ok:
                    break
            except Exception:
                await asyncio.sleep(0.4)

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

    created_at = float(pending.get("created_at") or 0.0)
    if (time.monotonic() - created_at) > CLARIFICATION_TTL_SEC:
        _PENDING_VOICE_CLARIFICATION.pop(user_id, None)
        return None

    verdict = _classify_confirmation(transcript_text)

    if verdict == "confirm":
        _PENDING_VOICE_CLARIFICATION.pop(user_id, None)
        orig_text = pending.get("original_transcript") or pending.get("suggested_meaning") or ""
        orig_route = pending.get("suggested_route") or {}
        if orig_route.get("intent") == "TASK":
            return handle_task_intent(orig_text, orig_route)
        return orig_text or None

    if verdict == "deny":
        _PENDING_VOICE_CLARIFICATION.pop(user_id, None)
        return "ဟုတ်ကဲ့ရှင်၊ ဆက်ပြောဖို့ ပြန်နားထောင်နေပါတယ် — ဘာလုပ်ချင်တာလဲ (သို့မဟုတ်) ဘာမေးချင်တာလဲ ပြောပြပေးပါရှင်။"

    _PENDING_VOICE_CLARIFICATION.pop(user_id, None)
    return None


async def handle_voice_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.effective_user:
        return

    user_id = update.effective_user.id
    if user_id != ADMIN_USER_ID:
        return

    if _is_duplicate_voice_update(user_id, update.message.message_id):
        return

    try:
        voice = update.message.voice or update.message.audio
        if not voice:
            return

        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
        ext, mime_type = _resolve_audio_extension_and_mime(voice, bool(update.message.voice))
        tg_file = await context.bot.get_file(voice.file_id)
        audio_bytes = bytes(await tg_file.download_as_bytearray())

        available_groups = get_available_group_names()
        pre_history_ctx = recall_relevant_history(user_id, "voice context", recent_limit=3, search_limit=3)
        voice_profile_ctx = _get_voice_profile_summary(user_id)

        analysis = await asyncio.to_thread(
            transcribe_and_analyze_voice,
            audio_bytes,
            mime_type,
            f"voice{ext}",
            voice_profile_ctx,
            pre_history_ctx,
            available_groups,
        )

        transcript_text = _normalize_heard_transcript((analysis.get("transcript") or "").strip())
        tone_label = analysis.get("tone") or "သဘာဝကျကျ"
        meaning_hint = _normalize_heard_transcript(analysis.get("interpreted_meaning") or transcript_text)

        if not transcript_text:
            clarify_msg = "အသံကို နည်းနည်း မရှင်းလိုက်ဘူးရှင်၊ ဟုတ်ရင် ဟုတ်တယ်လို့ ပြောပေးပါနော် — ဘာဆိုလိုတာလဲ တစ်ခေါက်လောက် ထပ်ပြောပေးပါလားရှင်။"
            _PENDING_VOICE_CLARIFICATION[user_id] = {
                "created_at": time.monotonic(),
                "original_transcript": "",
                "suggested_meaning": "",
                "suggested_route": {"intent": "GENERAL"},
            }
            await update.message.reply_text(_deduplicate_reply_text(clarify_msg))
            await send_voice_reply(update, context, clarify_msg)
            return

        _record_voice_profile(user_id, transcript_text, tone_label, meaning_hint)
        save_chat_message(user_id, "user", f"[Voice | {tone_label}]: {transcript_text}")

        resolved_from_pending = _resolve_pending_clarification(user_id, transcript_text)
        if resolved_from_pending is not None:
            if resolved_from_pending.startswith("ဟုတ်ကဲ့ရှင်၊ ဆက်ပြောဖို့") or "မှတ်ထားလိုက်ပါပြီ" in resolved_from_pending:
                clean_pending_reply = _limit_to_3_to_5_sentences(_deduplicate_reply_text(resolved_from_pending), max_sentences=5)
                save_chat_message(user_id, "assistant", clean_pending_reply)
                evolve_user_profile(user_id, transcript_text, clean_pending_reply)
                await update.message.reply_text(_deduplicate_reply_text(f"🎙️ \"{transcript_text}\"\n\n{clean_pending_reply}"))
                await send_voice_reply(update, context, clean_pending_reply)
                return
            if resolved_from_pending:
                transcript_text = resolved_from_pending

        use_ctx = _is_followup_reference(transcript_text) and not _is_new_command_or_task(transcript_text)
        if use_ctx:
            history_ctx = recall_relevant_history(user_id, f"{transcript_text} {meaning_hint}", recent_limit=4, search_limit=4)
            route_recent_ctx = (history_ctx or "")[-600:]
        else:
            history_ctx = recall_relevant_history(user_id, transcript_text, recent_limit=2, search_limit=2)
            route_recent_ctx = ""

        route = smart_route_and_parse(
            transcript_text,
            available_groups,
            recent_ctx=route_recent_ctx,
            user_pref_ctx="",
        )
        intent = route.get("intent", "GENERAL")
        if intent == "GENERAL" and analysis.get("intent_hint") in {"TASK", "GROUP", "WEB_SEARCH"}:
            intent = analysis["intent_hint"]
            route["intent"] = intent

        is_unclear_voice = bool(analysis.get("ambiguous")) and not _is_new_command_or_task(transcript_text)
        is_vague_remind = (
            intent == "TASK"
            and (route.get("task_action") or "").upper() in {"ADD", "REMIND"}
            and len(transcript_text.replace("remind", "").replace("reminder", "").replace("ပေးပါ", "").strip()) <= 2
        )

        if is_unclear_voice or is_vague_remind:
            q_txt = (analysis.get("clarification_question") or "").strip()
            if not q_txt:
                if is_vague_remind:
                    q_txt = "ဘာကို remind ပေးစေချင်တာလဲရှင်၊ ဟုတ်ရင် ဟုတ်တယ်လို့ ပြောပြီး ဘာလုပ်ရမယ်ဆိုတာ ပြောပြပေးပါနော်။"
                else:
                    q_txt = f"အသံနည်းနည်း မရှင်းလို့ “{meaning_hint}” လို့ ဆိုလိုတာလားရှင်၊ ဟုတ်ရင် ဟုတ်တယ်လို့ ပြောပေးပါနော်။"
            _PENDING_VOICE_CLARIFICATION[user_id] = {
                "created_at": time.monotonic(),
                "original_transcript": transcript_text,
                "suggested_meaning": meaning_hint,
                "suggested_route": route,
            }
            clean_q = _limit_to_3_to_5_sentences(_deduplicate_reply_text(q_txt), max_sentences=5)
            save_chat_message(user_id, "assistant", clean_q)
            await update.message.reply_text(_deduplicate_reply_text(f"🎙️ \"{transcript_text}\"\n\n{clean_q}"))
            await send_voice_reply(update, context, clean_q)
            return

        if intent == "TASK":
            t_act = (route.get("task_action") or "").upper()
            if t_act == "LIST" and not (route.get("title") or route.get("task_title")):
                reply = "ဘယ် task ကို ဆိုလိုတာလဲရှင်၊ Task နာမည်လေး ပြောပြပေးပါလားနော်။"
            else:
                if t_act in {"ADD", "REMIND", "UPDATE"} and not (route.get("title") or route.get("task_title")):
                    route["title"] = transcript_text
                raw_t_reply = handle_task_intent(transcript_text, route) or ""
                if len(raw_t_reply) > 450 and ("Task စာရင်း" in raw_t_reply or "Pending" in raw_t_reply):
                    reply = "ဘယ် task ကို ဆိုလိုတာလဲရှင်၊ Task နာမည်လေး ပြောပြပေးပါလားနော်။"
                else:
                    reply = raw_t_reply
        elif intent == "GROUP":
            reply = handle_group_query(transcript_text, route)
        elif intent == "WEB_SEARCH":
            search_q = route.get("search_query") or meaning_hint or transcript_text
            eng_q = route.get("english_search_query")
            raw_web = perform_hybrid_web_search(search_q, english_query=eng_q, history_ctx=history_ctx)
            reply = _limit_to_3_to_5_sentences(raw_web, max_sentences=5)
        else:
            cal_ctx = get_myanmar_calendar_context()
            prompt = f"""
အသုံးပြုသူက Voice message ဖြင့် ပြောကြားလာသည်ကို နားထောင်ခွဲခြမ်းစိတ်ဖြာထားခြင်း ဖြစ်သည်။
Current Myanmar Calendar & Time:
{cal_ctx}

Voice Tone & Meaning Profile:
{voice_profile_ctx or "လက်ရှိ အသံလေသံ: " + tone_label}
လက်ရှိ Voice လေသံ: {tone_label}
နားလည်ထားသော ဆိုလိုရင်း: {meaning_hint}

Context (follow-up ဖြစ်မှသာ အသုံးပြုပါ):
{route_recent_ctx or "မသုံးပါ"}

User Voice Transcript: "{transcript_text}"

ညွှန်ကြားချက်များ:
- အခုနောက်ဆုံး ပြောလိုက်သော User Voice Transcript ("{transcript_text}") ကိုသာ တိုက်ရိုက် ဦးစားပေး ဖြေဆိုပါ။ သက်ဆိုင်မှုမရှိသော အကြောင်းအရာဟောင်းများကို ထပ်မပြောပါနှင့်။
- စာကြောင်း ၃ မှ ၅ ကြောင်းအတွင်းသာ တိုတိုရှင်းရှင်း၊ လိုရင်းအနှစ်ချုပ်ဖြင့် လူအချင်းချင်း စကားပြောနေသလို သဘာဝကျကျ ဖြေဆိုပါ (ဤစာအတိုင်းပင် အသံဖြင့် ပြန်ပြောပြမည်)။ စာရှည်ကြီးများ မရေးပါနှင့်။
- ဘယ် task ကို ဆိုလိုမှန်း မသေချာလျှင် "ဘယ် task ကို ဆိုလိုတာလဲရှင်" ဟု သဘာဝကျကျ ပြန်မေးပါ။
- သင်ဇာ (Thinzar) အမည်ရှိ မြန်မာမိန်းကလေးဟန် AI Assistant အဖြစ် ရင်းရင်းနှီးနှီး၊ သဘာဝကျကျ ဖြေဆိုပါ။
- မြန်မာစာလုံးပေါင်းနှင့် သတ်ပုံများကို မှန်ကန်စွာ ရေးသားပါ။
- 'မောင်' ဟု ဆေးသောက်ရန်နှင့် မိသားစု/ကလေး Task များမှလွဲ၍ မသုံးပါနှင့်။
"""
            reply = call_ai_with_3tier_fallback(prompt)

        normalized_reply = _normalize_mm_spelling_and_voice(reply or "")
        dedup_reply = _deduplicate_reply_text(normalized_reply)
        clean_reply = _limit_to_3_to_5_sentences(dedup_reply, max_sentences=5) if intent != "TASK" else dedup_reply

        save_chat_message(user_id, "assistant", clean_reply)
        evolve_user_profile(user_id, transcript_text, clean_reply)

        await update.message.reply_text(_deduplicate_reply_text(f"🎙️ \"{transcript_text}\"\n\n{clean_reply}"))
        await send_voice_reply(update, context, clean_reply)

    except Exception as e:
        print(f"Error handling voice message: {e}")
        err_msg = "အသံနဲ့ ပြန်ဖြေရာမှာ ခဏအခက်အခဲ ရှိနေပါတယ်ရှင်၊ နောက်မှ ထပ်စမ်းကြည့်ပါ။"
        await update.message.reply_text(_deduplicate_reply_text(err_msg))

