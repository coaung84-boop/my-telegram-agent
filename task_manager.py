import re
from datetime import datetime, timedelta

from config import MM_TZ, MYANMAR_WEEKDAYS, RETENTION_DAYS
from database import supabase
from memory_manager import _call_json_intelligence


def _detect_task_category_and_priority(text: str, reason: str | None = None) -> tuple[str, str]:
    """Task စာသားပေါ်မူတည်၍ category (health/family/work/general) နှင့် priority (urgent/normal) ခွဲခြားသည်။"""
    combined = f"{text or ''} {reason or ''}".lower()

    if any(k in combined for k in
           ["ဆေး", "medicine", "pill", "tablet", "ဆေးသောက်", "ကျန်းမာရေး", "health", "ဆရာဝန်", "doctor"]):
        category = "health"
    elif any(k in combined for k in
             ["မိသားစု", "family", "ကလေး", "ကလေးတွေ", "သား", "သမီး", "child", "children", "kid", "kids", "ကျောင်း",
              "school", "ကျောင်းလခ"]):
        category = "family"
    elif any(k in combined for k in
             ["report", "vendor", "branch", "meeting", "contract", "order", "parcel", "delivery", "လုပ်ငန်း", "ရုံး",
              "စာရင်း", "အစည်းအဝေး", "customer", "client", "fulfillment"]):
        category = "work"
    else:
        category = "general"

    is_urgent = any(
        k in combined
        for k in ["အရေးကြီး", "urgent", "asap", "ချက်ချင်း", "မဖြစ်မနေ", "important", "priority", "ဆေးသောက်"]
    )
    priority = "urgent" if is_urgent else "normal"
    return category, priority


def _pack_task_reason(reason: str | None, category: str, priority: str) -> str:
    """Column မရှိသေးသည့် fallback အတွက် category/priority ကို reason ထဲ ထည့်သိမ်းသည်။"""
    clean_r = re.sub(r"\[meta:[^\]]*\]", "", str(reason or "")).strip()
    meta_tag = f"[meta:cat={category},prio={priority}]"
    return f"{clean_r} {meta_tag}".strip() if clean_r else meta_tag


def _unpack_task_meta(task_row: dict) -> tuple[str | None, str, str]:
    """Task row မှ user reason၊ category နှင့် priority တို့ကို ခွဲထုတ်ပေးသည်။"""
    raw_reason = str(task_row.get("reason") or "").strip()
    title = str(task_row.get("title") or "")
    default_cat, default_prio = _detect_task_category_and_priority(title, raw_reason)

    cat = str(task_row.get("category") or "").strip().lower() or default_cat
    prio = str(task_row.get("priority") or "").strip().lower() or default_prio

    m = re.search(r"\[meta:cat=([a-z_]+),prio=([a-z_]+)\]", raw_reason)
    if m:
        if not task_row.get("category"):
            cat = m.group(1)
        if not task_row.get("priority"):
            prio = m.group(2)

    clean_reason = re.sub(r"\[meta:[^\]]*\]", "", raw_reason).strip() or None
    return clean_reason, cat, prio


def _category_label_mm(category: str) -> str:
    mapping = {
        "health": "🩺 ကျန်းမာရေး",
        "family": "👨‍👩‍👧 မိသားစု",
        "work": "💼 လုပ်ငန်း",
        "general": "📌 အထွေထွေ",
    }
    return mapping.get(category, "📌 အထွေထွေ")


def _priority_badge_mm(priority: str) -> str:
    return "🔥 အရေးကြီး" if priority == "urgent" else ""


def _is_health_or_family_task(text: str, reason: str | None = None) -> bool:
    """ဆေးသောက်ရန် သို့မဟုတ် မိသားစု/ကလေးတွေနဲ့ ပတ်သက်တဲ့ task/reminder ဟုတ်မဟုတ် စစ်သည်။"""
    combined = f"{text or ''} {reason or ''}".lower()
    keywords = [
        "ဆေး", "medicine", "pill", "tablet", "ဆေးသောက်",
        "မိသားစု", "family", "ကလေး", "ကလေးတွေ", "သား", "သမီး", "child", "children", "kid", "kids",
        "ကျောင်း", "school"
    ]
    return any(k in combined for k in keywords)


def cleanup_old_done_tasks():
    """ပြီးသွားသော (status='done') task များကို ရက် ၃၀ (၁ လ) ကျော်လျှင် storage သက်သာစေရန် ဖျက်သည်။"""
    try:
        cutoff_dt = datetime.now(MM_TZ) - timedelta(days=RETENTION_DAYS)
        res = supabase.table("tasks").select("id, status, updated_at, created_at, remind_at").eq("status",
                                                                                                 "done").execute()
        for t in (res.data or []):
            ts_raw = t.get("updated_at") or t.get("remind_at") or t.get("created_at")
            if not ts_raw:
                continue
            try:
                dt_obj = datetime.fromisoformat(str(ts_raw).replace("Z", "+00:00"))
                dt_mm = dt_obj.replace(tzinfo=MM_TZ) if dt_obj.tzinfo is None else dt_obj.astimezone(MM_TZ)
                if dt_mm < cutoff_dt:
                    supabase.table("tasks").delete().eq("id", t["id"]).execute()
            except Exception:
                pass
    except Exception as e:
        print(f"Error cleaning up old done tasks: {e}")


