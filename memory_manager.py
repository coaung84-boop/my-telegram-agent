import json
import re
from datetime import datetime
import requests
from google.genai import types

from config import DEFAULT_CORE_RULES, MM_TZ, OPENROUTER_API_KEY
from database import supabase
from ai_engine import gemini_client, groq_client


# --- Timestamp, Chat Memory, Skill Versioning & Protected Core Rules Helpers ---
def _format_mm_timestamp(ts_str: str) -> str:
    if not ts_str:
        return ""
    try:
        dt = datetime.fromisoformat(str(ts_str).replace("Z", "+00:00"))
        return dt.astimezone(MM_TZ).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return str(ts_str)[:16]


def save_chat_message(user_id: int, role: str, content: str):
    try:
        supabase.table("chat_messages").insert({
            "user_id": user_id,
            "role": role,
            "content": content
        }).execute()
    except Exception as e:
        print(f"Error saving chat message: {e}")


def _sanitize_core_rules(raw_rules: str) -> str:
    """အရင် DB ထဲမှာ ပုံသေစကားလုံးစည်းမျဉ်းဟောင်း ကျန်နေသေးရင် ဖယ်ရှားပေးသည်။"""
    text = (raw_rules or "").strip()
    if not text:
        return DEFAULT_CORE_RULES
    lines = []
    for line in text.splitlines():
        if "စကားအဆုံးတွင် 'နော်'" in line or "ပါနော်" in line:
            continue
        lines.append(line)
    cleaned = "\n".join(lines).strip()
    return cleaned or DEFAULT_CORE_RULES


def get_core_rule_lines(user_id: int) -> list[str]:
    """လက်ရှိ core_rules စာသားထဲမှ rule line များကို list အဖြစ် ခွဲထုတ်ပေးသည်။"""
    rec = get_user_profile_record(user_id)
    raw = (rec.get("core_rules") or DEFAULT_CORE_RULES).strip()
    lines = []
    for ln in raw.splitlines():
        cleaned = re.sub(r"^\s*[-•*]+\s*", "", ln).strip()
        if cleaned:
            lines.append(cleaned)
    return lines or [DEFAULT_CORE_RULES]


def _save_core_rules_with_version(
    user_id: int,
    new_rules_lines: list[str],
    note: str,
    status_prefix: str,
    detail_line: str,
) -> str:
    """Rule line list ကို စစ်ဆေးပြီး အရင် version ကို မှတ်တမ်းတင်ကာ version တိုးသိမ်းပေးသည်။"""
    valid_lines = [re.sub(r"^\s*[-•*]+\s*", "", x).strip() for x in (new_rules_lines or []) if str(x or "").strip()]
    if not valid_lines:
        return "Core Rule စာရင်းကို အလွတ်ထား၍ မရပါဘူးရှင်"

    try:
        rec = get_user_profile_record(user_id)
        old_version = int(rec.get("version") or 1)
        old_rules = (rec.get("core_rules") or DEFAULT_CORE_RULES).strip()
        old_summary = (rec.get("profile_summary") or "").strip()
        old_prefs = (rec.get("style_preferences") or "").strip()

        try:
            supabase.table("skill_versions").insert({
                "user_id": int(user_id),
                "version": old_version,
                "core_rules": old_rules,
                "style_preferences": old_prefs,
                "note": note,
            }).execute()
        except Exception as ver_err:
            print(f"[Skill Version Archive Error]: {ver_err}")

        new_version = old_version + 1
        updated_rules = "\n".join(f"- {ln}" for ln in valid_lines)
        updated_prefs = f"{old_prefs}\n[Core Rule v{new_version}]: {detail_line}".strip()

        supabase.table("user_profile").upsert(
            {
                "user_id": int(user_id),
                "core_rules": updated_rules,
                "version": new_version,
                "profile_summary": old_summary,
                "style_preferences": updated_prefs,
                "updated_at": datetime.now(MM_TZ).isoformat(),
            },
            on_conflict="user_id",
        ).execute()

        return f"{status_prefix} (Version v{new_version}) ဖြစ်သွားပါပြီရှင်\n• {detail_line}"
    except Exception as e:
        print(f"[Error saving core rule version]: {type(e).__name__}: {e}")
        return f"Rule သိမ်းရာမှာ အခက်အခဲ ရှိနေပါတယ်ရှင် — {type(e).__name__}: {str(e)[:120]}"


