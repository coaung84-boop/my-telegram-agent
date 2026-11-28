import re
import json
import uuid
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from config import ADMIN_USER_ID, MM_TZ
from ai_engine import call_ai_with_3tier_fallback
from database import supabase

# Inline callback state caches
_TASK_LIST_VIEW_STATE: dict[str, dict] = {}
_PENDING_TASK_EDITS: dict[int, dict] = {}


def _get_mm_date_info(dt_obj: datetime) -> dict:
    """ကျောင်းဖွင့်ရက်/ရုံးပိတ်ရက် အခြေအနေကို နောက်ကွယ် context အတွက် တွက်ချက်သည်။"""
    weekday = dt_obj.weekday()  # 0=Mon .. 4=Fri, 5=Sat, 6=Sun
    mm_str = f"{dt_obj.month:02d}-{dt_obj.day:02d}"
    holidays = {
        "01-04": "လွတ်လပ်ရေးနေ့ (ရုံးပိတ်ရက်)",
        "02-12": "ပြည်ထောင်စုနေ့ (ရုံးပိတ်ရက်)",
        "03-02": "တောင်သူလယ်သမားနေ့ (ရုံးပိတ်ရက်)",
        "03-27": "တပ်မတော်နေ့ (ရုံးပိတ်ရက်)",
        "05-01": "အလုပ်သမားနေ့ (ရုံးပိတ်ရက်)",
        "07-19": "အာဇာနည်နေ့ (ရုံးပိတ်ရက်)",
        "12-25": "ခရစ္စမတ်နေ့ (ရုံးပိတ်ရက်)",
    }
    if mm_str in holidays:
        day_type = holidays[mm_str]
        is_school_day = False
    elif weekday in (5, 6):
        day_type = "စနေ/တနင်္ဂနွေ (ကျောင်းပိတ်/ရုံးပိတ်)"
        is_school_day = False
    else:
        day_type = "ကျောင်းဖွင့်ရက်/ပုံမှန်ရက်"
        is_school_day = True

    return {
        "day_type": day_type,
        "is_school_day": is_school_day,
    }


def format_myanmar_date_short(dt_obj: datetime | None = None) -> str:
    """ရက်စွဲ/အချိန်ကို စာရင်း/reminder မှာ ရှင်းလင်းစွာ ပြရန် format လုပ်ပေးသည်။"""
    now = dt_obj or datetime.now(MM_TZ)
    info = _get_mm_date_info(now)
    return f"{now.strftime('%m-%d %H:%M')} · {info['day_type']}"


def cleanup_old_done_tasks(days_old: int = 14) -> int:
    """သတ်မှတ်ရက်ထက် ကျော်သွားသော done task ဟောင်းများကို ရှင်းလင်းသည်။"""
    try:
        cutoff = (datetime.now(MM_TZ) - timedelta(days=days_old)).isoformat()
        res = supabase.table("tasks").delete().eq("status", "done").lt("completed_at", cutoff).execute()
        return len(res.data or [])
    except Exception:
        return 0


def get_myanmar_calendar_context(target_dt: datetime | None = None) -> str:
    """AI prompt များအတွက် လက်ရှိအချိန်နှင့် ကျောင်းဖွင့်ရက်/ရုံးပိတ်ရက် context ကို ပြန်ပေးသည်။"""
    now = target_dt or datetime.now(MM_TZ)
    mm_days = ["တနင်္လာ", "အင်္ဂါ", "ဗုဒ္ဓဟူး", "ကြာသပတေး", "သောကြာ", "စနေ", "တနင်္ဂနွေ"]
    day_name = mm_days[now.weekday()]
    info = _get_mm_date_info(now)
    return (
        f"Current Time (MMT): {now.strftime('%Y-%m-%d %H:%M')} ({day_name}နေ့)\n"
        f"Calendar Status: {info['day_type']} | School Day: {'Yes' if info['is_school_day'] else 'No'}"
    )