# --- Pure Python Myanmar Calendar Helper (Primarily for Tasks & Calendar Queries) ---
def _julian_day_from_gregorian(year: int, month: int, day: int) -> int:
    a = (14 - month) // 12
    y = year + 4800 - a
    m = month + 12 * a - 3
    return day + (153 * m + 2) // 5 + 365 * y + y // 4 - y // 100 + y // 400 - 32045


def _calculate_myanmar_date(dt_mm: datetime) -> dict:
    """Pure Python Myanmar Calendar calculation (Julian Day based)."""
    jdn = _julian_day_from_gregorian(dt_mm.year, dt_mm.month, dt_mm.day)
    anchor_jdn = _julian_day_from_gregorian(2026, 11, 10)  # 1388 ME Tazaundaing Full Moon (15 waxing)
    delta_days = jdn - anchor_jdn

    month_names = [
        "တန်ခူး",
        "ကဆုန်",
        "နယုန်",
        "ဝါဆို",
        "ဝါခေါင်",
        "တော်သလင်း",
        "သီတင်းကျွတ်",
        "တန်ဆောင်မုန်း",
        "နတ်တော်",
        "ပြာသို",
        "တပို့တွဲ",
        "တပေါင်း",
    ]
    month_days = [30, 29, 30, 29, 30, 29, 30, 29, 30, 29, 30, 30]

    cur_year = 1388
    cur_m_idx = 7
    cur_day = 15 + delta_days

    while cur_day > month_days[cur_m_idx]:
        cur_day -= month_days[cur_m_idx]
        cur_m_idx += 1
        if cur_m_idx >= 12:
            cur_m_idx = 0
            cur_year += 1

    while cur_day < 1:
        cur_m_idx -= 1
        if cur_m_idx < 0:
            cur_m_idx = 11
            cur_year -= 1
        cur_day += month_days[cur_m_idx]

    m_day_int = int(cur_day)
    m_len = month_days[cur_m_idx]

    if m_day_int <= 15:
        moon_phase = "လဆန်း"
        fortnight_day = m_day_int
    else:
        moon_phase = "လဆုတ်"
        fortnight_day = m_day_int - 15

    is_full_moon = (m_day_int == 15)
    is_new_moon = (m_day_int == m_len)
    is_sabbath = (fortnight_day in [8, 15]) or is_full_moon or is_new_moon

    wd = dt_mm.weekday()
    is_yatyaza = (
            (cur_m_idx in [0, 3, 6, 9] and wd in [1, 5]) or
            (cur_m_idx in [1, 4, 7, 10] and wd in [3, 6]) or
            (cur_m_idx in [2, 5, 8, 11] and wd in [2, 4])
    )
    is_pyathada = (
            (cur_m_idx in [0, 3, 6, 9] and wd == 5) or
            (cur_m_idx in [1, 4, 7, 10] and wd in [1, 2]) or
            (cur_m_idx in [2, 5, 8, 11] and wd in [0, 3])
    )

    return {
        "myanmar_year": cur_year,
        "month_name": month_names[cur_m_idx],
        "moon_phase": moon_phase,
        "fortnight_day": fortnight_day,
        "is_full_moon": is_full_moon,
        "is_new_moon": is_new_moon,
        "is_sabbath": is_sabbath,
        "is_yatyaza": is_yatyaza,
        "is_pyathada": is_pyathada,
    }


def get_myanmar_calendar_context(dt_mm: datetime = None) -> str:
    """လက်ရှိ မြန်မာစံတော်ချိန်အတွက် ပြက္ခဒိန်အချက်အလက်ကို စုစည်းပေးသည်။"""
    now_mm = dt_mm or datetime.now(MM_TZ)
    weekday_mm = MYANMAR_WEEKDAYS[now_mm.weekday()]
    gregorian_str = now_mm.strftime(f"%Y-%m-%d ({weekday_mm}) %H:%M MMT")

    info = _calculate_myanmar_date(now_mm)
    if info["is_full_moon"]:
        lunar_str = f"{info['month_name']} လပြည့်နေ့"
    elif info["is_new_moon"]:
        lunar_str = f"{info['month_name']} လကွယ်နေ့"
    else:
        lunar_str = f"{info['month_name']} {info['moon_phase']} {info['fortnight_day']} ရက်"

    return "\n".join([
        f"Gregorian Date/Time: {gregorian_str}",
        f"Myanmar Date: ကောဇာသက္ကရာဇ် {info['myanmar_year']} ခုနှစ်၊ {lunar_str}",
        f"Sabbath (ဥပုသ်): {'ဟုတ်ပါသည်' if info['is_sabbath'] else 'မဟုတ်ပါ'}",
        f"Yatyaza (ရက်ရာဇာ): {'ဟုတ်ပါသည်' if info['is_yatyaza'] else 'မဟုတ်ပါ'}",
        f"Pyathada (ပြဿဒါး): {'ဟုတ်ပါသည်' if info['is_pyathada'] else 'မဟုတ်ပါ'}",
    ])