def get_user_profile_record(user_id: int) -> dict:
    """user_profile row တစ်ခုလုံး (core_rules, version, profile_summary, style_preferences) ကို ဖတ်သည်။"""
    try:
        res = (
            supabase.table("user_profile")
            .select("*")
            .eq("user_id", user_id)
            .limit(1)
            .execute()
        )
        rows = res.data or []
        if rows:
            row = rows[0]
            row["core_rules"] = _sanitize_core_rules(row.get("core_rules") or "")
            if not row.get("version"):
                row["version"] = 1
            return row
    except Exception as e:
        print(f"Error reading user_profile record: {e}")

    return {
        "user_id": user_id,
        "core_rules": DEFAULT_CORE_RULES,
        "version": 1,
        "profile_summary": "",
        "style_preferences": "",
    }


def get_user_profile(user_id: int) -> str:
    row = get_user_profile_record(user_id)
    core_rules = (row.get("core_rules") or DEFAULT_CORE_RULES).strip()
    version = row.get("version") or 1
    summary = (row.get("profile_summary") or "").strip()
    prefs = (row.get("style_preferences") or "").strip()

    parts = [f"Locked Core Rules (v{version}):\n{core_rules}"]
    if summary:
        parts.append(f"Profile Notes:\n{summary}")
    if prefs:
        parts.append(f"Style & Routing Preferences:\n{prefs}")
    return "\n\n".join(parts)


def update_core_rule_version(user_id: int, new_rule_line: str, note: str = "User rule update") -> str:
    """Core rule အသစ် ထည့်တိုင်း အရင် version ကို မဖျက်ဘဲ version တိုးပြီး သိမ်းပေးသည်။"""
    clean_rule = (new_rule_line or "").strip().strip("<>").strip()
    if not clean_rule:
        return "ထည့်လိုသည့် Rule စာသားလေး ရေးပေးပါရှင်"

    lines = get_core_rule_lines(user_id)
    lines.append(clean_rule)
    return _save_core_rules_with_version(
        user_id=user_id,
        new_rules_lines=lines,
        note=note,
        status_prefix="Core Rule အသစ်ကို",
        detail_line=clean_rule,
    )


def edit_core_rule_line(user_id: int, rule_index: int, updated_rule_text: str) -> str:
    """ရှိပြီးသား rule တစ်ခုကို ပြင်ဆင်ပြီး version တိုးသိမ်းပေးသည်။"""
    clean_rule = (updated_rule_text or "").strip().strip("<>").strip()
    if not clean_rule:
        return "ပြင်ဆင်လိုသည့် Rule စာသားလေး ရေးပေးပါရှင်"

    lines = get_core_rule_lines(user_id)
    if rule_index < 0 or rule_index >= len(lines):
        return "ပြင်ဆင်လိုသည့် Rule ကို ရှာမတွေ့ပါဘူးရှင်"

    old_line = lines[rule_index]
    lines[rule_index] = clean_rule
    return _save_core_rules_with_version(
        user_id=user_id,
        new_rules_lines=lines,
        note=f"Edit rule #{rule_index + 1}: {clean_rule[:50]}",
        status_prefix=f"Rule #{rule_index + 1} ပြင်ဆင်မှုကို",
        detail_line=f"{old_line}  ->  {clean_rule}",
    )


