import asyncio
import os
import threading
import time
import uuid
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from ai_engine import call_ai_with_3tier_fallback, call_json_intelligence, gemini_client
from config import ADMIN_USER_ID, GITHUB_BRANCH, GITHUB_REPO, MM_TZ, MYANMAR_WEEKDAYS, TELEGRAM_BOT_TOKEN
from database import supabase
from github_ops import get_github_file, parse_patch_hunks, replace_github_snippet, upsert_github_file
from group_news_search import (
    allow_group_media_ai,
    answer_group_question_from_logs,
    cleanup_old_group_messages,
    collect_myanmar_news_digest,
    log_group_message,
    perform_web_search_summary,
    summarize_group_chat,
)
from memory_manager import (
    cleanup_old_raw_logs,
    evolve_user_profile,
    format_mm_timestamp,
    get_recent_history_for_router,
    get_skill_versions_summary,
    recall_relevant_history,
    save_chat_message,
    update_core_rule_version,
)
from task_manager import (
    add_task_record,
    complete_task_record,
    delete_task_record,
    format_tasks_list_message,
    generate_proactive_reminder_text,
    list_tasks_records,
)
from voice_handler import handle_voice_message, transcribe_telegram_voice

PENDING_PATCHES: dict[str, dict] = {}
PENDING_PATCH_TTL_SECONDS = 900
BOT_START_TIME = time.time()


def _cleanup_expired_patches():
    now_ts = time.time()
    expired = [k for k, v in PENDING_PATCHES.items() if (now_ts - v.get("created_at", 0)) > PENDING_PATCH_TTL_SECONDS]
    for k in expired:
        PENDING_PATCHES.pop(k, None)


def get_myanmar_time_context() -> str:
    now_mm = datetime.now(MM_TZ)
    wd_mm = MYANMAR_WEEKDAYS[now_mm.weekday()]
    return (
        f"လက်ရှိ မြန်မာစံတော်ချိန် (UTC+6:30): {now_mm.strftime('%Y-%m-%d %H:%M:%S')} ({wd_mm}, {now_mm.strftime('%A')})"
    )


def _format_uptime(seconds: float) -> str:
    secs = max(0, int(seconds))
    days, rem = divmod(secs, 86400)
    hrs, rem = divmod(rem, 3600)
    mins, _ = divmod(rem, 60)
    if days > 0:
        return f"{days}d {hrs}h {mins}m"
    if hrs > 0:
        return f"{hrs}h {mins}m"
    return f"{mins}m"


async def send_long_message(target_msg, text: str, parse_mode: str | None = "Markdown"):
    if not text:
        return
    max_len = 3900
    chunks = [text[i:i + max_len] for i in range(0, len(text), max_len)]
    for idx, chunk in enumerate(chunks):
        if idx == 0:
            try:
                await target_msg.reply_text(chunk, parse_mode=parse_mode)
            except Exception:
                await target_msg.reply_text(chunk)
        else:
            try:
                await target_msg.chat.send_message(chunk, parse_mode=parse_mode)
            except Exception:
                await target_msg.chat.send_message(chunk)


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"Thinzar AI Agent is alive!")

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

    def log_message(self, format, *args):
        pass


def start_health_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()


