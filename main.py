import os
import re
import asyncio
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime, timedelta
from telegram import Update, BotCommand, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
)

from config import TELEGRAM_BOT_TOKEN, ADMIN_USER_ID, MM_TZ, MYANMAR_WEEKDAYS
from database import supabase
from ai_engine import call_ai_with_3tier_fallback
from memory_manager import (
    save_chat_message,
    get_user_profile_record,
    update_core_rule_version,
    get_skill_versions_summary,
    evolve_user_profile,
    recall_relevant_history,
)
from task_manager import (
    _compute_next_repeat_datetime,
    _unpack_task_meta,
    _category_label_mm,
    _priority_badge_mm,
    _is_health_or_family_task,
    cleanup_old_done_tasks,
    get_myanmar_calendar_context,
    smart_route_and_parse,
    get_formatted_tasks,
    handle_task_intent,
)
from group_news_search import (
    cleanup_old_group_messages,
    save_group_message,
    get_available_group_names,
    handle_group_query,
    generate_all_groups_summary,
    handle_news_command,
    perform_hybrid_web_search,
)
from voice_handler import handle_voice_message, send_voice_reply
from github_ops import handle_patch_command, getfile_command, handle_getfile_callback


class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, format, *args):
        return


def _start_port_server():
    port = int(os.environ.get("PORT", "10000"))
    server = HTTPServer(("0.0.0.0", port), _HealthHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()


# --- Command Handlers ---
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_USER_ID:
        return
    await update.message.reply_text(
        "မင်္ဂလာပါရှင်! သင်ဇာ (Thinzar) ရောက်ပါပြီ\n"
        "Task/Reminder မှတ်တာ၊ ပြင်တာ၊ ဖျက်တာ၊ မြန်မာပြက္ခဒိန် မေးတာ၊ Group မှတ်တမ်း/Summary ကြည့်တာ၊ "
        "သတင်းနဲ့ Web Search ရှာတာ၊ Voice message ပြောတာတွေကို သဘာဝကျကျ ပြောနိုင်ပါတယ်ရှင်\n\n"
        "Command များ:\n"
        "• /tasks — လက်ရှိ Task စာရင်း\n"
        "• /news [local|intl] [topic] — ပြည်တွင်း/နိုင်ငံတကာ သတင်းအနှစ်ချုပ်\n"
        "• /skills — Locked Core Rules & Version မှတ်တမ်း\n"
        "• /setrule <rule> — Core Rule အသစ်ထည့်ပြီး version တိုးရန်\n"
        "• /patch <file> — GitHub ဖိုင်ကို patch လုပ်ရန်\n"
        "• /getfile — GitHub ဖိုင်ဆွဲချရန်"
    )


async def tasks_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_USER_ID:
        return
    await update.message.reply_text(get_formatted_tasks())


async def news_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_USER_ID:
        return
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    summary_text = handle_news_command(update.message.text or "/news")
    await update.message.reply_text(summary_text)


async def skills_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_USER_ID:
        return
    await update.message.reply_text(get_skill_versions_summary(update.effective_user.id))


async def setrule_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_USER_ID:
        return
    raw = (update.message.text or "").strip()
    parts = raw.split(maxsplit=1)
    rule_text = parts[1].strip() if len(parts) > 1 else ""
    if not rule_text:
        await update.message.reply_text(
            "ထည့်လိုသည့် Rule စာသားလေး ရေးပေးပါရှင်\nဥပမာ: `/setrule အဖြေများကို တိုတိုနှင့် လိုရင်းတိုရှင်း ရေးပါ`",
            parse_mode="Markdown",
        )
        return
    msg = update_core_rule_version(update.effective_user.id, rule_text, note=f"/setrule: {rule_text[:60]}")
    await update.message.reply_text(msg)


async def groups_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_USER_ID:
        return
    groups = get_available_group_names()
    if not groups:
        await update.message.reply_text("လက်ရှိ မှတ်တမ်းရှိတဲ့ Group စာရင်း မတွေ့သေးပါဘူးရှင်။")
        return
    lines = "\n".join(f"• {g}" for g in groups)
    await update.message.reply_text(f"📌 မှတ်တမ်းရှိသော Group များ:\n{lines}")


async def groupsummary_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_USER_ID:
        return
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📅 Today", callback_data="gsummary:today"),
            InlineKeyboardButton("📆 Yesterday", callback_data="gsummary:yesterday"),
        ]
    ])
    await update.message.reply_text("📊 Group Summary ကြည့်လိုသည့် ရက်ကို ရွေးပါရှင်:", reply_markup=kb)