def delete_core_rule_line(user_id: int, rule_index: int) -> str:
    """ရှိပြီးသား rule တစ်ခုကို ဖျက်ပြီး version တိုးသိမ်းပေးသည်။"""
    lines = get_core_rule_lines(user_id)
    if len(lines) <= 1:
        return "အနည်းဆုံး Rule တစ်ခု ကျန်ရှိနေရပါမည်ရှင်"
    if rule_index < 0 or rule_index >= len(lines):
        return "ဖျက်လိုသည့် Rule ကို ရှာမတွေ့ပါဘူးရှင်"

    removed_line = lines.pop(rule_index)
    return _save_core_rules_with_version(
        user_id=user_id,
        new_rules_lines=lines,
        note=f"Delete rule #{rule_index + 1}: {removed_line[:50]}",
        status_prefix=f"Rule #{rule_index + 1} ဖျက်ခြင်းကို",
        detail_line=f"Removed: {removed_line}",
    )


def get_skill_versions_summary(user_id: int) -> str:
    rec = get_user_profile_record(user_id)
    cur_v = rec.get("version") or 1
    rule_lines = get_core_rule_lines(user_id)
    numbered_rules = "\n".join(f"{idx + 1}. {ln}" for idx, ln in enumerate(rule_lines))
    lines = [f"📌 လက်ရှိ Active Core Rules (v{cur_v}):\n{numbered_rules}"]

    try:
        hist_res = (
            supabase.table("skill_versions")
            .select("version, note, created_at")
            .eq("user_id", user_id)
            .order("version", desc=True)
            .limit(5)
            .execute()
        )
        rows = hist_res.data or []
        if rows:
            hist_lines = [
                f"• v{r.get('version')} ({_format_mm_timestamp(r.get('created_at'))}) — {r.get('note') or 'Saved'}"
                for r in rows
            ]
            lines.append("\n🗂️ သိမ်းထားပြီးသား Version မှတ်တမ်းများ:\n" + "\n".join(hist_lines))
    except Exception:
        pass

    return "\n".join(lines)


def _extract_json_from_text(raw_text: str) -> dict | None:
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


def _call_json_intelligence(prompt: str) -> dict | None:
    """စကားပြော system prompt မရောဘဲ သန့်ရှင်းတဲ့ JSON ကိုသာ ထုတ်ယူသည့် Intelligence helper (Qwen 3.8 27B fallback ပါဝင်)။"""
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
                parsed = _extract_json_from_text(resp.text)
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
                    parsed = _extract_json_from_text(txt)
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
            parsed = _extract_json_from_text(raw_groq)
            if parsed is not None:
                return parsed
        except Exception as e:
            print(f"[JSON Intel Groq {g_model} Error]: {e}")

    return None


def evolve_user_profile(user_id: int, user_msg: str, assistant_reply: str):
    """Locked core_rules ကို မထိဘဲ dynamic profile_summary နဲ့ style_preferences ကိုသာ Self-improve လုပ်သည်။"""
    try:
        rec = get_user_profile_record(user_id)
        current_profile = get_user_profile(user_id)
        update_prompt = f"""
You are updating the dynamic memory notes of the user while preserving their Locked Core Rules untouched.

Existing Profile & Locked Rules:
{current_profile}

Latest Exchange:
User: {user_msg}
Assistant: {assistant_reply[:400]}

Return ONLY valid JSON with two concise fields in Burmese (max ~150 words each):
1. "profile_summary": Key facts, work context, recurring topics, and routing clarifications. Drop outdated/contradictory trivia.
2. "style_preferences": Natural female Burmese assistant tone/style preferences learned from conversation (e.g. 'ရှင်', warm/natural phrasing, no forced sentence-ending words).

JSON format:
{{"profile_summary":"...","style_preferences":"..."}}
"""
        parsed = _call_json_intelligence(update_prompt)
        if not parsed:
            return

        summary = str(parsed.get("profile_summary") or "").strip()
        prefs = str(parsed.get("style_preferences") or "").strip()
        if not summary and not prefs:
            return

        payload = {
            "user_id": int(user_id),
            "core_rules": rec.get("core_rules") or DEFAULT_CORE_RULES,
            "version": int(rec.get("version") or 1),
            "profile_summary": summary,
            "style_preferences": prefs,
            "updated_at": datetime.now(MM_TZ).isoformat(),
        }
        try:
            supabase.table("user_profile").upsert(payload, on_conflict="user_id").execute()
        except Exception:
            supabase.table("user_profile").upsert({
                "user_id": int(user_id),
                "profile_summary": summary,
                "style_preferences": prefs,
                "updated_at": datetime.now(MM_TZ).isoformat(),
            }, on_conflict="user_id").execute()
    except Exception as e:
        print(f"Error evolving user_profile: {e}")