def _category_label_mm(category: str | None) -> str:
    cat = (category or "general").strip().lower()
    mapping = {
        "health": "🩺 ကျန်းမာရေး",
        "family": "👨 👩 👧 မိသားစု",
        "မိသားစု": "👨 👩 👧 မိသားစု",
        "ကလေး": "👨 👩 👧 မိသားစု",
        "work": "💼 လုပ်ငန်း",
        "အလုပ်": "💼 လုပ်ငန်း",
        "ရုံး": "💼 လုပ်ငန်း",
        "general": "📌 အထွေထွေ",
        "အထွေထွေ": "📌 အထွေထွေ",
    }
    if cat in mapping:
        return mapping[cat]
    return f"💼 {category}" if category else "📌 အထွေထွေ"


def _priority_badge_mm(priority: str | None) -> str:
    p = (priority or "normal").strip().lower()
    return "🔥 အရေးကြီး" if p in {"urgent", "high"} else ""


def _unpack_task_meta(task_row: dict) -> tuple[str | None, str, str]:
    """Task row မှ user reason/note၊ category နှင့် priority တို့ကို ခွဲထုတ်ပေးသည်။"""
    if not isinstance(task_row, dict):
        return None, "general", "normal"

    raw_reason = str(task_row.get("reason") or task_row.get("notes") or "").strip()
    clean_reason = re.sub(r"\[meta:[^\]]*\]", "", raw_reason).strip() or None

    cat = str(task_row.get("category") or "general").strip().lower() or "general"
    prio = str(task_row.get("priority") or "normal").strip().lower() or "normal"

    m = re.search(r"\[meta:cat=([a-z_]+),prio=([a-z_]+)\]", raw_reason)
    if m:
        if not task_row.get("category"):
            cat = m.group(1)
        if not task_row.get("priority"):
            prio = m.group(2)

    return clean_reason, cat, prio


def _is_health_or_family_task(text: str, reason: str | None = None) -> bool:
    """ဆေးသောက်ရန် သို့မဟုတ် မိသားစု/ကလေးများနှင့် ပတ်သက်သော task ဟုတ်မဟုတ် စစ်သည်။"""
    combined = f"{text or ''} {reason or ''}".lower()
    keywords = [
        "ဆေး", "medicine", "pill", "tablet", "ဆေးသောက်",
        "မိသားစု", "family", "ကလေး", "ကလေးတွေ", "သား", "သမီး",
        "child", "children", "kid", "kids", "ကျောင်း", "school",
    ]
    return any(k in combined for k in keywords)


def _compute_next_repeat_datetime(
    current_rem_dt: datetime,
    repeat_type: str,
    now_mm: datetime | None = None,
) -> datetime | None:
    """Repeat task (daily / weekly / school_days) ပြီးသွားလျှင် နောက်တစ်ကြိမ် remind_at ကို တွက်ပေးသည်။"""
    now_ref = now_mm or datetime.now(MM_TZ)
    r_type = (repeat_type or "once").strip().lower()
    if r_type not in {"daily", "weekly", "school_days"}:
        return None

    next_dt = current_rem_dt
    if r_type == "daily":
        next_dt = current_rem_dt + timedelta(days=1)
        while next_dt <= now_ref:
            next_dt += timedelta(days=1)
    elif r_type == "weekly":
        next_dt = current_rem_dt + timedelta(days=7)
        while next_dt <= now_ref:
            next_dt += timedelta(days=7)
    elif r_type == "school_days":
        next_dt = current_rem_dt + timedelta(days=1)
        while not _get_mm_date_info(next_dt)["is_school_day"] or next_dt <= now_ref:
            next_dt += timedelta(days=1)

    return next_dt


def _normalize_iso_mmt(due_at: str | None) -> str | None:
    if not due_at:
        return None
    s = str(due_at).strip()
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            dt = datetime.strptime(s[:19], fmt).replace(tzinfo=MM_TZ)
            return dt.isoformat()
        except ValueError:
            continue
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        dt = dt.replace(tzinfo=MM_TZ) if dt.tzinfo is None else dt.astimezone(MM_TZ)
        return dt.isoformat()
    except Exception:
        return None