async def handle_groupsummary_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query or not query.from_user:
        return
    if query.from_user.id != ADMIN_USER_ID:
        await query.answer()
        return
    data = query.data or ""
    if not data.startswith("gsummary:"):
        return
    period = data.split(":", 1)[1].strip().lower()
    await query.answer("Summary ထုတ်နေပါတယ်...")
    await context.bot.send_chat_action(chat_id=query.message.chat.id, action="typing")
    summary_text = generate_all_groups_summary(period)
    await query.message.reply_text(summary_text)


async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_USER_ID:
        return
    await update.message.reply_text(
        "📋 Command Menu:\n"
        "• /tasks — လက်ရှိ Task စာရင်း\n"
        "• /groups — Group စာရင်း\n"
        "• /news — သတင်းအနှစ်ချုပ်\n"
        "• /skills — Core Rules\n"
        "• /setrule — Rule ထည့်ရန်\n"
        "• /patch — GitHub ဖိုင် patch လုပ်ရန်\n"
        "• /getfile — GitHub ဖိုင်ဆွဲချရန်"
    )


# --- Background Reminder Loop ---
async def reminder_loop(app):
    while True:
        try:
            now_mm = datetime.now(MM_TZ)
            cleanup_old_done_tasks()
            cleanup_old_group_messages()

            res = supabase.table("tasks").select("*").eq("status", "pending").execute()
            tasks = res.data or []
            for t in tasks:
                rem_raw = t.get("remind_at")
                if not rem_raw:
                    continue
                if t.get("reminded") is True:
                    continue
                try:
                    rem_dt = datetime.fromisoformat(str(rem_raw).replace("Z", "+00:00"))
                    rem_mm = rem_dt.replace(tzinfo=MM_TZ) if rem_dt.tzinfo is None else rem_dt.astimezone(MM_TZ)
                except Exception:
                    continue

                if rem_mm <= now_mm:
                    title = t.get("title", "Task")
                    clean_reason, cat, prio = _unpack_task_meta(t)
                    use_maung = (cat in ("health", "family")) or _is_health_or_family_task(title, clean_reason)
                    prefix = "မောင်ရေ — " if use_maung else ""
                    ending = "နော်" if use_maung else "ရှင်"

                    cat_lbl = _category_label_mm(cat)
                    prio_badge = _priority_badge_mm(prio)
                    badge_line = f"{cat_lbl}" + (f" · {prio_badge}" if prio_badge else "")

                    msg = f"⏰ {prefix}\"{title}\" လုပ်ရမယ့် အချိန်ရောက်ပါပြီ{ending}\n{badge_line}"
                    if clean_reason:
                        msg += f"\n📝 အကြောင်းရင်း — {clean_reason}"

                    rep_type = (t.get("repeat_type") or "once").strip().lower()
                    next_rem_dt = _compute_next_repeat_datetime(rem_mm, rep_type, now_mm)

                    if next_rem_dt:
                        if rep_type == "school_days":
                            msg += f"\n🔁 ကျောင်းဖွင့်ရက်တိုင်း ဆက်လက်သတိပေးပါမည် (နောက်တစ်ကြိမ်: {next_rem_dt.strftime('%m-%d %H:%M')})"
                        elif rep_type == "weekly":
                            msg += f"\n🔁 အပတ်စဉ် {MYANMAR_WEEKDAYS[next_rem_dt.weekday()]} ဆက်လက်သတိပေးပါမည် (နောက်တစ်ကြိမ်: {next_rem_dt.strftime('%m-%d %H:%M')})"
                        elif rep_type == "daily":
                            msg += f"\n🔁 နေ့စဉ် ဆက်လက်သတိပေးပါမည် (နောက်တစ်ကြိမ်: {next_rem_dt.strftime('%m-%d %H:%M')})"

                        await app.bot.send_message(chat_id=ADMIN_USER_ID, text=msg)
                        try:
                            supabase.table("tasks").update({
                                "remind_at": next_rem_dt.isoformat(),
                                "reminded": False,
                                "updated_at": now_mm.isoformat(),
                            }).eq("id", t["id"]).execute()
                        except Exception:
                            supabase.table("tasks").update({
                                "remind_at": next_rem_dt.isoformat(),
                                "reminded": False,
                            }).eq("id", t["id"]).execute()
                    else:
                        msg += f"\n\nပြီးသွားရင် \"ပြီးပြီ\" လို့ ပြောလိုက်ပါ{ending}"
                        await app.bot.send_message(chat_id=ADMIN_USER_ID, text=msg)
                        try:
                            supabase.table("tasks").update({
                                "reminded": True,
                                "updated_at": now_mm.isoformat(),
                            }).eq("id", t["id"]).execute()
                        except Exception:
                            supabase.table("tasks").update({"reminded": True}).eq("id", t["id"]).execute()

        except Exception as e:
            print(f"Reminder loop error: {e}")

        await asyncio.sleep(20)