def extract_search_terms(query_text: str) -> list:
    terms = []
    for raw in query_text.split():
        cleaned = re.sub(r"[^\w\u1000-\u109F]", "", raw)
        if len(cleaned) >= 2:
            terms.append(cleaned)

    compact = re.sub(r"\s+", "", query_text)
    if len(compact) >= 4:
        for i in range(0, min(len(compact), 36), 4):
            chunk = compact[i:i + 5]
            if len(chunk) >= 3 and chunk not in terms:
                terms.append(chunk)

    return terms[:10]


def get_recent_history_for_router(user_id: int, limit: int = 6) -> str:
    try:
        res = (
            supabase.table("chat_messages")
            .select("role, content")
            .eq("user_id", user_id)
            .order("id", desc=True)
            .limit(limit)
            .execute()
        )
        rows = list(reversed(res.data or []))
        return "\n".join([f"{r['role']}: {r['content'][:220]}" for r in rows])
    except Exception:
        return ""


def recall_relevant_history(user_id: int, query_text: str, recent_limit: int = 8, search_limit: int = 10) -> str:
    sections = []

    profile_text = get_user_profile(user_id)
    if profile_text:
        sections.append(f"--- Learned User Profile & Locked Rules ---\n{profile_text}")

    try:
        recent_res = (
            supabase.table("chat_messages")
            .select("role, content, created_at")
            .eq("user_id", user_id)
            .order("id", desc=True)
            .limit(recent_limit)
            .execute()
        )
        recent_rows = list(reversed(recent_res.data or []))

        search_terms = extract_search_terms(query_text)
        extra_words = [w for w in re.findall(r"[\w\u1000-\u109F]{2,}", query_text) if w not in search_terms]
        search_terms = (search_terms + extra_words)[:10]

        matched_rows = []
        seen = set((r.get("role"), r.get("content"), r.get("created_at")) for r in recent_rows)

        for term in search_terms:
            search_res = (
                supabase.table("chat_messages")
                .select("role, content, created_at")
                .eq("user_id", user_id)
                .ilike("content", f"%{term}%")
                .order("id", desc=True)
                .limit(search_limit)
                .execute()
            )
            for r in reversed(search_res.data or []):
                key = (r.get("role"), r.get("content"), r.get("created_at"))
                if key not in seen:
                    seen.add(key)
                    matched_rows.append(r)

        matched_rows.sort(key=lambda x: x.get("created_at") or "")

        if matched_rows:
            matched_lines = [
                f"[{_format_mm_timestamp(m.get('created_at'))} | {m.get('role')}]: {m.get('content')}"
                for m in matched_rows[-15:]
            ]
            sections.append("--- Past Memory Matches ---\n" + "\n".join(matched_lines))

        if recent_rows:
            recent_lines = [
                f"[{_format_mm_timestamp(r.get('created_at'))} | {r.get('role')}]: {r.get('content')}"
                for r in recent_rows
            ]
            sections.append("--- Recent Conversation ---\n" + "\n".join(recent_lines))

    except Exception as e:
        print(f"Error recalling chat history: {e}")

    return "\n\n".join(sections)
    