def add_task(title: str, due_at: str | None = None, category: str = "general", note: str | None = None):
    now_iso = datetime.now(MM_TZ).isoformat()
    rem_iso = _normalize_iso_mmt(due_at)
    cat_val = "family" if category in {"မိသားစု", "ကလေး", "family"} else "general"
    payload = {
        "title": title,
        "status": "pending",
        "priority": "normal",
        "repeat_type": "once",
        "category": cat_val,
        "reminded": False,
        "created_at": now_iso,
        "updated_at": now_iso,
    }
    if rem_iso:
        payload["due_at"] = rem_iso
        payload["remind_at"] = rem_iso
        payload["reminder_at"] = rem_iso
    if note:
        payload["notes"] = note
        payload["reason"] = note
    res = supabase.table("tasks").insert(payload).execute()
    return res.data


def list_tasks(status: str = "pending"):
    res = (
        supabase.table("tasks")
        .select("*")
        .eq("status", status)
        .order("id", desc=False)
        .execute()
    )
    rows = res.data or []
    for r in rows:
        if not r.get("due_at") and r.get("remind_at"):
            r["due_at"] = r["remind_at"]
    return rows


def mark_task_done(task_id: int):
    now_iso = datetime.now(MM_TZ).isoformat()
    res = (
        supabase.table("tasks")
        .update({
            "status": "done",
            "reminded": True,
            "completed_at": now_iso,
            "updated_at": now_iso,
        })
        .eq("id", int(task_id))
        .execute()
    )
    return res.data


def delete_task(task_id: int):
    res = supabase.table("tasks").delete().eq("id", int(task_id)).execute()
    return res.data


def update_task_fields(task_id: int, **kwargs):
    now_iso = datetime.now(MM_TZ).isoformat()
    upd: dict = {"updated_at": now_iso}
    if "title" in kwargs and kwargs["title"]:
        upd["title"] = kwargs["title"]
    if "category" in kwargs and kwargs["category"]:
        c = kwargs["category"]
        upd["category"] = "family" if c in {"မိသားစု", "ကလေး", "family"} else "general"
    if "note" in kwargs and kwargs["note"] is not None:
        upd["notes"] = kwargs["note"]
        upd["reason"] = kwargs["note"]
    if "due_at" in kwargs:
        rem_iso = _normalize_iso_mmt(kwargs["due_at"])
        if rem_iso:
            upd["due_at"] = rem_iso
            upd["remind_at"] = rem_iso
            upd["reminder_at"] = rem_iso
            upd["reminded"] = False
            upd["status"] = "pending"
    res = supabase.table("tasks").update(upd).eq("id", int(task_id)).execute()
    return res.data


def _format_short_due(due_at: str | None) -> str:
    if not due_at:
        return ""
    s = str(due_at).strip()
    m = re.match(r"^\d{4}-(\d{2}-\d{2})[T\s](\d{2}:\d{2})", s)
    if m:
        return f"{m.group(1)} {m.group(2)}"
    m2 = re.match(r"^(\d{2}-\d{2})\s+(\d{2}:\d{2})", s)
    if m2:
        return f"{m2.group(1)} {m2.group(2)}"
    return s[:16]


def _clean_task_title(raw_title: str) -> str:
    t = (raw_title or "").strip().strip('"').strip("'")
    t = re.sub(r"^(?:နောက်\s*\d+\s*မိနစ်(?:အတွင်း|နေရင်)?|မနက်ဖြန်|ဒီနေ့)\s*", "", t).strip()
    t = re.sub(r"\s*reminder\s*ပေးပါ\s*$", "", t, flags=re.IGNORECASE).strip()
    if t.lower() in {"er ပေးရန်", "er", "reminder ပေးရန်"}:
        return "Reminder သတိပေးရန်"
    return t or "Task အသစ်"