def classify_user_intent(user_id: int, message_text: str) -> dict:
    recent_ctx = get_recent_history_for_router(user_id, limit=6)
    prompt = f"""
Recent conversation:
{recent_ctx}

Current user message: "{message_text}"

Classify the user's intent into ONE of:
- "add_task": user wants to create a task or reminder (e.g., "remind me", "သတိပေး", "task ထည့်", "မနက်ဖြန် ... သွားရန်").
- "list_tasks": user asks for pending tasks/reminders/to-do list.
- "complete_task": user marks a task done (e.g., "done task 3", "task #2 ပြီးပြီ").
- "web_search": user asks for live web info, news, prices, weather, facts, or says "search ...".
- "chat": general conversation, coding help, brainstorming, or memory recall.

Return ONLY valid JSON:
{{"intent":"...","task_query":"...","task_id":null,"search_query":"..."}}
"""
    parsed = call_json_intelligence(prompt)
    if parsed and parsed.get("intent") in {"add_task", "list_tasks", "complete_task", "web_search", "chat"}:
        return parsed
    return {"intent": "chat", "task_query": "", "task_id": None, "search_query": ""}


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or user.id != ADMIN_USER_ID:
        return
    msg = (
        "မင်္ဂလာပါရှင် 🌸 သင်ဇာ (Thinzar) ကိုယ်ပိုင် AI Assistant မှ ကြိုဆိုပါတယ်။\n\n"
        "• **Chat & Memory:** ရိုးရိုးစကားပြောခြင်း၊ အတိတ်မှတ်တမ်း ပြန်ရှာခြင်း\n"
        "• **Task & Reminder:** `/addtask`, `/tasks`, `/done <id>`, `/deltask <id>`\n"
        "• **News & Search:** `/news`, `/search <query>`\n"
        "• **Group Summary (Group ထဲတွင်):** `/summary [hours]`, `/ask <question>`\n"
        "• **Skills & GitHub Patch:** `/skills`, `/setrule <rule>`, `/patch <file>`, `/upload <file>`, `/status`"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or user.id != ADMIN_USER_ID:
        return
    up_str = _format_uptime(time.time() - BOT_START_TIME)
    await update.message.reply_text(
        f"✅ **Thinzar AI Agent Status**\n"
        f"• Uptime: `{up_str}`\n"
        f"• Repo: `{GITHUB_REPO or 'Not set'}` (`{GITHUB_BRANCH}`)\n"
        f"• {get_myanmar_time_context()}",
        parse_mode="Markdown",
    )


async def cmd_addtask(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or user.id != ADMIN_USER_ID:
        return
    raw = " ".join(context.args).strip()
    if not raw:
        await update.message.reply_text("အသုံးပြုပုံ: `/addtask ည ၈ နာရီ ဆေးသောက်ရန်`", parse_mode="Markdown")
        return
    res = add_task_record(user.id, raw)
    if res.get("ok"):
        t = res["task"]
        dt_info = f"\n⏰ `{format_mm_timestamp(t.get('reminder_at') or t.get('due_at'))}`" if (t.get("reminder_at") or t.get("due_at")) else ""
        await update.message.reply_text(f"✅ Task ထည့်ပြီးပါပြီရှင် — `#{t.get('id')}` **{t.get('title')}**{dt_info}", parse_mode="Markdown")
    else:
        await update.message.reply_text(f"Task ထည့်ရာမှာ အခက်အခဲ ရှိနေပါတယ်ရှင်: {res.get('error')}")


async def cmd_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or user.id != ADMIN_USER_ID:
        return
    include_done = bool(context.args and context.args[0].lower() in ("all", "done"))
    tasks = list_tasks_records(user.id, include_done=include_done)
    await update.message.reply_text(format_tasks_list_message(tasks), parse_mode="Markdown")


async def cmd_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or user.id != ADMIN_USER_ID:
        return
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("အသုံးပြုပုံ: `/done <task_id>`", parse_mode="Markdown")
        return
    res = complete_task_record(user.id, int(context.args[0]))
    if res.get("ok"):
        await update.message.reply_text(f"✅ Task `#{context.args[0]}` ပြီးစီးကြောင်း မှတ်သားပြီးပါပြီရှင်။", parse_mode="Markdown")
    else:
        await update.message.reply_text(res.get("error") or "မတွေ့ပါဘူးရှင်။")


async def cmd_deltask(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or user.id != ADMIN_USER_ID:
        return
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("အသုံးပြုပုံ: `/deltask <task_id>`", parse_mode="Markdown")
        return
    res = delete_task_record(user.id, int(context.args[0]))
    if res.get("ok"):
        await update.message.reply_text(f"🗑️ Task `#{context.args[0]}` ကို ဖျက်ပြီးပါပြီရှင်။", parse_mode="Markdown")
    else:
        await update.message.reply_text(res.get("error") or "မတွေ့ပါဘူးရှင်။")


async def cmd_news(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or user.id != ADMIN_USER_ID:
        return
    wait_msg = await update.message.reply_text("📰 မြန်မာ့သတင်းရင်းမြစ်များကို စုစည်းနေပါတယ်ရှင်...")
    digest = collect_myanmar_news_digest()
    await wait_msg.delete()
    await send_long_message(update.message, digest)


async def cmd_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or user.id != ADMIN_USER_ID:
        return
    query = " ".join(context.args).strip()
    if not query:
        await update.message.reply_text("အသုံးပြုပုံ: `/search <ရှာလိုသည့်အကြောင်းအရာ>`", parse_mode="Markdown")
        return
    wait_msg = await update.message.reply_text(f"🔎 `{query}` ကို ရှာဖွေနေပါတယ်ရှင်...", parse_mode="Markdown")
    ans = perform_web_search_summary(query)
    await wait_msg.delete()
    await send_long_message(update.message, ans)


async def cmd_skills(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or user.id != ADMIN_USER_ID:
        return
    await update.message.reply_text(get_skill_versions_summary(user.id))


async def cmd_setrule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or user.id != ADMIN_USER_ID:
        return
    rule_txt = " ".join(context.args).strip()
    if not rule_txt:
        await update.message.reply_text("အသုံးပြုပုံ: `/setrule <Core Rule အသစ်>`", parse_mode="Markdown")
        return
    res_msg = update_core_rule_version(user.id, rule_txt)
    await update.message.reply_text(res_msg)


async def cmd_summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or user.id != ADMIN_USER_ID:
        return
    chat = update.effective_chat
    if not chat or chat.type == "private":
        await update.message.reply_text("ဒီ command ကို Group chat ထဲမှာ အသုံးပြုပါရှင်။")
        return
    hours = 24
    if context.args and context.args[0].isdigit():
        hours = max(1, min(168, int(context.args[0])))
    wait_msg = await update.message.reply_text(f"📝 ပြီးခဲ့တဲ့ {hours} နာရီအတွင်း Group မှတ်တမ်းကို အကျဉ်းချုပ်နေပါတယ်ရှင်...")
    summary = summarize_group_chat(chat.id, chat.title or "Group", hours=hours)
    await wait_msg.delete()
    await send_long_message(update.message, summary)


async def cmd_ask(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or user.id != ADMIN_USER_ID:
        return
    chat = update.effective_chat
    if not chat or chat.type == "private":
        await update.message.reply_text("ဒီ command ကို Group chat ထဲမှာ အသုံးပြုပါရှင်။")
        return
    question = " ".join(context.args).strip()
    if not question:
        await update.message.reply_text("အသုံးပြုပုံ: `/ask <Group မှတ်တမ်းမှ သိလိုသည့်မေးခွန်း>`", parse_mode="Markdown")
        return
    wait_msg = await update.message.reply_text("🔍 Group မှတ်တမ်းများထဲမှ ရှာဖွေဖြေဆိုနေပါတယ်ရှင်...")
    ans = answer_group_question_from_logs(chat.id, chat.title or "Group", question)
    await wait_msg.delete()
    await send_long_message(update.message, ans)


async def cmd_patch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or user.id != ADMIN_USER_ID:
        return
    _cleanup_expired_patches()
    raw_msg = update.message.text or ""
    parts = raw_msg.split(None, 2)
    if len(parts) < 3:
        await update.message.reply_text(
            "အသုံးပြုပုံ:\n"
            "`/patch <file_path>`\n"
            "`<<<OLD>>>`\n...old code...\n"
            "`<<<NEW>>>`\n...new code...",
            parse_mode="Markdown",
        )
        return

    file_path = parts[1].strip()
    patch_body = parts[2]
    hunks = parse_patch_hunks(patch_body)
    if not hunks:
        await update.message.reply_text("`<<<OLD>>>` နှင့် `<<<NEW>>>` block များ မှန်ကန်စွာ ထည့်ပေးပါရှင်။", parse_mode="Markdown")
        return

    cur = get_github_file(file_path)
    if not cur.get("ok"):
        await update.message.reply_text(f"File ဖတ်မရပါရှင်: {cur.get('error')}")
        return

    from github_ops import apply_hunks_to_source
    ok, preview_text, err = apply_hunks_to_source(cur.get("content", ""), hunks)
    if not ok:
        await update.message.reply_text(f"Patch preview မအောင်မြင်ပါရှင်: {err}")
        return

    req_id = uuid.uuid4().hex[:8]
    PENDING_PATCHES[req_id] = {
        "created_at": time.time(),
        "mode": "patch",
        "file_path": file_path,
        "patch_body": patch_body,
        "new_content": preview_text,
    }
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Commit Patch", callback_data=f"patch_yes:{req_id}"),
            InlineKeyboardButton("❌ Cancel", callback_data=f"patch_no:{req_id}"),
        ]
    ])
    await update.message.reply_text(
        f"🔧 **Patch Ready** (`{file_path}`, `{len(hunks)}` hunk(s))\nCommit တင်မလားရှင်?",
        reply_markup=kb,
        parse_mode="Markdown",
    )


async def cmd_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or user.id != ADMIN_USER_ID:
        return
    _cleanup_expired_patches()
    raw_msg = update.message.text or ""
    parts = raw_msg.split(None, 2)
    if len(parts) < 3:
        await update.message.reply_text("အသုံးပြုပုံ: `/upload <file_path> <full_file_content>`", parse_mode="Markdown")
        return

    file_path = parts[1].strip()
    full_content = parts[2]
    req_id = uuid.uuid4().hex[:8]
    PENDING_PATCHES[req_id] = {
        "created_at": time.time(),
        "mode": "upload",
        "file_path": file_path,
        "new_content": full_content,
    }
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Upload File", callback_data=f"patch_yes:{req_id}"),
            InlineKeyboardButton("❌ Cancel", callback_data=f"patch_no:{req_id}"),
        ]
    ])
    await update.message.reply_text(
        f"📤 **Full File Upload Ready** (`{file_path}`, `{len(full_content)}` chars)\nCommit တင်မလားရှင်?",
        reply_markup=kb,
        parse_mode="Markdown",
    )