# --- Single Smart Intelligence Classifier (TASK / GROUP / ADVICE / GENERAL / WEB_SEARCH + Multi-Task) ---
def _extract_weekday_target(text: str) -> int | None:
    t = text.lower()
    mapping = {
        0: ["တနင်္လာ", "monday", "mon"],
        1: ["အင်္ဂါ", "tuesday", "tue"],
        2: ["ဗုဒ္ဓဟူး", "wednesday", "wed"],
        3: ["ကြာသပတေး", "thursday", "thu"],
        4: ["သောကြာ", "friday", "fri"],
        5: ["စနေ", "saturday", "sat"],
        6: ["တနင်္ဂနွေ", "sunday", "sun"],
    }
    for idx, keys in mapping.items():
        if any(k in t for k in keys):
            return idx
    return None


def _parse_time_parts_mmt(text: str) -> tuple[int, int] | None:
    mm_digits = str.maketrans("၀၁၂၃၄၅၆၇၈၉", "0123456789")
    norm = text.translate(mm_digits)
    lower = text.lower()

    hm = re.search(r"\b(\d{1,2}):(\d{2})\b", norm)
    if hm:
        h, m = int(hm.group(1)), int(hm.group(2))
        if 0 <= h <= 23 and 0 <= m <= 59:
            return h, m

    hr = re.search(r"(\d{1,2})\s*(?:နာရီ|hour|hr)", norm, re.IGNORECASE)
    if not hr:
        return None

    h = int(hr.group(1))
    after_hr = norm[hr.end():hr.end() + 14]
    m = 0
    if "ခွဲ" in after_hr or "half" in after_hr.lower():
        m = 30
    else:
        min_m = re.search(r"(\d{1,2})\s*(?:မိနစ်|min)", after_hr, re.IGNORECASE)
        if min_m:
            m = int(min_m.group(1))

    if ("ညနေ" in text or "ည" in text or "pm" in lower) and h < 12:
        h += 12
    elif ("မနက်" in text or "am" in lower) and h == 12:
        h = 0

    if 0 <= h <= 23 and 0 <= m <= 59:
        return h, m
    return None


def _last_day_of_month(year: int, month: int) -> int:
    if month == 12:
        return 31
    next_month_first = datetime(year, month + 1, 1, tzinfo=MM_TZ)
    return (next_month_first - timedelta(days=1)).day


def parse_reminder_datetime_mmt(text: str) -> datetime | None:
    now_mm = datetime.now(MM_TZ)
    mm_digits = str.maketrans("၀၁၂၃၄၅၆၇၈၉", "0123456789")
    norm_text = text.translate(mm_digits)
    lower = text.lower()

    # 1) Relative minutes ("နောက် ၁၅ မိနစ်")
    rel_min = re.search(r"(?:နောက်\s*)?(\d{1,3})\s*(?:မိနစ်|minute|min)s?", norm_text, re.IGNORECASE)
    if rel_min and "နာရီ" not in norm_text:
        mins = int(rel_min.group(1))
        if 1 <= mins <= 1440:
            return now_mm + timedelta(minutes=mins)

    # 2) Relative days ("နောက် ၂ ရက်နေရင်" / "နောက် ၃ ရက်")
    rel_days_match = re.search(r"နောက်\s*(\d{1,2})\s*ရက်(?:\s*(?:နေရင်|အကြာ|မှာ))?", norm_text)
    parts = _parse_time_parts_mmt(text)

    if rel_days_match and not parts:
        days_add = int(rel_days_match.group(1))
        if 1 <= days_add <= 365:
            target_d = (now_mm + timedelta(days=days_add)).date()
            default_h = 9 if now_mm.hour >= 9 else max(9, now_mm.hour)
            return datetime(target_d.year, target_d.month, target_d.day, default_h, 0, 0, tzinfo=MM_TZ)

    if not parts:
        return None
    hour, minute = parts

    target_date = now_mm.date()
    target_wd = _extract_weekday_target(text)

    if rel_days_match:
        days_add = int(rel_days_match.group(1))
        if 1 <= days_add <= 365:
            target_date = (now_mm + timedelta(days=days_add)).date()
    elif "လကုန်" in text:
        end_d = _last_day_of_month(now_mm.year, now_mm.month)
        target_date = datetime(now_mm.year, now_mm.month, end_d, tzinfo=MM_TZ).date()
        if target_date < now_mm.date():
            next_m = 1 if now_mm.month == 12 else now_mm.month + 1
            next_y = now_mm.year + 1 if now_mm.month == 12 else now_mm.year
            end_d = _last_day_of_month(next_y, next_m)
            target_date = datetime(next_y, next_m, end_d, tzinfo=MM_TZ).date()
    elif target_wd is not None:
        days_ahead = (target_wd - now_mm.weekday()) % 7
        candidate_check = datetime(now_mm.year, now_mm.month, now_mm.day, hour, minute, 0, tzinfo=MM_TZ) + timedelta(
            days=days_ahead)
        if days_ahead == 0 and candidate_check <= now_mm:
            days_ahead = 7
        target_date = (now_mm + timedelta(days=days_ahead)).date()
    elif "မနက်ဖြန်" in text or "tomorrow" in lower:
        target_date = (now_mm + timedelta(days=1)).date()

    candidate = datetime(target_date.year, target_date.month, target_date.day, hour, minute, 0, tzinfo=MM_TZ)
    if (
            target_wd is None
            and not rel_days_match
            and "လကုန်" not in text
            and candidate < (now_mm - timedelta(minutes=1))
            and "မနက်ဖြန်" not in text
            and "tomorrow" not in lower
    ):
        candidate += timedelta(days=1)
    return candidate