def smart_route_and_parse(
    user_text: str,
    available_groups: list[str] | None = None,
    recent_ctx: str = "",
    user_pref_ctx: str = "",
) -> dict:
    """User message ကို TASK / GROUP / WEB_SEARCH / GENERAL အဖြစ် ခွဲခြားပြီး structured info ထုတ်သည်။"""
    now = datetime.now(MM_TZ)
    cal_ctx = get_myanmar_calendar_context(now)
    groups_str = ", ".join(available_groups or [])

    prompt = f"""
Classify the user's message into one of: TASK, GROUP, WEB_SEARCH, GENERAL.
Current context:
{cal_ctx}
Available Telegram groups: [{groups_str}]
Recent chat context:
{recent_ctx[:600]}

User message: "{user_text}"

Return ONLY valid JSON with keys:
- "intent": "TASK" | "GROUP" | "WEB_SEARCH" | "GENERAL"
- "task_action": "ADD" | "LIST" | "DONE" | "DELETE" | "UPDATE" | "REMIND" | null
- "title": string | null (concise clean Burmese/English title without filler words)
- "due_at": string | null (format "YYYY-MM-DD HH:MM" in MMT, e.g. "{(now + timedelta(minutes=8)).strftime('%Y-%m-%d %H:%M')}" for "in 8 minutes")
- "category": string (default "general", or "family" for family/kids)
- "note": string | null
- "task_id": int | null
- "search_query": string | null
- "english_search_query": string | null
- "group_name": string | null

Rules:
- If user asks to show/check/list tasks or reminders (e.g. "task စာရင်း", "reminder ဘာရှိလဲ", "လုပ်စရာစာရင်း"), set intent="TASK", task_action="LIST".
- If user asks to set/add a reminder or task (e.g. "နောက် ၈ မိနစ်နေရင် reminder ပေး", "မနက်ဖြန် ၉ နာရီ ... မှတ်ထား"), set intent="TASK", task_action="ADD" and compute exact due_at.
"""
    try:
        raw = call_ai_with_3tier_fallback(prompt) or ""
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if m:
            parsed = json.loads(m.group(0))
            if isinstance(parsed, dict) and parsed.get("intent"):
                return parsed
    except Exception as e:
        print(f"[Route Parse Error]: {e}")

    low = user_text.lower()
    if any(k in low for k in ["task စာရင်း", "လုပ်စရာ", "reminder ဘာ", "စာရင်းပြ"]):
        return {"intent": "TASK", "task_action": "LIST"}
    rel_m = re.search(r"နောက်\s*(\d+)\s*မိနစ်", user_text)
    if rel_m or "reminder" in low or "မှတ်ထား" in user_text:
        mins = int(rel_m.group(1)) if rel_m else 10
        due_str = (now + timedelta(minutes=mins)).strftime("%Y-%m-%d %H:%M")
        return {
            "intent": "TASK",
            "task_action": "ADD",
            "title": _clean_task_title(user_text),
            "due_at": due_str,
            "category": "general",
        }
    return {"intent": "GENERAL"}


