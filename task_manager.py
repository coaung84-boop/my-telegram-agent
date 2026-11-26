import json
import re
from datetime import datetime, timedelta

from ai_engine import call_ai_with_3tier_fallback, call_json_intelligence
from config import MM_TZ
from database import supabase
from memory_manager import format_mm_timestamp


def has_maung_trigger_context(text: str) -> bool:
    t = (text or "").lower()
    return any(k in t for k in [
        "ဆေး", "ဆေးသောက်", "medicine", "pill", "tablet",
        "သား", "သမီး", "ကလေး", "မိသားစု", "family", "kid", "child", "son", "daughter",
    ])


def get_task_tone_instruction(task_title: str) -> str:
    if has_maung_trigger_context(task_title):
        return (
            "ဒီ Task/Reminder ဟာ ဆေးသောက်ရန် သို့မဟုတ် မိသားစု/ကလေးများနှင့် ပတ်သက်နေသောကြောင့် "
            "သင်ဇာ (Thinzar) ဟန်ဖြင့် နွေးနွေးထွေးထွေး ဂရုတစိုက် 'မောင်' ဟု သုံးပြီး သတိပေးပါ။"
        )
    return (
        "ဒီ Task/Reminder ဟာ ဆေးသောက်ရန် သို့မဟုတ် မိသားစု/ကလေးများနှင့် မသက်ဆိုင်သောကြောင့် "
        "'မောင်' ဟု လုံးဝ မသုံးဘဲ သာမန် သင်ဇာ (Thinzar) ဟန်ဖြင့်သာ ပုံမှန် သတိပေးပါ။"
    )


def _extract_reminder_minutes(text: str) -> int | None:
    t = (text or "").strip().lower()
    m_min = re.search(r"(\d+)\s*(?:min|mins|minute|minutes|မိနစ်)", t)
    if m_min:
        return max(1, int(m_min.group(1)))
    m_hr = re.search(r"(\d+(?:\.\d+)?)\s*(?:hr|hrs|hour|hours|နာရီ)", t)
    if m_hr:
        return max(1, int(round(float(m_hr.group(1)) * 60)))
    return None


def _parse_explicit_datetime_mm(text: str, now_mm: datetime) -> datetime | None:
    raw = (text or "").strip()
    t = raw.lower()

    m_iso = re.search(r"\b(20\d{2}-\d{2}-\d{2})[T\s](\d{1,2}:\d{2})(?::\d{2})?\b", raw)
    if m_iso:
        try:
            naive = datetime.strptime(f"{m_iso.group(1)} {m_iso.group(2)}", "%Y-%m-%d %H:%M")
            return naive.replace(tzinfo=MM_TZ)
        except Exception:
            pass

    m_slash = re.search(r"\b(\d{1,2})/(\d{1,2})/(20\d{2})\s+(\d{1,2}:\d{2})\b", raw)
    if m_slash:
        try:
            naive = datetime.strptime(
                f"{m_slash.group(3)}-{int(m_slash.group(2)):02d}-{int(m_slash.group(1)):02d} {m_slash.group(4)}",
                "%Y-%m-%d %H:%M",
            )
            return naive.replace(tzinfo=MM_TZ)
        except Exception:
            pass

    target_date = now_mm.date()
    if "မနက်ဖြန်" in t or "tomorrow" in t:
        target_date = (now_mm + timedelta(days=1)).date()
    elif "သန်ဘက်ခါ" in t:
        target_date = (now_mm + timedelta(days=2)).date()
    elif "ဒီနေ့" in t or "ယနေ့" in t or "today" in t:
        target_date = now_mm.date()

    hour = None
    minute = 0

    m_hm = re.search(r"\b([01]?\d|2[0-3]):([0-5]\d)\b", t)
    if m_hm:
        hour = int(m_hm.group(1))
        minute = int(m_hm.group(2))
    else:
        m_mm_time = re.search(r"(?:ည|မနက်|နေ့လယ်|ညနေ|နံနက်)?\s*(\d{1,2})\s*နာရီ(?:\s*(\d{1,2})\s*မိနစ်)?", raw)
        if m_mm_time:
            h_raw = int(m_mm_time.group(1))
            minute = int(m_mm_time.group(2)) if m_mm_time.group(2) else 0
            if "ည" in raw and 1 <= h_raw <= 11:
                hour = h_raw + 12
            elif "ညနေ" in raw and 1 <= h_raw <= 6:
                hour = h_raw + 12
            elif "နေ့လယ်" in raw and 1 <= h_raw <= 4:
                hour = h_raw + 12 if h_raw < 12 else h_raw
            else:
                hour = h_raw % 24

    if hour is not None and 0 <= hour <= 23 and 0 <= minute <= 59:
        cand = datetime(
            target_date.year, target_date.month, target_date.day,
            hour, minute, tzinfo=MM_TZ,
        )
        if cand <= now_mm and "မနက်ဖြန်" not in t and "tomorrow" not in t and "သန်ဘက်ခါ" not in t:
            cand = cand + timedelta(days=1)
        return cand

    return None