def _compute_next_repeat_datetime(current_rem_dt: datetime, repeat_type: str, now_mm: datetime) -> datetime | None:
    """Repeat task (daily / weekly / school_days) ပြီးသွားလျှင် နောက်တစ်ကြိမ် remind_at ကို တွက်ပေးသည်။"""
    r_type = (repeat_type or "once").strip().lower()
    if r_type not in {"daily", "weekly", "school_days"}:
        return None

    next_dt = current_rem_dt
    if r_type == "daily":
        next_dt = current_rem_dt + timedelta(days=1)
        while next_dt <= now_mm:
            next_dt += timedelta(days=1)
    elif r_type == "weekly":
        next_dt = current_rem_dt + timedelta(days=7)
        while next_dt <= now_mm:
            next_dt += timedelta(days=7)
    elif r_type == "school_days":
        next_dt = current_rem_dt + timedelta(days=1)
        while next_dt.weekday() >= 5 or next_dt <= now_mm:
            next_dt += timedelta(days=1)

    return next_dt


def _clean_task_title_fallback(raw_text: str) -> str:
    t = raw_text.strip()
    t = re.sub(r"^(?:မဟုတ်ဘူးလေ|မဟုတ်ဘူး|Task\s*[-–—:]?)", "", t, flags=re.IGNORECASE)
    t = re.sub(r"(?:အပတ်စဉ်|နေ့စဉ်|ကျောင်းဖွင့်ရက်|လကုန်ရက်|လကုန်|မြန်မာ\s*calendar|တိုင်း)", "", t,
               flags=re.IGNORECASE)
    t = re.sub(
        r"(?:sunday|monday|tuesday|wednesday|thursday|friday|saturday|တနင်္လာ|အင်္ဂါ|ဗုဒ္ဓဟူး|ကြာသပတေး|သောကြာ|စနေ|တနင်္ဂနွေ)",
        "", t, flags=re.IGNORECASE)
    t = re.sub(r"(?:မနက်|ညနေ|ည|နေ့လယ်|နောက်\s*)?[၀-၉\d]+\s*(?:နာရီ|hour|hr)(?:\s*(?:ခွဲ|[၀-၉\d]+\s*မိနစ်))?", "", t,
               flags=re.IGNORECASE)
    t = re.sub(r"(?:နောက်\s*)?[၀-၉\d]+\s*(?:ရက်|မိနစ်|minute|min)s?\s*(?:နေရင်|မှာ|အကြာမှာ)?", "", t,
               flags=re.IGNORECASE)
    t = re.sub(r"\b\d{1,2}:\d{2}\b", "", t)
    t = re.sub(
        r"(?:remind\s*လုပ်ပေးပါ|remind\s*ပေးပါ|သတိပေးပါ|သတိပေး|ပြောင်းပေးပါ|ဖျက်ပေးပါ|ဖျက်လိုက်ပါ|remind|ပြောတယ်|မှာ)",
        "", t, flags=re.IGNORECASE)
    t = re.sub(r"\s+", " ", t).strip(" -–—၊။")
    return t or raw_text.strip()