def _build_task_list_payload(show_done: bool = False) -> tuple[str, InlineKeyboardMarkup]:
    """Pending စာရင်းကို ဦးစားပေးပြပြီး ခလုတ်နှိပ်မှ Done စာရင်းကို ကြည့်နိုင်အောင် တည်ဆောက်သည်။"""
    pending_rows = list_tasks(status="pending") or []
    done_rows = list_tasks(status="done") or []

    if not show_done:
        lines = [f"📋 **Task စာရင်း** · Pending **{len(pending_rows)}** ခု", ""]
        if not pending_rows:
            lines.append("• လက်ရှိ လုပ်စရာ Pending task မရှိသေးပါဘူးရှင်။")
        else:
            for idx, t in enumerate(pending_rows[:10], start=1):
                tid = t.get("id")
                title = _clean_task_title(t.get("title") or "")
                due_txt = _format_short_due(t.get("due_at"))
                clean_reason, cat, prio = _unpack_task_meta(t)
                cat_badge = _category_label_mm(cat)
                prio_badge = _priority_badge_mm(prio)
                time_str = f"⏰ `{due_txt}`" if due_txt else "⏰ အချိန်မသတ်မှတ်ရသေး"
                prio_str = f" · {prio_badge}" if prio_badge else ""
                lines.append(f"• **{idx}. {title}** (#{tid}){prio_str}")
                lines.append(f"  {time_str} · {cat_badge}")
                if clean_reason:
                    lines.append(f"  📝 {clean_reason}")
            if len(pending_rows) > 10:
                lines.append(f"• … နောက်ထပ် **{len(pending_rows) - 10}** ခု ကျန်ပါသေးတယ်။")

        lines.append("")
        lines.append("💡 ပြီးစီးစာရင်း ကြည့်ရန် အောက်က **✅ ပြီးစီးပြီး** ကို နှိပ်ပါ။")
    else:
        lines = [f"✅ **ပြီးစီးပြီး Task စာရင်း** ({len(done_rows)} ခု)", ""]
        if not done_rows:
            lines.append("• ပြီးစီးထားတဲ့ task မရှိသေးပါဘူးရှင်။")
        else:
            for t in done_rows[:12]:
                title = _clean_task_title(t.get("title") or "")
                _, cat, _ = _unpack_task_meta(t)
                cat_badge = _category_label_mm(cat)
                lines.append(f"• ✓ **{title}** · {cat_badge}")
            if len(done_rows) > 15:
                lines.append(f"• … နောက်ထပ် **{len(done_rows) - 15}** ခု ကျန်ပါသေးတယ်။")

    view_id = uuid.uuid4().hex[:8]
    _TASK_LIST_VIEW_STATE[view_id] = {"show_done": show_done}

    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                f"{'▸ ' if not show_done else ''}⏳ Pending ({len(pending_rows)})",
                callback_data=f"tskview:{view_id}:pending",
            ),
            InlineKeyboardButton(
                f"{'▸ ' if show_done else ''}✅ ပြီးစီးပြီး ({len(done_rows)})",
                callback_data=f"tskview:{view_id}:done",
            ),
        ]
    ]

    if not show_done and pending_rows:
        for t in pending_rows[:5]:
            tid = t.get("id")
            short_lbl = _clean_task_title(t.get("title") or "")[:16]
            rows.append([
                InlineKeyboardButton(f"✓ {short_lbl}", callback_data=f"tskact:done:{tid}:{view_id}"),
                InlineKeyboardButton("✏️ Edit", callback_data=f"tskact:edit:{tid}:{view_id}"),
                InlineKeyboardButton("⏰ ချိန်း", callback_data=f"tskact:sched:{tid}:{view_id}"),
                InlineKeyboardButton("🔔 သတိပေး", callback_data=f"tskact:remind:{tid}:{view_id}"),
            ])

    rows.append([
        InlineKeyboardButton("➕ Task ထပ်ထည့်မယ်", callback_data=f"tskact:add:0:{view_id}"),
        InlineKeyboardButton("🔄 Refresh", callback_data=f"tskact:refresh:0:{view_id}"),
    ])

    return "\n".join(lines), InlineKeyboardMarkup(rows)