def _clean_task_title(raw_text: str) -> str:
    title = (raw_text or "").strip()
    title = re.sub(r"^(?:/addtask|/todo|/task|task\s*:|todo\s*:)\s*", "", title, flags=re.IGNORECASE)
    title = re.sub(
        r"\b(?:in\s+\d+\s*(?:min|mins|minute|minutes|hr|hrs|hour|hours)|at\s+\d{1,2}:\d{2}|today|tomorrow)\b",
        "",
        title,
        flags=re.IGNORECASE,
    )
    title = re.sub(
        r"(?:ဒီနေ့|ယနေ့|မနက်ဖြန်|သန်ဘက်ခါ|ည|မနက်|နေ့လယ်|ညနေ|နံနက်)?\s*\d{1,2}\s*နာရီ(?:\s*\d{1,2}\s*မိနစ်)?",
        "",
        title,
    )
    title = re.sub(r"\d+\s*(?:မိနစ်|နာရီ)\s*(?:အကြာ|ကြာရင်|မှာ)?", "", title)
    title = re.sub(
        r"(?:သတိပေးပါ|သတိပေး|remind\s*me(?:\s*to)?|reminder(?:\s*to)?|ထည့်ပေးပါ|ထည့်ပါ|task\s*လုပ်ပေးပါ|လုပ်ရန်|မှာ|ကို|နော်|ရှင်)",
        "",
        title,
        flags=re.IGNORECASE,
    )
    title = re.sub(r"\s{2,}", " ", title).strip(" -–—:.,")
    return title or (raw_text or "").strip()


def parse_task_details_with_ai(raw_text: str) -> dict:
    now_mm = datetime.now(MM_TZ)
    now_str = now_mm.strftime("%Y-%m-%d %H:%M (%A)")

    rel_mins = _extract_reminder_minutes(raw_text)
    if rel_mins is not None:
        due_dt = now_mm + timedelta(minutes=rel_mins)
        return {
            "title": _clean_task_title(raw_text),
            "due_at": due_dt.isoformat(),
            "reminder_at": due_dt.isoformat(),
            "priority": "high" if has_maung_trigger_context(raw_text) else "normal",
            "notes": "",
        }

    explicit_dt = _parse_explicit_datetime_mm(raw_text, now_mm)
    if explicit_dt is not None:
        return {
            "title": _clean_task_title(raw_text),
            "due_at": explicit_dt.isoformat(),
            "reminder_at": explicit_dt.isoformat(),
            "priority": "high" if has_maung_trigger_context(raw_text) else "normal",
            "notes": "",
        }

    prompt = f"""
Current Myanmar Time (UTC+6:30): {now_str}
User task/reminder input: "{raw_text}"

Extract task details and return ONLY valid JSON:
{{
  "title": "concise task title in user's language (Burmese/English)",
  "due_at": "ISO-8601 string with +06:30 offset or null if no time/date mentioned",
  "reminder_at": "ISO-8601 string with +06:30 offset or null if no reminder time mentioned",
  "priority": "high | normal | low",
  "notes": "optional short note"
}}

Rules:
- If user says "in X minutes/hours" or "X မိနစ်/နာရီ", add to current time {now_mm.isoformat()}.
- If user mentions a clock time like "8pm", "ည ၈ နာရီ", "09:30", convert accurately to +06:30 ISO format.
- If no time is specified, set due_at and reminder_at to null.
"""
    parsed = call_json_intelligence(prompt)
    if parsed and parsed.get("title"):
        return parsed

    return {
        "title": _clean_task_title(raw_text),
        "due_at": None,
        "reminder_at": None,
        "priority": "normal",
        "notes": "",
    }