def smart_route_and_parse(
        user_message: str,
        available_groups: list,
        recent_ctx: str = "",
        user_pref_ctx: str = "",
) -> dict:
    now_mm = datetime.now(MM_TZ)
    cal_info = get_myanmar_calendar_context(now_mm)
    groups_str = ", ".join(available_groups[:25]) if available_groups else "None"

    prompt = f"""
Understand the user's intent naturally from meaning and conversation context (not rigid keywords).
Calendar Context (especially useful for tasks/reminders):
{cal_info}

Available Telegram Groups: [{groups_str}]

User Preferences & Locked Rules:
{user_pref_ctx[:450] or "None"}

Recent Conversation:
{recent_ctx or "None"}

Latest User Message: "{user_message}"

Classify into ONE primary intent:
- "TASK": Setting, rescheduling/editing, deleting, completing, or listing personal reminders or to-dos, or correcting a task entry (including multiple tasks in one message).
- "GROUP": Asking about anything discussed/reported/shared in Telegram groups, asking for a group summary/report, or correcting a misrouted group query.
- "ADVICE": Seeking recommendations, options, how-to guidance, or decision support.
- "GENERAL": Translating, drafting, explaining concepts, recalling personal chat/profile, or casual chat.
- "WEB_SEARCH": Looking up external/live/current facts, news, sports/scores, weather, prices, or web info not from private/group logs.

If intent is "WEB_SEARCH", also output:
- "search_query": A self-contained search query combining Recent Conversation context if the user says a follow-up like "Yes, ရှာပေးပါ" or "အသေးစိတ် ရှာပေးပါ".
- "english_search_query": A concise, high-accuracy English search query for live facts/sports/news/prices/schedules (e.g. "Chelsea next match schedule November 2026"), or null if already in English.

If intent is "TASK", also extract:
- "task_action": "add" | "edit" | "delete" | "done" | "list" | "cancel_last"
- "target_task_query": If editing/rescheduling/deleting/completing a specific existing task by name/topic, put the existing task keyword here (e.g. "ဆေးသောက်", "ဖုန်းဆက်"), else null.
- "tasks": list of task objects if adding one or more tasks: [{{"clean_title": "...", "reason": null, "repeat_type": "once|daily|weekly|school_days", "remind_iso": "YYYY-MM-DDTHH:MM:SS+06:30 or null"}}]
- "clean_title": pure action in Burmese without time/schedule/reason/filler words (or new title if editing)
- "reason": short reason if stated, else null
- "repeat_type": "once" | "daily" | "weekly" | "school_days"
- "remind_iso": ISO-8601 datetime with +06:30 offset for the next occurrence in MMT, or null

If intent is "GROUP", also extract:
- "group_name": exact matching group from Available Groups if clear, else null
- "group_mode": "summary" | "query"
- "period": "today" | "yesterday" | "recent"

Return ONLY valid JSON:
{{
  "intent": "TASK|GROUP|ADVICE|GENERAL|WEB_SEARCH",
  "search_query": null,
  "task_action": "add",
  "target_task_query": null,
  "tasks": [],
  "clean_title": null,
  "reason": null,
  "repeat_type": "once",
  "remind_iso": null,
  "group_name": null,
  "group_mode": "query",
  "period": "recent"
}}
"""
    parsed = _call_json_intelligence(prompt)
    if not parsed:
        return {
            "intent": "GENERAL",
            "search_query": None,
            "task_action": "add",
            "target_task_query": None,
            "tasks": [],
            "clean_title": _clean_task_title_fallback(user_message),
            "reason": None,
            "repeat_type": "once",
            "remind_dt": None,
            "group_name": None,
            "group_mode": "query",
            "period": "recent",
        }

    intent = str(parsed.get("intent") or "GENERAL").upper().strip()
    if intent not in {"TASK", "GROUP", "ADVICE", "GENERAL", "WEB_SEARCH"}:
        intent = "GENERAL"

    target_wd = _extract_weekday_target(user_message)
    repeat_type = str(parsed.get("repeat_type") or "once").strip()
    if repeat_type not in {"once", "daily", "weekly", "school_days"}:
        repeat_type = "once"

    rem_dt = None
    if parsed.get("remind_iso"):
        try:
            dt_obj = datetime.fromisoformat(str(parsed["remind_iso"]))
            rem_dt = dt_obj.replace(tzinfo=MM_TZ) if dt_obj.tzinfo is None else dt_obj.astimezone(MM_TZ)
        except Exception:
            rem_dt = parse_reminder_datetime_mmt(user_message)
    elif intent == "TASK":
        rem_dt = parse_reminder_datetime_mmt(user_message)

    if rem_dt:
        if repeat_type == "school_days":
            while rem_dt.weekday() >= 5:
                rem_dt += timedelta(days=1)
        elif repeat_type == "weekly" and target_wd is not None:
            days_ahead = (target_wd - rem_dt.weekday()) % 7
            if days_ahead == 0 and rem_dt <= now_mm:
                days_ahead = 7
            rem_dt += timedelta(days=days_ahead)

    normalized_tasks = []
    raw_tasks = parsed.get("tasks")
    if isinstance(raw_tasks, list) and raw_tasks:
        for item in raw_tasks[:8]:
            if not isinstance(item, dict):
                continue
            item_title = _clean_task_title_fallback(str(item.get("clean_title") or ""))
            if not item_title:
                continue
            item_rep = str(item.get("repeat_type") or repeat_type or "once").strip()
            if item_rep not in {"once", "daily", "weekly", "school_days"}:
                item_rep = "once"
            item_rem_dt = None
            if item.get("remind_iso"):
                try:
                    d_o = datetime.fromisoformat(str(item["remind_iso"]))
                    item_rem_dt = d_o.replace(tzinfo=MM_TZ) if d_o.tzinfo is None else d_o.astimezone(MM_TZ)
                except Exception:
                    item_rem_dt = parse_reminder_datetime_mmt(item_title)
            else:
                item_rem_dt = parse_reminder_datetime_mmt(item_title) or rem_dt
            item_reason = str(item.get("reason")).strip() if item.get("reason") else None
            normalized_tasks.append({
                "clean_title": item_title,
                "reason": item_reason,
                "repeat_type": item_rep,
                "remind_dt": item_rem_dt,
            })

    raw_title = parsed.get("clean_title")
    clean_title = (str(raw_title).strip() if raw_title else "") or _clean_task_title_fallback(user_message)
    clean_title = _clean_task_title_fallback(clean_title) or _clean_task_title_fallback(user_message)

    raw_target = parsed.get("target_task_query")
    target_task_query = str(raw_target).strip() if raw_target else None

    raw_reason = parsed.get("reason")
    reason = str(raw_reason).strip() if raw_reason else None

    raw_sq = parsed.get("search_query")
    search_query = str(raw_sq).strip() if raw_sq else None

    raw_esq = parsed.get("english_search_query")
    english_search_query = str(raw_esq).strip() if raw_esq else None

    return {
        "intent": intent,
        "search_query": search_query,
        "task_action": str(parsed.get("task_action") or "add").lower().strip(),
        "target_task_query": target_task_query,
        "tasks": normalized_tasks,
        "clean_title": clean_title,
        "reason": reason,
        "repeat_type": repeat_type,
        "remind_dt": rem_dt,
        "group_name": parsed.get("group_name") or None,
        "group_mode": str(parsed.get("group_mode") or "query").lower().strip(),
        "period": str(parsed.get("period") or "recent").lower().strip(),
    }


