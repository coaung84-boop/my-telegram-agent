import re
import time
import requests
from google import genai
from google.genai import types
from groq import Groq

from config import (
    GEMINI_API_KEY,
    GROQ_API_KEY,
    OPENROUTER_API_KEY,
    STRICT_MYANMAR_SYSTEM,
)

try:
    from config import GEMINI_API_KEYS
except ImportError:
    GEMINI_API_KEYS = [k for k in [GEMINI_API_KEY] if k]

gemini_clients = [genai.Client(api_key=k) for k in GEMINI_API_KEYS if k]
gemini_client = gemini_clients[0] if gemini_clients else genai.Client(api_key=GEMINI_API_KEY)
groq_client = Groq(api_key=GROQ_API_KEY)

_GEMINI_RR_INDEX = 0


def _ordered_gemini_clients() -> list:
    """Gemini client များကို request အလိုက် တလှည့်စီ (round-robin) ပြန်ပေးသည်။"""
    global _GEMINI_RR_INDEX
    base = gemini_clients or ([gemini_client] if gemini_client else [])
    if not base:
        return []
    n = len(base)
    idx = _GEMINI_RR_INDEX % n
    _GEMINI_RR_INDEX = (_GEMINI_RR_INDEX + 1) % n
    return base[idx:] + base[:idx]


def _trim_fallback_prompt(prompt_text, max_chars: int = 4000) -> str:
    """Tier 3 fallback တွင် request too large မဖြစ်စေရန် prompt ကို ချုံ့ပေးသည်။"""
    txt = str(prompt_text or "")
    if len(txt) <= max_chars:
        return txt
    return txt[-max_chars:]


