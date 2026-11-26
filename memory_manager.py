import re
from datetime import datetime, timedelta

from ai_engine import call_json_intelligence
from config import DEFAULT_CORE_RULES, MM_TZ, RETENTION_DAYS
from database import supabase


def format_mm_timestamp(ts_str: str) -> str:
    if not ts_str:
        return ""
    try:
        dt = datetime.fromisoformat(str(ts_str).replace("Z", "+00:00"))
        return dt.astimezone(MM_TZ).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return str(ts_str)[:16]


def cleanup_old_raw_logs():
    try:
        cutoff_dt = datetime.now(MM_TZ) - timedelta(days=RETENTION_DAYS)
        supabase.table("raw_logs").delete().lt("created_at", cutoff_dt.isoformat()).execute()
    except Exception as e:
        print(f"Error cleaning up old raw_logs: {e}")


def save_chat_message(user_id: int, role: str, content: str):
    try:
        supabase.table("chat_messages").insert({
            "user_id": user_id,
            "role": role,
            "content": content,
        }).execute()
    except Exception as e:
        print(f"Error saving chat message: {e}")


def _sanitize_core_rules(raw_rules: str) -> str:
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


def get_user_profile_record(user_id: int) -> dict:
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
    clean_rule = (new_rule_line or "").strip().strip("<>").strip()
    if not clean_rule:
        return "ထည့်လိုသည့် Rule စာသားလေး ရေးပေးပါရှင်"

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
        updated_rules = f"{old_rules}\n- {clean_rule}".strip()
        updated_prefs = f"{old_prefs}\n[Core Rule v{new_version}]: {clean_rule}".strip()

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

        return f"Core Rule အသစ်ကို Version v{new_version} အဖြစ် မှတ်သားပေးလိုက်ပါပြီရှင်\n• {clean_rule}"
    except Exception as e:
        print(f"[Error updating core rule version]: {type(e).__name__}: {e}")
        return f"Rule သိမ်းရာမှာ အခက်အခဲ ရှိနေပါတယ်ရှင် — {type(e).__name__}: {str(e)[:120]}"


def get_skill_versions_summary(user_id: int) -> str:
    rec = get_user_profile_record(user_id)
    cur_v = rec.get("version") or 1
    cur_rules = (rec.get("core_rules") or DEFAULT_CORE_RULES).strip()
    lines = [f"📌 လက်ရှိ Active Core Rules (v{cur_v}):\n{cur_rules}"]

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
                f"• v{r.get('version')} ({format_mm_timestamp(r.get('created_at'))}) — {r.get('note') or 'Saved'}"
                for r in rows
            ]
            lines.append("\n🗂️ သိမ်းထားပြီးသား Version မှတ်တမ်းများ:\n" + "\n".join(hist_lines))
    except Exception:
        pass

    lines.append("\n💡 Rule အသစ်ထည့်ချင်ရင် `/setrule <rule>` လို့ ရေးထည့်နိုင်ပါတယ်ရှင်")
    return "\n".join(lines)


def evolve_user_profile(user_id: int, user_msg: str, assistant_reply: str):
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
        parsed = call_json_intelligence(update_prompt)
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
                f"[{format_mm_timestamp(m.get('created_at'))} | {m.get('role')}]: {m.get('content')}"
                for m in matched_rows[-15:]
            ]
            sections.append("--- Past Memory Matches ---\n" + "\n".join(matched_lines))

        if recent_rows:
            recent_lines = [
                f"[{format_mm_timestamp(r.get('created_at'))} | {r.get('role')}]: {r.get('content')}"
                for r in recent_rows
            ]
            sections.append("--- Recent Conversation ---\n" + "\n".join(recent_lines))

    except Exception as e:
        print(f"Error recalling chat history: {e}")

    return "\n\n".join(sections)