async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = update.effective_user
    if not query or not user or user.id != ADMIN_USER_ID:
        return
    await query.answer()
    _cleanup_expired_patches()

    data = query.data or ""
    if ":" not in data:
        return
    action, req_id = data.split(":", 1)
    item = PENDING_PATCHES.pop(req_id, None)
    if not item:
        await query.edit_message_text("ဒီ patch request က သက်တမ်းကုန်သွားပါပြီရှင်။")
        return

    if action == "patch_no":
        await query.edit_message_text(f"❌ `{item.get('file_path')}` patch/upload ကို ပယ်ဖျက်လိုက်ပါပြီရှင်။", parse_mode="Markdown")
        return

    if action == "patch_yes":
        fpath = item.get("file_path", "")
        if item.get("mode") == "patch":
            res = replace_github_snippet(fpath, item.get("patch_body", ""), "")
        else:
            res = upsert_github_file(fpath, item.get("new_content", ""), commit_message=f"Upload {fpath} via Thinzar")

        if res.get("ok"):
            await query.edit_message_text(
                f"✅ **GitHub Committed**\n• File: `{res.get('path')}`\n• Commit: `{res.get('commit')}` (`{res.get('branch')}`)",
                parse_mode="Markdown",
            )
        else:
            await query.edit_message_text(f"❌ Commit မအောင်မြင်ပါရှင်:\n{res.get('error')}")