def add_task(
        title: str,
        remind_dt: datetime | None = None,
        reason: str | None = None,
        repeat_type: str = "once",
) -> dict:
    category, priority = _detect_task_category_and_priority(title, reason)
    clean_reason = re.sub(r"\[meta:[^\]]*\]", "", str(reason or "")).strip() or None

    payload = {
        "title": title,
        "status": "pending",
        "repeat_type": repeat_type or "once",
        "category": category,
        "priority": priority,
        "reason": clean_reason,
    }
    if remind_dt:
        payload["remind_at"] = remind_dt.isoformat()
        payload["reminded"] = False
    try:
        supabase.table("tasks").insert(payload).execute()
    except Exception:
        fallback_payload = {
            "title": title,
            "status": "pending",
            "repeat_type": repeat_type or "once",
            "reason": _pack_task_reason(clean_reason, category, priority),
        }
        if remind_dt:
            fallback_payload["remind_at"] = remind_dt.isoformat()
            fallback_payload["reminded"] = False
        try:
            supabase.table("tasks").insert(fallback_payload).execute()
        except Exception:
            minimal_payload = {"title": title, "status": "pending"}
            if remind_dt:
                minimal_payload["remind_at"] = remind_dt.isoformat()
                minimal_payload["reminded"] = False
            supabase.table("tasks").insert(minimal_payload).execute()
    return {"status": "success", "message": f"Task added: {title}"}


def list_tasks() -> list:
    cleanup_old_done_tasks()
    res = supabase.table("tasks").select("*").order("id").execute()
    return res.data


def get_formatted_tasks() -> str:
    tasks = list_tasks()
    if not tasks:
        return "လက်ရှိ Task မရှိသေးပါဘူးရှင်"

    pending_rows = []
    done_rows = []
    for t in tasks:
        title = t.get("title", "")
        clean_reason, cat, prio = _unpack_task_meta(t)
        cat_lbl = _category_label_mm(cat)
        prio_badge = _priority_badge_mm(prio)

        if t.get("status") == "done":
            done_rows.append(f"✓ {title} ({cat_lbl})")
        else:
            r_str = ""
            if t.get("remind_at"):
                try:
                    r_dt = datetime.fromisoformat(str(t["remind_at"]).replace("Z", "+00:00")).astimezone(MM_TZ)
                    r_str = f" (⏰ {r_dt.strftime('%m-%d %H:%M')})"
                except Exception:
                    pass
            rep = t.get("repeat_type") or "once"
            rep_badge = ""
            if rep == "daily":
                rep_badge = " · နေ့စဉ်"
            elif rep == "weekly":
                rep_badge = " · အပတ်စဉ်"
            elif rep == "school_days":
                rep_badge = " · ကျောင်းဖွင့်ရက်"

            tags = " ".join([x for x in [prio_badge, f"[{cat_lbl}]"] if x])
            line = f"• {title}{r_str}{rep_badge}"
            if tags:
                line += f" — {tags}"
            if clean_reason:
                line += f"\n   📝 {clean_reason}"
            pending_rows.append(line)

    parts = [f"လက်ရှိ Task စာရင်းပါရှင် (Pending: {len(pending_rows)} ခု)"]
    if pending_rows:
        parts.append("\n".join(pending_rows))
    else:
        parts.append("လုပ်ဆောင်ရန် ကျန်ရှိနေတဲ့ Task မရှိပါဘူးရှင်")

    if done_rows:
        parts.append("ပြီးစီးပြီး:\n" + "\n".join(done_rows[-8:]))

    return "\n\n".join(parts)


def delete_latest_pending_task() -> str | None:
    try:
        last_res = (
            supabase.table("tasks")
            .select("id, title")
            .eq("status", "pending")
            .order("id", desc=True)
            .limit(1)
            .execute()
        )
        rows = last_res.data or []
        if rows:
            t_id = rows[0]["id"]
            t_title = rows[0].get("title", "")
            supabase.table("tasks").delete().eq("id", t_id).execute()
            return t_title
    except Exception as e:
        print(f"Error deleting latest pending task: {e}")
    return None