# --- Message & Photo Handler ---
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    chat = update.effective_chat
    user = update.effective_user
    user_id = user.id if user else 0
    sender_name = (
        f"{user.first_name or ''} {user.last_name or ''}".strip()
        if user else "Member"
    ) or (user.username if user and user.username else "Member")

    # Group / Supergroup messages -> save to group_messages for later summary/search
    if chat and chat.type in ("group", "supergroup"):
        chat_title = chat.title or f"Group {chat.id}"
        text_content = (update.message.text or update.message.caption or "").strip()

        if update.message.photo and not text_content:
            from group_news_search import _allow_group_media_ai
            if _allow_group_media_ai():
                try:
                    photo = update.message.photo[-1]
                    tg_file = await context.bot.get_file(photo.file_id)
                    img_bytes = await tg_file.download_as_bytearray()
                    caption_res = call_ai_with_3tier_fallback(
                        "ဤပုံထဲတွင် မြင်ရသော အဓိကအချက်အလက် သို့မဟုတ် စာသားကို မြန်မာဘာသာဖြင့် တိုတိုတုတ်တုတ် ဖော်ပြပါ။",
                        image_bytes=img_bytes,
                        mime_type="image/jpeg",
                    )
                    if caption_res and "ခဏအလုပ်များနေပါတယ်" not in caption_res:
                        text_content = f"[ဓာတ်ပုံ]: {caption_res}"
                    else:
                        text_content = "[ဓာတ်ပုံ ပေးပို့ခဲ့သည်]"
                except Exception:
                    text_content = "[ဓာတ်ပုံ ပေးပို့ခဲ့သည်]"
            else:
                text_content = "[ဓာတ်ပုံ ပေးပို့ခဲ့သည်]"
        elif update.message.document and not text_content:
            doc_name = update.message.document.file_name or "document"
            text_content = f"[ဖိုင်]: {doc_name}"

        if text_content:
            save_group_message(chat.id, chat_title, sender_name, text_content)
        return

    # Private chat with admin user only
    if user_id != ADMIN_USER_ID:
        return

    user_text = (update.message.text or update.message.caption or "").strip()
    image_bytes = None
    mime_type = None

    if update.message.photo:
        photo = update.message.photo[-1]
        tg_file = await context.bot.get_file(photo.file_id)
        image_bytes = await tg_file.download_as_bytearray()
        mime_type = "image/jpeg"
        if not user_text:
            user_text = "ဤပုံထဲက အချက်အလက်များကို ဖတ်ပြီး ရှင်းပြပေးပါရှင်"

    if not user_text and not image_bytes:
        return

    await context.bot.send_chat_action(chat_id=chat.id, action="typing")
    save_chat_message(user_id, "user", user_text)

    if image_bytes:
        history_ctx = recall_relevant_history(user_id, user_text, recent_limit=6, search_limit=6)
        prompt = f"""
အသုံးပြုသူက ပုံတစ်ပုံ ပေးပို့ထားသည်။
Memory & User Context:
{history_ctx}

User Prompt: {user_text}

သင်ဇာ (Thinzar) အမည်ရှိ မြန်မာမိန်းကလေးဟန် AI Assistant အဖြစ် ပုံထဲမှ အချက်အလက်များကို တိကျစွာ ဖတ်ရှုဖြေဆိုပါ။
'မောင်' ဟု လုံးဝ မသုံးပါနှင့်။
"""
        reply = call_ai_with_3tier_fallback(prompt, image_bytes=image_bytes, mime_type=mime_type)
        save_chat_message(user_id, "assistant", reply)
        evolve_user_profile(user_id, user_text, reply)
        await update.message.reply_text(reply)
        return

    history_ctx = recall_relevant_history(user_id, user_text, recent_limit=8, search_limit=10)
    available_groups = get_available_group_names()
    route = smart_route_and_parse(
        user_text,
        available_groups,
        recent_ctx=history_ctx[-1400:],
        user_pref_ctx=history_ctx[:1000],
    )
    intent = route.get("intent", "GENERAL")

    if intent == "TASK":
        reply = handle_task_intent(user_text, route)
    elif intent == "GROUP":
        reply = handle_group_query(user_text, route)
    elif intent == "WEB_SEARCH":
        search_q = route.get("search_query") or user_text
        eng_q = route.get("english_search_query")
        reply = perform_hybrid_web_search(search_q, english_query=eng_q, history_ctx=history_ctx)
    else:
        cal_ctx = get_myanmar_calendar_context()
        prompt = f"""
Current Myanmar Calendar & Time:
{cal_ctx}

Memory & User Context:
{history_ctx}

Latest User Message: "{user_text}"

ညွှန်ကြားချက်များ:
1. သင်ဇာ (Thinzar) အမည်ရှိ မြန်မာမိန်းကလေးဟန် ကိုယ်ပိုင် AI Assistant အဖြစ် သဘာဝကျကျ၊ ရင်းနှီးနွေးထွေးစွာ ဖြေဆိုပါ။
2. အကြံဉာဏ် / ရွေးချယ်စရာ / ဆုံးဖြတ်ချက် အကူအညီ တောင်းလျှင် အကျိုး/အပြစ်၊ လက်တွေ့အသုံးဝင်မှု နှင့် အကြံပြုချက်များကို ရှင်းရှင်းလင်းလင်း စဉ်းစားသုံးသပ် ဖြေဆိုပါ။
3. မြန်မာပြက္ခဒိန် / ဥပုသ် / ရက်ရာဇာ / ပြဿဒါး မေးလျှင် အထက်ပါ Calendar Context ကို အခြေခံ၍ တိကျစွာ ဖြေပါ။
4. အတိတ်မှတ်တမ်း / Profile နှင့် ပတ်သက်၍ မေးလျှင် Memory & User Context ထဲမှ အချက်အလက်များကို အခြေခံ၍ တိကျစွာ ဖြေပါ။
5. 'မောင်' ဟူသော အသုံးအနှုန်းကို ဆေးသောက်ရန် နှင့် မိသားစု/ကလေး Task/Reminder များမှလွဲ၍ အခြားနေရာများတွင် လုံးဝ မသုံးပါနှင့်။
"""
        reply = call_ai_with_3tier_fallback(prompt)

    save_chat_message(user_id, "assistant", reply)
    evolve_user_profile(user_id, user_text, reply)
    await update.message.reply_text(reply)