async def handle_photo_or_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat = update.effective_chat
    if not user:
        return

    if chat and chat.type != "private":
        if not allow_group_media_ai():
            return
        caption = (update.message.caption or "ပုံထဲက အချက်အလက်ကို အကျဉ်းချုပ်ပြောပြပေးပါ။").strip()
        if update.message.photo:
            tg_file = await context.bot.get_file(update.message.photo[-1].file_id)
            img_bytes = await tg_file.download_as_bytearray()
            desc = call_ai_with_3tier_fallback(caption, image_bytes=img_bytes, mime_type="image/jpeg")
            sender_name = user.first_name or user.username or "Member"
            log_group_message(chat.id, chat.title or "Group", user.id, sender_name, "photo", f"{caption} -> {desc[:300]}")
        return

    if user.id != ADMIN_USER_ID:
        return

    caption = (update.message.caption or "ဒီပုံထဲက အချက်အလက်များကို ရှင်းပြပေးပါရှင်။").strip()
    if update.message.photo:
        tg_file = await context.bot.get_file(update.message.photo[-1].file_id)
        img_bytes = await tg_file.download_as_bytearray()
        save_chat_message(user.id, "user", f"[Photo]: {caption}")
        mem_ctx = recall_relevant_history(user.id, caption)
        reply_text = call_ai_with_3tier_fallback(f"{mem_ctx}\n\n{caption}", image_bytes=img_bytes, mime_type="image/jpeg")
        save_chat_message(user.id, "assistant", reply_text)
        await send_long_message(update.message, reply_text)