def handle_task_intent(user_text: str, route: dict) -> str:
    """Non-list task intents (ADD / DONE / DELETE / UPDATE / REMIND) ကို လုပ်ဆောင်သည်။"""
    action = (route.get("task_action") or "ADD").upper()
    now = datetime.now(MM_TZ)

    if action == "LIST":
        return "ဘယ် task ကို ဆိုလိုတာလဲရှင်၊ Task နာမည်လေး ဒါမှမဟုတ် ဘာလုပ်ချင်လဲ ပြောပြပေးပါလားနော်။"

    if action == "DONE":
        tid = route.get("task_id")
        if tid:
            mark_task_done(int(tid))
            return f"✅ Task #{tid} ကို ပြီးစီးပြီးအဖြစ် မှတ်သားလိုက်ပါပြီရှင်။"
        return "ပြီးစီးကြောင်း မှတ်လိုတဲ့ Task ID သို့မဟုတ် အမည်ကို ပြောပြပေးပါရှင်။"

    if action == "DELETE":
        tid = route.get("task_id")
        if tid:
            delete_task(int(tid))
            return f"🗑️ Task #{tid} ကို ဖျက်လိုက်ပါပြီရှင်။"
        return "ဖျက်လိုတဲ့ Task ID ကို ပြောပြပေးပါရှင်။"

    title = _clean_task_title(route.get("title") or user_text)
    due_at = route.get("due_at")
    if not due_at:
        m_min = re.search(r"နောက်\s*(\d+)\s*မိနစ်", user_text)
        m_hr = re.search(r"နောက်\s*(\d+)\s*နာရီ", user_text)
        if m_min:
            due_at = (now + timedelta(minutes=int(m_min.group(1)))).strftime("%Y-%m-%d %H:%M")
        elif m_hr:
            due_at = (now + timedelta(hours=int(m_hr.group(1)))).strftime("%Y-%m-%d %H:%M")

    category = (route.get("category") or "general").strip()
    note = route.get("note")

    if action in {"UPDATE", "REMIND"} and route.get("task_id"):
        tid = int(route["task_id"])
        update_task_fields(tid, title=title, due_at=due_at, category=category, note=note)
        due_short = _format_short_due(due_at)
        return f"🔔 Task #{tid} (**{title}**) ကို ပြင်ဆင်/သတိပေးချိန် ညှိပြီးပါပြီရှင်" + (f" · ⏰ `{due_short}`" if due_short else "")

    add_task(title=title, due_at=due_at, category=category, note=note)
    due_short = _format_short_due(due_at)
    cat_badge = _category_label_mm(category)
    due_line = f"\n⏰ `{due_short}`" if due_short else ""
    return f"“{title}” မှတ်ထားလိုက်ပါပြီရှင်\n[{cat_badge}]{due_line}"


async def send_smart_task_list(update: Update, context: ContextTypes.DEFAULT_TYPE, show_done: bool = False):
    """Task စာရင်းကို inline buttons နှင့်အတူ ပို့ပေးသည်။"""
    text, markup = _build_task_list_payload(show_done=show_done)
    await update.message.reply_text(text, reply_markup=markup)


