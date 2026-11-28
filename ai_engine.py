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

try:
    from config import (
        GEMINI_FAST_MODELS,
        GEMINI_THINKING_MODELS,
        OPENROUTER_FREE_MODELS,
        GROQ_FALLBACK_MODELS,
    )
except ImportError:
    GEMINI_FAST_MODELS = ["gemini-3.5-flash-lite", "gemini-3.8-flash"]
    GEMINI_THINKING_MODELS = ["gemini-3.8-flash", "gemini-3.5-flash-lite"]
    OPENROUTER_FREE_MODELS = [
        "openrouter/free",
        "google/gemma-4-26b-a4b-it:free",
        "google/gemma-4-31b-it:free",
    ]
    GROQ_FALLBACK_MODELS = ["llama-3.3-70b-versatile"]

gemini_clients = [genai.Client(api_key=k) for k in GEMINI_API_KEYS if k]
gemini_client = gemini_clients[0] if gemini_clients else genai.Client(api_key=GEMINI_API_KEY)
groq_client = Groq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None

_KEY_RR_INDEX = 0


def _next_gemini_clients(clients: list) -> list:
    """Round-robin key order so both Gemini keys share load evenly."""
    global _KEY_RR_INDEX
    if not clients:
        return []
    n = len(clients)
    start = _KEY_RR_INDEX % n
    _KEY_RR_INDEX = (_KEY_RR_INDEX + 1) % n
    return clients[start:] + clients[:start]


def _needs_deep_thinking(prompt_text) -> bool:
    """Detect queries that benefit from deeper reasoning / multi-step analysis."""
    if not isinstance(prompt_text, str):
        return False
    s = prompt_text.lower()
    thinking_cues = [
        "နှိုင်းယှဉ်", "သုံးသပ်", "ဘာကြောင့်", "ဘယ်လိုစဉ်းစား", "အကြံပြု",
        "ဆုံးဖြတ်", "အစီအစဉ်", "စဉ်းစား", "စကားထာ", "logic", "puzzle",
        "plan", "strategy", "analyze", "compare", "why", "reason",
        "architecture", "design", "debug",
    ]
    return len(s) > 260 or any(k in s for k in thinking_cues)