def _find_matching_pending_task(pending_tasks: list, query_text: str) -> dict | None:
    q = (query_text or "").strip().lower()
    if not pending_tasks:
        return None
    if not q:
        return pending_tasks[0]
    exact = next((t for t in pending_tasks if q in str(t.get("title", "")).lower()), None)
    if exact:
        return exact
    words = [w for w in re.findall(r"[\w\u1000-\u109F]{2,}", q)]
    for w in words:
        m = next((t for t in pending_tasks if w in str(t.get("title", "")).lower()), None)
        if m:
            return m
    return None


def _format_single_task_added(title: str, remind_dt: datetime | None, reason: str | None, repeat_mode: str,
                              raw_ctx: str) -> str:
    clean_reason, cat, prio = _unpack_task_meta({"title": title, "reason": reason})
    if not reason:
        cat, prio = _detect_task_category_and_priority(f"{title} {raw_ctx}", None)

    use_maung = (cat in ("health", "family")) or _is_health_or_family_task(f"{title} {raw_ctx}", clean_reason)
    prefix = "မောင်ရေ — " if use_maung else ""
    ending = "နော်" if use_maung else "ရှင်"

    cat_lbl = _category_label_mm(cat)
    prio_badge = _priority_badge_mm(prio)
    meta_line = f"{cat_lbl}" + (f" · {prio_badge}" if prio_badge else "")

    line = f"{prefix}\"{title}\" မှတ်ထားလိုက်ပါပြီ{ending}\n{meta_line}"
    if remind_dt:
        line += f"\n⏰ {remind_dt.strftime('%Y-%m-%d %H:%M')} ({MYANMAR_WEEKDAYS[remind_dt.weekday()]})"
    if repeat_mode == "school_days":
        line += " · ကျောင်းဖွင့်ရက်"
    elif repeat_mode == "weekly":
        line += " · အပတ်စဉ်"
    elif repeat_mode == "daily":
        line += " · နေ့စဉ်"
    if clean_reason:
        line += f"\n📝 {clean_reason}"
    return line