def _clean_thinzar_voice(text: str) -> str:
    """Fallback model များက ယောက်ျားလေးအသုံးအနှုန်း၊ ထိုင်းစာလုံး သို့မဟုတ် သတ်ပုံအမှား ရောမိရင် သန့်ရှင်းပေးသည်။"""
    if not text:
        return text
    text = text.replace("ကျွန်တော့်", "သင်ဇာ့").replace("ကျွန်တော်", "သင်ဇာ")
    text = text.replace("မಧပိုင်း", "မွန်းလွဲပိုင်း")
    text = re.sub(r"[\u0E00-\u0E7F]+", "", text)
    text = re.sub(r"\[meta:[^\]]*\]", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()


# --- Resilient Multi-Tier AI Fallback Helper (Gemini -> OpenRouter + Qwen 3.8 27B -> Groq + Qwen 3.8 27B) ---
def call_ai_with_3tier_fallback(
    prompt_text,
    image_bytes=None,
    mime_type=None,
    use_google_search=False,
    audio_bytes=None,
):
    errors = []
    if audio_bytes:
        contents = [
            types.Part.from_bytes(data=bytes(audio_bytes), mime_type=mime_type or "audio/ogg"),
            prompt_text,
        ]
    elif image_bytes:
        contents = [
            types.Part.from_bytes(data=bytes(image_bytes), mime_type=mime_type or "image/jpeg"),
            prompt_text,
        ]
    else:
        contents = prompt_text

    tools = [types.Tool(google_search=types.GoogleSearch())] if use_google_search else None
    spelling_rule = "\nမြန်မာစာလုံးပေါင်းနှင့် သတ်ပုံများကို တိကျမှန်ကန်စွာ (ဥပမာ 'မွန်းလွဲပိုင်း') ရေးသားပါ။"
    sys_inst = f"{STRICT_MYANMAR_SYSTEM or ''}{spelling_rule}".strip()
    config = types.GenerateContentConfig(
        system_instruction=sys_inst,
        tools=tools,
    )

    # Tier 1: Gemini 3.5 Flash-Lite (round-robin across keys)
    clients_to_try = _ordered_gemini_clients()
    for key_idx, g_client in enumerate(clients_to_try):
        for attempt in range(2):
            try:
                response = g_client.models.generate_content(
                    model="gemini-3.5-flash-lite",
                    contents=contents,
                    config=config,
                )
                if response and response.text and response.text.strip():
                    return _clean_thinzar_voice(response.text.strip())
            except Exception as e:
                err_msg = f"T1(K{key_idx+1},{attempt+1}): {type(e).__name__} - {str(e)[:90]}"
                print(err_msg)
                errors.append(err_msg)
                err_str = str(e)
                if "429" in err_str or "RESOURCE_EXHAUSTED" in err_str:
                    break
                if attempt == 0:
                    time.sleep(1.0)

    # Tier 2: Gemini 3.8 Flash (round-robin across keys)
    for key_idx, g_client in enumerate(clients_to_try):
        try:
            response = g_client.models.generate_content(
                model="gemini-3.8-flash",
                contents=contents,
                config=config,
            )
            if response and response.text and response.text.strip():
                return _clean_thinzar_voice(response.text.strip())
        except Exception as e:
            err_msg = f"T2(K{key_idx+1}): {type(e).__name__} - {str(e)[:90]}"
            print(err_msg)
            errors.append(err_msg)

    if audio_bytes:
        debug_tail = "\n".join(errors[-3:])
        return f"လောလောဆယ် AI ဆာဗာ ခဏအလုပ်များနေပါတယ်ရှင်၊ ခဏနေပြီး ထပ်စမ်းကြည့်ပေးပါ:\n{debug_tail}"

    safe_fallback_prompt = _trim_fallback_prompt(prompt_text, max_chars=4000)

    # Tier 3A: OpenRouter Fallback
    if OPENROUTER_API_KEY:
        openrouter_models = [
            "openrouter/free",
            "google/gemma-4-31b-it:free",
        ]
        headers = {
            "Authorization": f"Bearer {OPENROUTER_API_KEY}",
            "Content-Type": "application/json",
        }
        for or_model in openrouter_models:
            try:
                data = {
                    "model": or_model,
                    "messages": [
                        {"role": "system", "content": STRICT_MYANMAR_SYSTEM},
                        {"role": "user", "content": safe_fallback_prompt},
                    ],
                    "temperature": 0.4,
                }
                response = requests.post(
                    "https://openrouter.ai/api/v1/chat/completions",
                    headers=headers,
                    json=data,
                    timeout=12,
                )
                res_json = response.json()
                if "choices" in res_json and len(res_json["choices"]) > 0:
                    txt = (res_json["choices"][0]["message"].get("content") or "").strip()
                    if txt:
                        return _clean_thinzar_voice(txt)
                else:
                    errors.append(f"T3A({or_model}): {str(res_json)[:90]}")
            except Exception as e:
                err_msg = f"T3A({or_model}): {type(e).__name__} - {str(e)[:90]}"
                print(err_msg)
                errors.append(err_msg)

    # Tier 3B: Groq Fallback
    groq_models = ["llama-3.3-70b-versatile"]
    for g_model in groq_models:
        try:
            completion = groq_client.with_options(timeout=12.0).chat.completions.create(
                model=g_model,
                messages=[
                    {"role": "system", "content": STRICT_MYANMAR_SYSTEM},
                    {"role": "user", "content": safe_fallback_prompt},
                ],
                temperature=0.4,
            )
            if completion and completion.choices and completion.choices[0].message.content:
                txt = completion.choices[0].message.content.strip()
                if txt:
                    return _clean_thinzar_voice(txt)
        except Exception as e:
            err_msg = f"T3B({g_model}): {type(e).__name__} - {str(e)[:90]}"
            print(err_msg)
            errors.append(err_msg)

    debug_tail = "\n".join(errors[-3:])
    return f"လောလောဆယ် AI ဆာဗာ ခဏအလုပ်များနေပါတယ်ရှင်၊ ခဏနေပြီး ထပ်စမ်းကြည့်ပေးပါ:\n{debug_tail}"