async def handle_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat = update.effective_chat
    msg = update.message
    if not user or not msg or not msg.text:
        return

    text = msg.text.strip()

    if chat and chat.type != "private":
        sender_name = user.first_name or user.username or "Member"
        log_group_message(chat.id, chat.title or "Group", user.id, sender_name, "text", text)
        return

    if user.id != ADMIN_USER_ID:
        return

    save_chat_message(user.id, "user", text)
    intent_obj = classify_user_intent(user.id, text)
    intent = intent_obj.get("intent", "chat")

    if intent == "add_task":
        t_raw = (intent_obj.get("task_query") or text).strip()
        res = add_task_record(user.id, t_raw)
        if res.get("ok"):
            t = res["task"]
            dt_info = f" (`{format_mm_timestamp(t.get('reminder_at') or t.get('due_at'))}`)" if (t.get("reminder_at") or t.get("due_at")) else ""
            reply_text = f"✅ Task / Reminder မှတ်သားပြီးပါပြီရှင် — `#{t.get('id')}` **{t.get('title')}**{dt_info}"
        else:
            reply_text = f"Task မှတ်ရာမှာ အခက်အခဲ ရှိနေပါတယ်ရှင်: {res.get('error')}"
        save_chat_message(user.id, "assistant", reply_text)
        await send_long_message(msg, reply_text)
        return

    if intent == "list_tasks":
        tasks = list_tasks_records(user.id, include_done=False)
        reply_text = format_tasks_list_message(tasks)
        save_chat_message(user.id, "assistant", reply_text)
        await send_long_message(msg, reply_text)
        return

    if intent == "complete_task" and intent_obj.get("task_id"):
        try:
            tid = int(intent_obj["task_id"])
            res = complete_task_record(user.id, tid)
            reply_text = (
                f"✅ Task `#{tid}` ပြီးစီးကြောင်း မှတ်သားပြီးပါပြီရှင်။"
                if res.get("ok")
                else (res.get("error") or "Task မတွေ့ပါဘူးရှင်။")
            )
            save_chat_message(user.id, "assistant", reply_text)
            await send_long_message(msg, reply_text)
            return
        except Exception:
            pass

    if intent == "web_search":
        q_txt = (intent_obj.get("search_query") or text).strip()
        wait_msg = await msg.reply_text("🔎 ရှာဖွေစစ်ဆေးနေပါတယ်ရှင်...")
        reply_text = perform_web_search_summary(q_txt)
        await wait_msg.delete()
        save_chat_message(user.id, "assistant", reply_text)
        evolve_user_profile(user.id, text, reply_text)
        await send_long_message(msg, reply_text)
        return

    memory_context = recall_relevant_history(user.id, text)
    time_ctx = get_myanmar_time_context()
    prompt = f"""{time_ctx}

{memory_context}

User: {text}

အထက်ပါ မှတ်တမ်းများ၊ Core Rules နှင့် မြန်မာစံတော်ချိန်တို့ကို အခြေခံ၍ သင်ဇာ (Thinzar) ဟန်ဖြင့် သဘာဝကျကျ ဖြေဆိုပေးပါ။"""
    reply_text = call_ai_with_3tier_fallback(prompt, use_google_search=True)
    save_chat_message(user.id, "assistant", reply_text)
    evolve_user_profile(user.id, text, reply_text)
    await send_long_message(msg, reply_text)