def handle_task_intent(user_message: str, route: dict) -> str:
    action = (route.get("task_action") or "add").lower().strip()
    task_title = (route.get("clean_title") or "").strip()
    target_query = (route.get("target_task_query") or "").strip()
    reason = route.get("reason")
    use_maung = _is_health_or_family_task(f"{task_title} {target_query} {user_message}", reason)
    prefix = "မောင်ရေ — " if use_maung else ""
    ending = "နော်" if use_maung else "ရှင်"

    if action == "list":
        return get_formatted_tasks()

    if action == "cancel_last":
        removed = delete_latest_pending_task()
        if removed:
            is_hf = _is_health_or_family_task(removed)
            p = "မောင်ရေ — " if is_hf else ""
            e = "နော်" if is_hf else "ရှင်"
            return f"{p}\"{removed}\" ကို Task ထဲက ပြန်ဖျက်ပေးလိုက်ပါပြီ{e}"
        return f"{prefix}ပြန်ဖျက်စရာ Task မတွေ့ပါဘူး{ending}"

    if action == "delete":
        try:
            pending_res = (
                supabase.table("tasks")
                .select("*")
                .eq("status", "pending")
                .order("id", desc=True)
                .limit(30)
                .execute()
            )
            pending_tasks = pending_res.data or []
            matched = _find_matching_pending_task(pending_tasks, target_query or task_title or user_message)
            if matched:
                m_title = matched["title"]
                supabase.table("tasks").delete().eq("id", matched["id"]).execute()
                is_hf = _is_health_or_family_task(m_title, matched.get("reason"))
                p = "မောင်ရေ — " if is_hf else ""
                e = "နော်" if is_hf else "ရှင်"
                return f"{p}\"{m_title}\" ကို Task ထဲက ဖျက်ပေးလိုက်ပါပြီ{e}"
            return f"{prefix}ဖျက်ရမယ့် Task ကို မတွေ့သေးပါဘူး{ending}"
        except Exception as e:
            print(f"Task delete error: {e}")
            return f"{prefix}Task ဖျက်ရာမှာ ခဏအဆင်မပြေ ဖြစ်သွားပါတယ်၊ ထပ်စမ်းကြည့်ပါ{ending}"

    if action in ("edit", "reschedule"):
        try:
            pending_res = (
                supabase.table("tasks")
                .select("*")
                .eq("status", "pending")
                .order("id", desc=True)
                .limit(30)
                .execute()
            )
            pending_tasks = pending_res.data or []
            matched = _find_matching_pending_task(pending_tasks, target_query or task_title or user_message)
            new_rem_dt = route.get("remind_dt") or parse_reminder_datetime_mmt(user_message)
            new_rep = route.get("repeat_type") or "once"

            if matched:
                upd = {}
                final_title = matched["title"]
                if task_title and len(task_title) >= 2 and task_title not in matched["title"]:
                    final_title = task_title
                    upd["title"] = final_title
                if new_rem_dt:
                    upd["remind_at"] = new_rem_dt.isoformat()
                    upd["reminded"] = False
                if new_rep != "once":
                    upd["repeat_type"] = new_rep
                if reason:
                    upd["reason"] = reason

                if upd:
                    try:
                        supabase.table("tasks").update(upd).eq("id", matched["id"]).execute()
                    except Exception:
                        supabase.table("tasks").update({"title": final_title}).eq("id", matched["id"]).execute()

                is_hf = _is_health_or_family_task(final_title, matched.get("reason"))
                p = "မောင်ရေ — " if is_hf else ""
                e = "နော်" if is_hf else "ရှင်"
                msg = f"{p}\"{final_title}\" ကို ပြင်ဆင်မှတ်သားပေးလိုက်ပါပြီ{e}"
                if new_rem_dt:
                    msg += f"\n⏰ {new_rem_dt.strftime('%Y-%m-%d %H:%M')} ({MYANMAR_WEEKDAYS[new_rem_dt.weekday()]})"
                return msg
            return f"{prefix}ပြင်ဆင်ရမယ့် Pending Task ကို မတွေ့သေးပါဘူး{ending}"
        except Exception as e:
            print(f"Task edit error: {e}")
            return f"{prefix}Task ပြင်ဆင်ရာမှာ ခဏအဆင်မပြေ ဖြစ်သွားပါတယ်၊ ထပ်စမ်းကြည့်ပါ{ending}"

    if action == "done":
        try:
            search_key = (target_query or task_title or user_message).strip()
            pending_res = (
                supabase.table("tasks")
                .select("*")
                .eq("status", "pending")
                .order("id", desc=True)
                .limit(25)
                .execute()
            )
            pending_tasks = pending_res.data or []
            matched = _find_matching_pending_task(pending_tasks, search_key) or (
                pending_tasks[0] if pending_tasks else None)
            if matched:
                m_title = matched["title"]
                is_hf = _is_health_or_family_task(m_title, matched.get("reason"))
                p = "မောင်ရေ — " if is_hf else ""
                e = "နော်" if is_hf else "ရှင်"
                done_payload = {"status": "done", "reminded": True, "updated_at": datetime.now(MM_TZ).isoformat()}
                try:
                    supabase.table("tasks").update(done_payload).eq("id", matched["id"]).execute()
                except Exception:
                    supabase.table("tasks").update({"status": "done", "reminded": True}).eq("id",
                                                                                            matched["id"]).execute()
                return f"{p}\"{m_title}\" ပြီးသွားပြီမို့ Done မှတ်လိုက်ပါပြီ (နောက်ထပ် remind မလုပ်တော့ပါ){e}"

            return f"{prefix}ပြီးစီးကြောင်း မှတ်ရမယ့် Pending Task မတွေ့သေးပါဘူး{ending}"
        except Exception as e:
            print(f"Task update error: {e}")
            return f"{prefix}Task အခြေအနေ ပြောင်းရာမှာ ခဏအဆင်မပြေ ဖြစ်သွားပါတယ်၊ ထပ်စမ်းကြည့်ပါ{ending}"

    multi_tasks = route.get("tasks") or []
    if len(multi_tasks) > 1:
        added_lines = []
        for idx, mt in enumerate(multi_tasks, start=1):
            t_title = mt["clean_title"]
            t_rem = mt.get("remind_dt")
            t_reason = mt.get("reason")
            t_rep = mt.get("repeat_type", "once")
            add_task(t_title, t_rem, reason=t_reason, repeat_type=t_rep)
            added_lines.append(f"{idx}. {_format_single_task_added(t_title, t_rem, t_reason, t_rep, user_message)}")
        return "\n\n".join(added_lines)

    clean_title = (multi_tasks[0]["clean_title"] if multi_tasks else task_title) or _clean_task_title_fallback(
        user_message)
    remind_dt = (multi_tasks[0].get("remind_dt") if multi_tasks else route.get("remind_dt"))
    repeat_mode = (multi_tasks[0].get("repeat_type") if multi_tasks else route.get("repeat_type", "once"))
    reason = (multi_tasks[0].get("reason") if multi_tasks else reason)

    is_correction = any(w in user_message for w in ["မဟုတ်ဘူး", "ပြောတယ်", "ပြင်"])
    if is_correction:
        try:
            latest_res = (
                supabase.table("tasks")
                .select("*")
                .eq("status", "pending")
                .order("id", desc=True)
                .limit(1)
                .execute()
            )
            latest_rows = latest_res.data or []
            if latest_rows:
                last_id = latest_rows[0]["id"]
                upd = {"title": clean_title, "repeat_type": repeat_mode or "once"}
                if reason:
                    upd["reason"] = reason
                if remind_dt:
                    upd["remind_at"] = remind_dt.isoformat()
                    upd["reminded"] = False
                try:
                    supabase.table("tasks").update(upd).eq("id", last_id).execute()
                except Exception:
                    supabase.table("tasks").update({"title": clean_title}).eq("id", last_id).execute()
            else:
                add_task(clean_title, remind_dt, reason=reason, repeat_type=repeat_mode)
        except Exception:
            add_task(clean_title, remind_dt, reason=reason, repeat_type=repeat_mode)
    else:
        add_task(clean_title, remind_dt, reason=reason, repeat_type=repeat_mode)

    return _format_single_task_added(clean_title, remind_dt, reason, repeat_mode, user_message)