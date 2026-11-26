import json
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


def clean_thinzar_voice(text: str) -> str:
    if not text:
        return text
    text = text.replace("ကျွန်တော့်", "သင်ဇာ့").replace("ကျွန်တော်", "သင်ဇာ")
    text = re.sub(r"[\u0E00-\u0E7F]+", "", text)
    text = re.sub(r"\[meta:[^\]]*\]", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()


def extract_json_from_text(raw_text: str) -> dict | None:
    if not raw_text:
        return None
    cleaned = re.sub(r"```(?:json)?", "", raw_text, flags=re.IGNORECASE).replace("```", "").strip()
    match = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def call_json_intelligence(prompt: str) -> dict | None:
    json_sys = "You are a strict JSON intent & entity parser. Return ONLY valid JSON with no markdown or extra text."

    for model_name in ("gemini-3.5-flash-lite", "gemini-3.8-flash"):
        try:
            resp = gemini_client.models.generate_content(
                model=model_name,
                contents=prompt,
                config=types.GenerateContentConfig(
                    system_instruction=json_sys,
                    response_mime_type="application/json",
                    temperature=0.0,
                ),
            )
            if resp and resp.text:
                parsed = extract_json_from_text(resp.text)
                if parsed is not None:
                    return parsed
        except Exception as e:
            print(f"[JSON Intel {model_name} Error]: {e}")

    if OPENROUTER_API_KEY:
        for or_model in ("qwen/qwen3.8-27b", "google/gemma-4-31b-it:free"):
            try:
                resp = requests.post(
                    "https://openrouter.ai/api/v1/chat/completions",
                    headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}", "Content-Type": "application/json"},
                    json={
                        "model": or_model,
                        "messages": [
                            {"role": "system", "content": json_sys},
                            {"role": "user", "content": prompt},
                        ],
                        "temperature": 0.0,
                        "max_tokens": 500,
                    },
                    timeout=8,
                )
                res_json = resp.json()
                if "choices" in res_json and res_json["choices"]:
                    txt = (res_json["choices"][0]["message"].get("content") or "").strip()
                    parsed = extract_json_from_text(txt)
                    if parsed is not None:
                        return parsed
            except Exception as e:
                print(f"[JSON Intel OpenRouter {or_model} Error]: {e}")

    for g_model in ("qwen/qwen3.8-27b", "llama-3.1-8b-instant"):
        try:
            completion = groq_client.with_options(timeout=6.0).chat.completions.create(
                model=g_model,
                messages=[
                    {"role": "system", "content": json_sys},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.0,
                max_tokens=500,
            )
            raw_groq = (completion.choices[0].message.content or "").strip()
            parsed = extract_json_from_text(raw_groq)
            if parsed is not None:
                return parsed
        except Exception as e:
            print(f"[JSON Intel Groq {g_model} Error]: {e}")

    return None


def call_ai_with_3tier_fallback(
    prompt_text,
    image_bytes=None,
    mime_type=None,
    use_google_search=False,
    system_instruction: str | None = None,
    temperature: float | None = None,
):
    errors = []
    sys_inst = system_instruction or STRICT_MYANMAR_SYSTEM
    contents = (
        [types.Part.from_bytes(data=bytes(image_bytes), mime_type=mime_type or "image/jpeg"), prompt_text]
        if image_bytes is not None
        else prompt_text
    )
    tools = [types.Tool(google_search=types.GoogleSearch())] if use_google_search else None
    cfg_kwargs = {
        "system_instruction": sys_inst,
        "tools": tools,
    }
    if temperature is not None:
        cfg_kwargs["temperature"] = temperature
    config = types.GenerateContentConfig(**cfg_kwargs)

    for attempt in range(2):
        try:
            response = gemini_client.models.generate_content(
                model="gemini-3.5-flash-lite",
                contents=contents,
                config=config,
            )
            if response and response.text and response.text.strip():
                return clean_thinzar_voice(response.text.strip())
        except Exception as e:
            err_msg = f"T1({attempt+1}): {type(e).__name__} - {str(e)[:90]}"
            print(err_msg)
            errors.append(err_msg)
            if "429" in str(e) or "RESOURCE_EXHAUSTED" in str(e):
                break
            if attempt == 0:
                time.sleep(1.0)

    try:
        response = gemini_client.models.generate_content(
            model="gemini-3.8-flash",
            contents=contents,
            config=config,
        )
        if response and response.text and response.text.strip():
            return clean_thinzar_voice(response.text.strip())
    except Exception as e:
        err_msg = f"T2: {type(e).__name__} - {str(e)[:90]}"
        print(err_msg)
        errors.append(err_msg)

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
                        {"role": "system", "content": sys_inst},
                        {"role": "user", "content": prompt_text},
                    ],
                    "temperature": temperature if temperature is not None else 0.4,
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
                        return clean_thinzar_voice(txt)
                else:
                    errors.append(f"T3A({or_model}): {str(res_json)[:90]}")
            except Exception as e:
                err_msg = f"T3A({or_model}): {type(e).__name__} - {str(e)[:90]}"
                print(err_msg)
                errors.append(err_msg)

    groq_models = ["qwen/qwen3.8-27b", "llama-3.1-8b-instant"]
    for g_model in groq_models:
        try:
            completion = groq_client.with_options(timeout=12.0).chat.completions.create(
                model=g_model,
                messages=[
                    {"role": "system", "content": sys_inst},
                    {"role": "user", "content": prompt_text},
                ],
                temperature=temperature if temperature is not None else 0.4,
            )
            if completion and completion.choices and completion.choices[0].message.content:
                txt = completion.choices[0].message.content.strip()
                if txt:
                    return clean_thinzar_voice(txt)
        except Exception as e:
            err_msg = f"T3B({g_model}): {type(e).__name__} - {str(e)[:90]}"
            print(err_msg)
            errors.append(err_msg)

    debug_tail = "\n".join(errors[-3:])
    return f"လောလောဆယ် AI ဆာဗာ ခဏအလုပ်များနေပါတယ်ရှင်၊ ခဏနေပြီး ထပ်စမ်းကြည့်ပေးပါ:\n{debug_tail}"