async def job_check_due_reminders(context: ContextTypes.DEFAULT_TYPE):
    if not ADMIN_USER_ID:
        return
    try:
        now_mm = datetime.now(MM_TZ)
        res = (
            supabase.table("tasks")
            .select("*")
            # filter by status and reminded flag
            .eq("status", "pending")
            .eq("reminded", False)
            .execute()
        )
        for t in (res.data or []):
            rem_str = t.get("reminder_at") or t.get("due_at")
            if not rem_str:
                continue
            try:
                rem_dt = datetime.fromisoformat(str(rem_str).replace("Z", "+00:00")).astimezone(MM_TZ)
            except Exception:
                continue
            if rem_dt <= now_mm:
                reminder_msg = generate_proactive_reminder_text(t)
                await context.bot.send_message(
                    chat_id=ADMIN_USER_ID,
                    text=f"⏰ **Reminder (`#{t.get('id')}`)**\n\n{reminder_msg}",
                    parse_mode="Markdown",
                )
                rep_type = (t.get("repeat_type") or "once").strip().lower()
                next_dt = compute_next_repeat_datetime(rem_dt, rep_type, now_mm)
                if next_dt:
                    supabase.table("tasks").update({"reminder_at": next_dt.isoformat(), "due_at": next_dt.isoformat(), "reminded": False}).eq("id", t["id"]).execute()
                else:
                    supabase.table("tasks").update({"reminded": True}).eq("id", t["id"]).execute()
    except Exception as e:
        print(f"[Reminder Job Error]: {e}")


async def job_daily_cleanup(context: ContextTypes.DEFAULT_TYPE):
    cleanup_old_raw_logs()
    cleanup_old_group_messages(days=14)


def main():
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is missing")

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    start_health_server()
    
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("addtask", cmd_addtask))
    app.add_handler(CommandHandler("tasks", cmd_tasks))
    app.add_handler(CommandHandler("done", cmd_done))
    app.add_handler(CommandHandler("deltask", cmd_deltask))
    app.add_handler(CommandHandler("news", cmd_news))
    app.add_handler(CommandHandler("search", cmd_search))
    app.add_handler(CommandHandler("skills", cmd_skills))
    app.add_handler(CommandHandler("setrule", cmd_setrule))
    app.add_handler(CommandHandler("summary", cmd_summary))
    app.add_handler(CommandHandler("ask", cmd_ask))
    app.add_handler(CommandHandler("patch", cmd_patch))
    app.add_handler(CommandHandler("upload", cmd_upload))

    app.add_handler(CallbackQueryHandler(handle_callback_query))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice_message))
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.ALL, handle_photo_or_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_message))

    if app.job_queue:
        app.job_queue.run_repeating(job_check_due_reminders, interval=30, first=10)
        app.job_queue.run_daily(job_daily_cleanup, time=datetime.strptime("03:30", "%H:%M").replace(tzinfo=MM_TZ).timetz())

    print("Thinzar AI Agent started successfully.")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