async def post_init(application):
    await application.bot.set_my_commands([
        BotCommand("start", "စတင်ရန်"),
        BotCommand("menu", "Menu စာရင်းကြည့်ရန်"),
        BotCommand("tasks", "လက်ရှိ Task စာရင်း"),
        BotCommand("groups", "Group စာရင်းကြည့်ရန်"),
        BotCommand("group", "Group စာရင်းကြည့်ရန်"),
        BotCommand("news", "သတင်းအနှစ်ချုပ်"),
        BotCommand("skills", "Core Rules မှတ်တမ်း"),
        BotCommand("setrule", "Core Rule အသစ်ထည့်ရန်"),
        BotCommand("patch", "GitHub ဖိုင် patch လုပ်ရန်"),
        BotCommand("getfile", "GitHub ဖိုင်ဆွဲချရန်"),
    ])
    application.create_task(reminder_loop(application))


async def _on_error(update, context):
    print(f"Telegram Bot Error: {context.error}")


def main():
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is missing.")

    _start_port_server()

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("menu", menu_command))
    app.add_handler(CommandHandler("tasks", tasks_command))
    app.add_handler(CommandHandler("groups", groups_command))
    app.add_handler(CommandHandler("group", groups_command))
    app.add_handler(CommandHandler("news", news_command))
    app.add_handler(CommandHandler("skills", skills_command))
    app.add_handler(CommandHandler("setrule", setrule_command))
    app.add_handler(CommandHandler("patch", handle_patch_command))
    app.add_handler(CommandHandler("getfile", getfile_command))
    app.add_handler(CallbackQueryHandler(handle_getfile_callback, pattern=r"^getfile:"))

    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice_message))
    app.add_handler(MessageHandler((filters.TEXT | filters.PHOTO | filters.Document.ALL) & ~filters.COMMAND, handle_message))
    app.add_error_handler(_on_error)

    print("သင်ဇာ (Thinzar) Telegram Bot is starting...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
