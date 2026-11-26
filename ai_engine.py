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

gemini_client = genai.Client(api_key=GEMINI_API_KEY)
groq_client = Groq(api_key=GROQ_API_KEY)


def _clean_thinzar_voice(text: str) -> str:
    """Fallback model များက ယောက်ျားလေးအသုံးအနှုန်း သို့မဟုတ် ထိုင်းစာလုံး ရောမိရင် သန့်ရှင်းပေးသည်။"""
    if not text:
        return text
    text = text.replace("ကျွန်တော့်", "သင်ဇာ့").replace("ကျွန်တော်", "သင်ဇာ")
    text = re.sub(r"[\u0E00-\u0E7F]+", "", text)
    text = re.sub(r"\[meta:[^\]]*\]", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()


# --- Resilient Multi-Tier AI Fallback Helper (Gemini -> OpenRouter + Qwen 3.8 27B -> Groq + Qwen 3.8 27B) ---
def call_ai_with_3tier_fallback(prompt_text, image_bytes=None, mime_type=None, use_google_search=False):
    errors = []
    contents = (
        [types.Part.from_bytes(data=bytes(image_bytes), mime_type=mime_type or 'image/jpeg'), prompt_text]
        if image_bytes else prompt_text
    )
    tools = [types.Tool(google_search=types.GoogleSearch())] if use_google_search else None
    config = types.GenerateContentConfig(
        system_instruction=STRICT_MYANMAR_SYSTEM,
        tools=tools,
    )

    # Tier 1: Gemini 3.5 Flash-Lite
    for attempt in range(2):
        try:
            response = gemini_client.models.generate_content(
                model="gemini-3.5-flash-lite",
                contents=contents,
                config=config
            )
            if response and response.text and response.text.strip():
                return _clean_thinzar_voice(response.text.strip())
        except Exception as e:
            err_msg = f"T1({attempt+1}): {type(e).__name__} - {str(e)[:90]}"
            print(err_msg)
            errors.append(err_msg)
            if "429" in str(e) or "RESOURCE_EXHAUSTED" in str(e):
                break
            if attempt == 0:
                time.sleep(1.0)

    # Tier 2: Gemini 3.8 Flash
    try:
        response = gemini_client.models.generate_content(
            model="gemini-3.8-flash",
            contents=contents,
            config=config
        )
        if response and response.text and response.text.strip():
            return _clean_thinzar_voice(response.text.strip())
    except Exception as e:
        err_msg = f"T2: {type(e).__name__} - {str(e)[:90]}"
        print(err_msg)
        errors.append(err_msg)

    # Tier 3A: OpenRouter (Qwen 3.8 27B + Free Fallbacks)
    if OPENROUTER_API_KEY:
        openrouter_models = [
            "qwen/qwen3.8-27b",
            "google/gemma-4-31b-it:free",
            "meta-llama/llama-3.3-70b-instruct:free",
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
                        {"role": "user", "content": prompt_text}
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

    # Tier 3B: Groq (Qwen 3.8 27B + Active Fallback)
    groq_models = ["qwen/qwen3.8-27b", "llama-3.1-8b-instant"]
    for g_model in groq_models:
        try:
            completion = groq_client.with_options(timeout=12.0).chat.completions.create(
                model=g_model,
                messages=[
                    {"role": "system", "content": STRICT_MYANMAR_SYSTEM},
                    {"role": "user", "content": prompt_text}
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