def _build_gemini_config(sys_inst: str, tools, deep_thinking: bool) -> types.GenerateContentConfig:
    """Build GenerateContentConfig safely, enabling thinking when supported by SDK/model."""
    if deep_thinking:
        try:
            thinking_cfg = types.ThinkingConfig(thinking_budget=1024)
            return types.GenerateContentConfig(
                system_instruction=sys_inst,
                tools=tools,
                thinking_config=thinking_cfg,
            )
        except Exception:
            pass
    return types.GenerateContentConfig(
        system_instruction=sys_inst,
        tools=tools,
    )


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

    deep_thinking = (not audio_bytes) and (not image_bytes) and _needs_deep_thinking(prompt_text)
    tools = [types.Tool(google_search=types.GoogleSearch())] if use_google_search else None
    spelling_rule = "\nမြန်မာစာလုံးပေါင်းနှင့် သတ်ပုံများကို တိကျမှန်ကန်စွာ (ဥပမာ 'မွန်းလွဲပိုင်း') ရေးသားပါ။"
    thinking_rule = (
        "\nယခုမေးခွန်းအတွက် အချက်အလက်နှင့် ယုတ္တိကို အတွင်းပိုင်းတွင် သေချာစဉ်းစားသုံးသပ်ပြီး "
        "နောက်ဆုံးအဖြေကို ရှင်းလင်းပြတ်သားသော မြန်မာစာဖြင့် ဖြေဆိုပါ။"
        if deep_thinking
        else ""
    )
    sys_inst = f"{STRICT_MYANMAR_SYSTEM or ''}{spelling_rule}{thinking_rule}".strip()
    config = _build_gemini_config(sys_inst, tools, deep_thinking=deep_thinking)

    model_candidates = list(
        dict.fromkeys(
            (GEMINI_THINKING_MODELS if deep_thinking else GEMINI_FAST_MODELS)
            + (GEMINI_FAST_MODELS if deep_thinking else GEMINI_THINKING_MODELS)
        )
    )

    clients_to_try = _next_gemini_clients(gemini_clients or ([gemini_client] if gemini_client else []))
    for m_idx, m_name in enumerate(model_candidates):
        for key_idx, g_client in enumerate(clients_to_try):
            max_attempts = 2 if m_idx == 0 and key_idx == 0 else 1
            for attempt in range(max_attempts):
                try:
                    response = g_client.models.generate_content(
                        model=m_name,
                        contents=contents,
                        config=config,
                    )
                    if response and response.text and response.text.strip():
                        return _clean_thinzar_voice(response.text.strip())
                except Exception as e:
                    err_str = str(e)
                    err_msg = f"Gemini({m_name},K{key_idx+1},{attempt+1}): {type(e).__name__} - {err_str[:90]}"
                    print(err_msg)
                    errors.append(err_msg)
                    if "429" in err_str or "RESOURCE_EXHAUSTED" in err_str:
                        break
                    if attempt + 1 < max_attempts:
                        time.sleep(0.8)

    if audio_bytes:
        print("Audio AI tiers failed:", " | ".join(errors[-3:]))
        return "လောလောဆယ် အသံကို နားထောင်ဖို့ AI ဆာဗာ ခဏအလုပ်များနေပါတယ်ရှင်၊ ခဏနေပြီး ထပ်စမ်းကြည့်ပေးပါနော်။"

    if image_bytes:
        print("Image AI tiers failed:", " | ".join(errors[-3:]))
        return "လောလောဆယ် ပုံကို ခွဲခြမ်းစိတ်ဖြာဖို့ AI ဆာဗာ ခဏအလုပ်များနေပါတယ်ရှင်၊ ခဏနေပြီး ထပ်ပို့ကြည့်ပေးပါနော်။"

    compact_prompt = str(prompt_text or "")
    if len(compact_prompt) > 4200:
        compact_prompt = compact_prompt[-4200:]

    # Tier 3A: OpenRouter Free Models
    if OPENROUTER_API_KEY and OPENROUTER_FREE_MODELS:
        headers = {
            "Authorization": f"Bearer {OPENROUTER_API_KEY}",
            "Content-Type": "application/json",
        }
        for or_model in OPENROUTER_FREE_MODELS:
            try:
                payload = {
                    "model": or_model,
                    "messages": [
                        {"role": "system", "content": STRICT_MYANMAR_SYSTEM},
                        {"role": "user", "content": compact_prompt},
                    ],
                    "temperature": 0.4,
                }
                response = requests.post(
                    "https://openrouter.ai/api/v1/chat/completions",
                    headers=headers,
                    json=payload,
                    timeout=12,
                )
                res_json = response.json()
                if "choices" in res_json and len(res_json["choices"]) > 0:
                    txt = (res_json["choices"][0]["message"].get("content") or "").strip()
                    if txt:
                        return _clean_thinzar_voice(txt)
                err_txt = str(res_json)
                print(f"T3A({or_model}): {err_txt[:140]}")
                if any(k in err_txt.lower() for k in ("404", "unavailable for free", "does not exist")):
                    continue
                errors.append(f"T3A({or_model}): {err_txt[:90]}")
            except Exception as e:
                err_msg = f"T3A({or_model}): {type(e).__name__} - {str(e)[:90]}"
                print(err_msg)
                if any(k in err_msg.lower() for k in ("404", "unavailable for free", "does not exist")):
                    continue
                errors.append(err_msg)

    # Tier 3B: Groq Fallback
    if groq_client and GROQ_FALLBACK_MODELS:
        for g_model in GROQ_FALLBACK_MODELS:
            try:
                completion = groq_client.with_options(timeout=12.0).chat.completions.create(
                    model=g_model,
                    messages=[
                        {"role": "system", "content": STRICT_MYANMAR_SYSTEM},
                        {"role": "user", "content": compact_prompt},
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
                if any(k in err_msg.lower() for k in ("404", "does not exist")):
                    continue
                errors.append(err_msg)

    if errors:
        print("All AI tiers failed:", " | ".join(errors[-4:]))
    return "လောလောဆယ် AI ဆာဗာ ခဏအလုပ်များနေပါတယ်ရှင်၊ ခဏနေပြီး ထပ်စမ်းကြည့်ပေးပါနော်။"