async def handle_task_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Pending/Done toggle နှင့် Task တစ်ခုချင်းစီ၏ Done / Edit / Reschedule / Remind action များကို ကိုင်တွယ်သည်။"""
    query = update.callback_query
    if not query or not query.from_user:
        return
    if query.from_user.id != ADMIN_USER_ID:
        await query.answer("Unauthorized.", show_alert=True)
        return

    await query.answer()
    data = query.data or ""
    parts = data.split(":")

    # tskview:<view_id>:<pending|done>
    if parts[0] == "tskview" and len(parts) == 3:
        _, view_id, mode = parts
        show_done = mode == "done"
        _TASK_LIST_VIEW_STATE[view_id] = {"show_done": show_done}
        text, markup = _build_task_list_payload(show_done=show_done)
        try:
            await query.edit_message_text(text=text, reply_markup=markup)
        except Exception:
            await query.message.reply_text(text=text, reply_markup=markup)
        return

    # tskact:<action>:<tid>:<view_id>
    if parts[0] == "tskact" and len(parts) == 4:
        _, act, tid_str, view_id = parts
        cur_show_done = bool((_TASK_LIST_VIEW_STATE.get(view_id) or {}).get("show_done", False))
        tid = int(tid_str) if tid_str.isdigit() else 0

        if act == "done" and tid > 0:
            mark_task_done(tid)
            text, markup = _build_task_list_payload(show_done=cur_show_done)
            try:
                await query.edit_message_text(text=text, reply_markup=markup)
            except Exception:
                await query.message.reply_text(f"✅ Task #{tid} ပြီးစီးကြောင်း မှတ်ပြီးပါပြီရှင်။")
            return

        if act == "refresh":
            text, markup = _build_task_list_payload(show_done=cur_show_done)
            try:
                await query.edit_message_text(text=text, reply_markup=markup)
            except Exception:
                await query.message.reply_text(text=text, reply_markup=markup)
            return

        if act == "add":
            _PENDING_TASK_EDITS[query.from_user.id] = {"mode": "ADD", "view_id": view_id}
            await query.message.reply_text(
                "➕ **Task အသစ် ထည့်ရန်**\n"
                "အမည်နဲ့ အချိန်ကို သဘာဝကျကျ ရေးပို့ပေးပါရှင်\n"
                "*(ဥပမာ — မနက်ဖြန် နေ့လယ် ၁ နာရီ Vendor ဖုန်းဆက်ရန်)*"
            )
            return

        if act == "edit" and tid > 0:
            _PENDING_TASK_EDITS[query.from_user.id] = {"mode": "EDIT", "task_id": tid, "view_id": view_id}
            await query.message.reply_text(
                f"✏️ **Task #{tid} ပြင်ဆင်ရန်**\n"
                "ပြင်လိုတဲ့ အမည်အသစ် (သို့မဟုတ် အချိန်ပါတဲ့ စာသားအသစ်) ကို ရေးပို့ပေးပါရှင်။"
            )
            return

        if act == "sched" and tid > 0:
            _PENDING_TASK_EDITS[query.from_user.id] = {"mode": "SCHED", "task_id": tid, "view_id": view_id}
            kb = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("+15 Min", callback_data=f"tskquick:{tid}:15m:{view_id}"),
                    InlineKeyboardButton("+1 Hr", callback_data=f"tskquick:{tid}:1h:{view_id}"),
                    InlineKeyboardButton("မနက်ဖြန် 9:00", callback_data=f"tskquick:{tid}:tmr9:{view_id}"),
                ]
            ])
            await query.message.reply_text(
                f"⏰ **Task #{tid} Schedule ပြောင်းရန်**\n"
                "အောက်က shortcut ကို နှိပ်ပါ (သို့မဟုတ်) `2026-11-28 10:30` / `နောက် ၃၀ မိနစ်` လို့ ရေးပို့ပါရှင်။",
                reply_markup=kb,
            )
            return

        if act == "remind" and tid > 0:
            _PENDING_TASK_EDITS[query.from_user.id] = {"mode": "REMIND", "task_id": tid, "view_id": view_id}
            kb = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("🔔 နောက် ၁၀ မိနစ်", callback_data=f"tskquick:{tid}:10m:{view_id}"),
                    InlineKeyboardButton("🔔 နောက် ၁ နာရီ", callback_data=f"tskquick:{tid}:1h:{view_id}"),
                ]
            ])
            await query.message.reply_text(
                f"🔔 **Task #{tid} သတိပေးချိန် ညှိရန်**\n"
                "ဘယ်အချိန်မှာ ပြန်သတိပေးစေချင်လဲ ရွေးပါ (သို့မဟုတ်) အချိန် ရေးပို့ပေးပါရှင်။",
                reply_markup=kb,
            )
            return

    # tskquick:<tid>:<preset>:<view_id>
    if parts[0] == "tskquick" and len(parts) == 4:
        _, tid_str, preset, view_id = parts
        tid = int(tid_str) if tid_str.isdigit() else 0
        now = datetime.now(MM_TZ)
        if tid > 0:
            if preset == "10m":
                new_due = (now + timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M")
            elif preset == "15m":
                new_due = (now + timedelta(minutes=15)).strftime("%Y-%m-%d %H:%M")
            elif preset == "1h":
                new_due = (now + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M")
            elif preset == "tmr9":
                new_due = (now + timedelta(days=1)).strftime("%Y-%m-%d 09:00")
            else:
                new_due = (now + timedelta(minutes=15)).strftime("%Y-%m-%d %H:%M")

            update_task_fields(tid, due_at=new_due)
            cur_show_done = bool((_TASK_LIST_VIEW_STATE.get(view_id) or {}).get("show_done", False))
            text, markup = _build_task_list_payload(show_done=cur_show_done)
            await query.message.reply_text(f"⏰ Task #{tid} အတွက် အချိန်ကို `{_format_short_due(new_due)}` သို့ ညှိပြီးပါပြီရှင်။")
            await query.message.reply_text(text=text, reply_markup=markup)


async def consume_pending_task_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Add / Edit / Schedule / Remind button နှိပ်ပြီးနောက် user ရေးပို့လာသော စာကို လက်ခံဆောင်ရွက်သည်။"""
    msg = update.message
    if not msg or not msg.text or not update.effective_user:
        return False
    uid = update.effective_user.id
    if uid != ADMIN_USER_ID or uid not in _PENDING_TASK_EDITS:
        return False

    state = _PENDING_TASK_EDITS.pop(uid)
    txt = msg.text.strip()
    mode = state.get("mode")
    tid = int(state.get("task_id") or 0)
    view_id = state.get("view_id") or ""
    now = datetime.now(MM_TZ)

    parsed = smart_route_and_parse(txt)
    due_at = parsed.get("due_at")
    if not due_at:
        m_dt = re.search(r"(\d{4}-\d{2}-\d{2})\s+(\d{1,2}:\d{2})", txt)
        m_min = re.search(r"နောက်\s*(\d+)\s*မိနစ်", txt)
        m_hr = re.search(r"နောက်\s*(\d+)\s*နာရီ", txt)
        if m_dt:
            hh, mm = m_dt.group(2).split(":")
            due_at = f"{m_dt.group(1)} {int(hh):02d}:{int(mm):02d}"
        elif m_min:
            due_at = (now + timedelta(minutes=int(m_min.group(1)))).strftime("%Y-%m-%d %H:%M")
        elif m_hr:
            due_at = (now + timedelta(hours=int(m_hr.group(1)))).strftime("%Y-%m-%d %H:%M")

    if mode == "ADD":
        title = _clean_task_title(parsed.get("title") or txt)
        cat = (parsed.get("category") or "general").strip()
        add_task(title=title, due_at=due_at, category=cat)
        await msg.reply_text(f"➕ **“{title}”** ကို Task စာရင်းထဲ ထည့်ပြီးပါပြီရှင်" + (f" · ⏰ `{_format_short_due(due_at)}`" if due_at else ""))
    elif mode == "EDIT" and tid > 0:
        new_title = _clean_task_title(parsed.get("title") or txt)
        if due_at:
            update_task_fields(tid, title=new_title, due_at=due_at)
        else:
            update_task_fields(tid, title=new_title)
        await msg.reply_text(f"✏️ Task #{tid} ကို **“{new_title}”** ဟု ပြင်ဆင်ပြီးပါပြီရှင်။")
    elif mode in {"SCHED", "REMIND"} and tid > 0:
        if due_at:
            update_task_fields(tid, due_at=due_at)
            await msg.reply_text(f"🔔 Task #{tid} အတွက် အချိန်ကို `{_format_short_due(due_at)}` သို့ ပြောင်းပြီးပါပြီရှင်။")
        else:
            await msg.reply_text("အချိန်ပုံစံကို မရှင်းလင်းသေးပါရှင် (ဥပမာ `နောက် ၂၀ မိနစ်` သို့မဟုတ် `2026-11-28 10:00`)။")

    cur_show_done = bool((_TASK_LIST_VIEW_STATE.get(view_id) or {}).get("show_done", False))
    list_txt, markup = _build_task_list_payload(show_done=cur_show_done)
    await msg.reply_text(list_txt, reply_markup=markup)
    return True