def add_task_record(user_id: int, raw_text: str) -> dict:
    details = parse_task_details_with_ai(raw_text)
    title = (details.get("title") or raw_text).strip()
    due_at = details.get("due_at")
    reminder_at = details.get("reminder_at") or due_at
    priority = details.get("priority") if details.get("priority") in ("high", "normal", "low") else "normal"
    notes = (details.get("notes") or "").strip()

    row_payload = {
        "user_id": int(user_id),
        "title": title,
        "status": "pending",
        "priority": priority,
        "due_at": due_at,
        "reminder_at": reminder_at,
        "reminded": False,
        "notes": notes,
    }
    try:
        res = supabase.table("tasks").insert(row_payload).execute()
        inserted = (res.data or [row_payload])[0]
        return {"ok": True, "task": inserted}
    except Exception as e:
        print(f"Error inserting task: {e}")
        return {"ok": False, "error": str(e)}


def list_tasks_records(user_id: int, include_done: bool = False, limit: int = 20) -> list:
    try:
        q = supabase.table("tasks").select("*").eq("user_id", user_id)
        if not include_done:
            q = q.neq("status", "done")
        res = q.order("id", desc=False).limit(limit).execute()
        return res.data or []
    except Exception as e:
        print(f"Error listing tasks: {e}")
        return []


def complete_task_record(user_id: int, task_id: int) -> dict:
    try:
        res = (
            supabase.table("tasks")
            .update({"status": "done", "completed_at": datetime.now(MM_TZ).isoformat()})
            .eq("user_id", user_id)
            .eq("id", task_id)
            .execute()
        )
        if res.data:
            return {"ok": True, "task": res.data[0]}
        return {"ok": False, "error": "Task ID မတွေ့ပါဘူးရှင်"}
    except Exception as e:
        print(f"Error completing task: {e}")
        return {"ok": False, "error": str(e)}


def delete_task_record(user_id: int, task_id: int) -> dict:
    try:
        res = (
            supabase.table("tasks")
            .delete()
            .eq("user_id", user_id)
            .eq("id", task_id)
            .execute()
        )
        if res.data:
            return {"ok": True}
        return {"ok": False, "error": "Task ID မတွေ့ပါဘူးရှင်"}
    except Exception as e:
        print(f"Error deleting task: {e}")
        return {"ok": False, "error": str(e)}


def format_tasks_list_message(tasks: list, title_header: str = "📋 လက်ရှိ Task / Reminder စာရင်း") -> str:
    if not tasks:
        return f"{title_header}\n\nလက်ရှိ pending ဖြစ်နေတဲ့ Task မရှိသေးပါဘူးရှင်။"

    lines = [title_header, ""]
    for t in tasks:
        tid = t.get("id")
        status = t.get("status", "pending")
        icon = "✅" if status == "done" else ("🔴" if t.get("priority") == "high" else "🔹")
        t_title = t.get("title", "")
        time_str = ""
        dt_val = t.get("reminder_at") or t.get("due_at")
        if dt_val:
            time_str = f" ⏰ `{format_mm_timestamp(dt_val)}`"
        strike = f"~{t_title}~" if status == "done" else f"**{t_title}**"
        lines.append(f"{icon} `#{tid}` {strike}{time_str}")

    lines.append("\nပြီးစီးရင် `/done <id>`၊ ဖျက်ချင်ရင် `/deltask <id>` လို့ ရိုက်နိုင်ပါတယ်ရှင်။")
    return "\n".join(lines)


def generate_proactive_reminder_text(task: dict) -> str:
    title = task.get("title", "")
    notes = task.get("notes", "")
    tone_inst = get_task_tone_instruction(f"{title} {notes}")
    prompt = f"""
{tone_inst}
အခုအချိန်မှာ အောက်ပါ Task / Reminder ကို အသုံးပြုသူထံ သဘာဝကျကျ၊ ကြင်နာနွေးထွေးစွာ အသိပေး သတိပေးစာ ရေးပေးပါ:
- Task: {title}
- Notes: {notes or '-'}

စာကြောင်း ၁ ကြောင်း မှ ၂ ကြောင်းခန့်သာ တိုတိုရှင်းရှင်းနှင့် သဘာဝကျကျ ရေးပါ။
"""
    return call_ai_with_3tier_fallback(prompt)