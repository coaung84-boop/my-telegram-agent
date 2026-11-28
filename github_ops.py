import os
import re
import ast
import json
import uuid
import time
import base64
import asyncio
import requests
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from config import ADMIN_USER_ID, MM_TZ
from ai_engine import call_ai_with_3tier_fallback

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "").strip()
GITHUB_REPO = os.getenv("GITHUB_REPO", "").strip()
GITHUB_BRANCH = os.getenv("GITHUB_BRANCH", "main").strip() or "main"

ALLOWED_GITHUB_FILES = {
    "main.py",
    "config.py",
    "database.py",
    "ai_engine.py",
    "memory_manager.py",
    "task_manager.py",
    "group_news_search.py",
    "voice_handler.py",
    "github_ops.py",
}

_PENDING_PATCHES: dict[str, dict] = {}
_PENDING_GETFILES: dict[str, str] = {}
_PENDING_DEPLOY_FILE = "/tmp/thinzar_pending_deploy.json"
_DEPLOY_WATCHDOG_TASK: asyncio.Task | None = None


def _github_headers() -> dict:
    return {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _normalize_target_filename(raw_name: str) -> str:
    cleaned = (raw_name or "").strip().strip("`").lstrip("/")
    if not cleaned:
        return "main.py"
    if not cleaned.endswith(".py") and f"{cleaned}.py" in ALLOWED_GITHUB_FILES:
        return f"{cleaned}.py"
    return cleaned


def _normalize_python_whitespace(code_text: str) -> str:
    cleaned = code_text.replace("\r\n", "\n").replace("\r", "\n")
    cleaned = re.sub(r"[\u00a0\u200b\u200c\u200d\ufeff]", " ", cleaned)
    lines = []
    for ln in cleaned.split("\n"):
        stripped_leading = ln.lstrip("\t")
        tab_cnt = len(ln) - len(stripped_leading)
        fixed_ln = ("    " * tab_cnt) + stripped_leading
        lines.append(fixed_ln.rstrip())
    return "\n".join(lines) + ("\n" if cleaned.endswith("\n") else "")


def _auto_fix_python_syntax(code_text: str, file_path: str, max_retries: int = 2) -> tuple[bool, str, str]:
    cur_code = _normalize_python_whitespace(code_text)
    for _ in range(max_retries + 1):
        try:
            ast.parse(cur_code)
            return True, cur_code, ""
        except SyntaxError as se:
            err_line_no = se.lineno or 1
            src_lines = cur_code.splitlines()
            start_i = max(0, err_line_no - 18)
            end_i = min(len(src_lines), err_line_no + 18)
            numbered_snippet = "\n".join(
                f"{idx + 1}: {src_lines[idx]}" for idx in range(start_i, end_i)
            )
            fix_prompt = (
                f"Fix ONLY the Python SyntaxError/IndentationError in `{file_path}`.\n"
                f"Error at line {se.lineno}: {se.msg}\n"
                "Do NOT change business logic, variable names, or unrelated code.\n"
                "Return ONLY the corrected full Python code without markdown fences.\n\n"
                f"Context around line {se.lineno}:\n{numbered_snippet}\n\n"
                f"Full code:\n{cur_code}"
            )
            ai_fixed = (call_ai_with_3tier_fallback(fix_prompt) or "").strip()
            ai_fixed = re.sub(r"^```(?:python)?\s*\n?", "", ai_fixed)
            ai_fixed = re.sub(r"\n?```\s*$", "", ai_fixed)
            if not ai_fixed or "ခဏအလုပ်များနေပါတယ်" in ai_fixed:
                return False, cur_code, f"Syntax error — line {se.lineno}: {se.msg}"
            cur_code = _normalize_python_whitespace(ai_fixed)
    try:
        ast.parse(cur_code)
        return True, cur_code, ""
    except SyntaxError as se:
        return False, cur_code, f"Syntax error — line {se.lineno}: {se.msg}"


def get_github_file(file_path: str, branch: str | None = None) -> dict:
    clean_path = _normalize_target_filename(file_path)
    if not GITHUB_TOKEN or not GITHUB_REPO:
        return {"ok": False, "error": "GITHUB_TOKEN သို့မဟုတ် GITHUB_REPO မသတ်မှတ်ရသေးပါရှင်"}
    if clean_path not in ALLOWED_GITHUB_FILES:
        return {
            "ok": False,
            "error": f"'{clean_path}' ကို ခွင့်ပြုထားသော ဖိုင်စာရင်းထဲ ထည့်မထားပါရှင် ({', '.join(sorted(ALLOWED_GITHUB_FILES))})",
        }

    ref = branch or GITHUB_BRANCH
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{clean_path}"
    try:
        resp = requests.get(url, headers=_github_headers(), params={"ref": ref}, timeout=12)
        if resp.status_code != 200:
            return {"ok": False, "error": f"GitHub read failed ({resp.status_code}): {resp.text[:180]}"}
        data = resp.json()
        raw_b64 = (data.get("content") or "").replace("\n", "")
        content_str = base64.b64decode(raw_b64).decode("utf-8") if raw_b64 else ""
        return {"ok": True, "path": clean_path, "sha": data.get("sha"), "content": content_str}
    except Exception as e:
        return {"ok": False, "error": f"GitHub read error: {type(e).__name__}: {str(e)[:150]}"}


def upsert_github_file(
    file_path: str,
    new_content: str,
    commit_message: str = "Update file via Thinzar",
    branch: str | None = None,
) -> dict:
    clean_path = _normalize_target_filename(file_path)
    if not GITHUB_TOKEN or not GITHUB_REPO:
        return {"ok": False, "error": "GITHUB_TOKEN သို့မဟုတ် GITHUB_REPO မသတ်မှတ်ရသေးပါရှင်"}
    if clean_path not in ALLOWED_GITHUB_FILES:
        return {"ok": False, "error": f"'{clean_path}' ကို ခွင့်ပြုထားသော ဖိုင်စာရင်းထဲ ထည့်မထားပါရှင်"}

    if clean_path.endswith(".py"):
        ok_fix, fixed_code, fix_err = _auto_fix_python_syntax(new_content, clean_path)
        if not ok_fix:
            return {"ok": False, "error": f"Python {fix_err}"}
        new_content = fixed_code

    ref = branch or GITHUB_BRANCH
    existing = get_github_file(clean_path, branch=ref)
    sha = existing.get("sha") if existing.get("ok") else None

    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{clean_path}"
    encoded = base64.b64encode(new_content.encode("utf-8")).decode("ascii")
    payload = {
        "message": (commit_message or f"Update {clean_path}")[:120],
        "content": encoded,
        "branch": ref,
    }
    if sha:
        payload["sha"] = sha

    try:
        resp = requests.put(url, headers=_github_headers(), json=payload, timeout=15)
        if resp.status_code not in (200, 201):
            return {"ok": False, "error": f"GitHub commit failed ({resp.status_code}): {resp.text[:180]}"}
        res_json = resp.json()
        commit_sha = ((res_json.get("commit") or {}).get("sha") or "")[:7]
        _save_pending_deploy_info(clean_path, commit_sha)
        return {"ok": True, "path": clean_path, "commit": commit_sha, "branch": ref}
    except Exception as e:
        return {"ok": False, "error": f"GitHub commit error: {type(e).__name__}: {str(e)[:150]}"}


def _parse_patch_hunks(raw_body: str) -> list[tuple[str, str]]:
    if not raw_body:
        return []

    hunks: list[tuple[str, str]] = []

    if "<<<<<<< SEARCH" in raw_body and "=======" in raw_body and ">>>>>>> REPLACE" in raw_body:
        pattern = re.compile(r"<<<<<<< SEARCH\s*\r?\n(.*?)\r?\n=======\s*\r?\n(.*?)\r?\n>>>>>>> REPLACE", re.DOTALL)
        for m in pattern.finditer(raw_body):
            old_b = m.group(1).strip("\r\n")
            new_b = m.group(2).strip("\r\n")
            if old_b:
                hunks.append((old_b, new_b))
        if hunks:
            return hunks

    if "<<<OLD>>>" in raw_body and "<<<NEW>>>" in raw_body:
        chunks = raw_body.split("<<<OLD>>>")
        for part in chunks[1:]:
            if "<<<NEW>>>" not in part:
                continue
            old_raw, new_raw = part.split("<<<NEW>>>", 1)
            old_clean = old_raw.strip("\r\n")
            new_clean = new_raw.strip("\r\n")
            if old_clean:
                hunks.append((old_clean, new_clean))

    return hunks


def _align_new_block_indent(matched_old_block: str, new_block: str) -> str:
    old_lines = matched_old_block.splitlines()
    new_lines = new_block.splitlines()
    if not old_lines or not new_lines:
        return new_block

    first_old = old_lines[0]
    target_indent_len = len(first_old) - len(first_old.lstrip(" "))
    target_indent = " " * max(0, target_indent_len)

    non_empty = [ln for ln in new_lines if ln.strip()]
    if not non_empty:
        return "\n".join(["" for _ in new_lines])

    min_new_indent = min((len(ln) - len(ln.lstrip(" ")) for ln in non_empty), default=0)
    aligned = []
    for ln in new_lines:
        if not ln.strip():
            aligned.append("")
        else:
            cur_ind = len(ln) - len(ln.lstrip(" "))
            rel_extra = max(0, cur_ind - min_new_indent)
            aligned.append(target_indent + (" " * rel_extra) + ln.lstrip(" "))
    return "\n".join(aligned)


def _find_matching_block_in_text(source_text: str, old_block: str) -> tuple[str | None, str]:
    src_norm = source_text.replace("\r\n", "\n")
    old_norm = old_block.replace("\r\n", "\n")

    if not old_norm.strip():
        return None, "အစားထိုးမယ့် မူရင်းစာသား (OLD/SEARCH) လိုအပ်ပါတယ်ရှင်"

    exact_cnt = src_norm.count(old_norm)
    if exact_cnt == 1:
        return old_norm, ""
    if exact_cnt > 1:
        return None, f"တူညီနေတဲ့ စာသား {exact_cnt} နေရာ တွေ့နေပါတယ်၊ ပိုတိကျအောင် ထည့်ပေးပါရှင်"

    old_lines = [ln.strip() for ln in old_norm.splitlines()]
    while old_lines and old_lines[0] == "":
        old_lines.pop(0)
    while old_lines and old_lines[-1] == "":
        old_lines.pop()

    src_lines = src_norm.splitlines()
    if not old_lines or len(src_lines) < len(old_lines):
        return None, "အစားထိုးမယ့် မူရင်းစာသားကို ဖိုင်ထဲမှာ မတွေ့ပါဘူးရှင်"

    candidates: list[str] = []
    n_old = len(old_lines)
    for i in range(len(src_lines) - n_old + 1):
        win = src_lines[i:i + n_old]
        if [w.strip() for w in win] == old_lines:
            candidates.append("\n".join(win))

    if len(candidates) == 1:
        return candidates[0], ""
    if len(candidates) > 1:
        return None, f"Line-level မှာ တူညီနေတဲ့ စာသား {len(candidates)} နေရာ တွေ့နေပါတယ်၊ ပိုရှည်ပြီး တိကျတဲ့ line များ ထည့်ပေးပါရှင်"

    return None, "အစားထိုးမယ့် မူရင်းစာသားကို ဖိုင်ထဲမှာ မတွေ့ပါဘူးရှင်"


def apply_hunks_to_source(source_text: str, hunks: list[tuple[str, str]]) -> tuple[bool, str, str]:
    cur_text = _normalize_python_whitespace(source_text)
    for idx, (old_b, new_b) in enumerate(hunks, start=1):
        old_clean = _normalize_python_whitespace(old_b).strip("\n")
        new_clean = _normalize_python_whitespace(new_b).strip("\n")
        matched_old, err = _find_matching_block_in_text(cur_text, old_clean)
        if not matched_old:
            return False, cur_text, f"Hunk #{idx}: {err}"
        aligned_new = _align_new_block_indent(matched_old, new_clean)
        cur_text = cur_text.replace(matched_old, aligned_new, 1)
    return True, _normalize_python_whitespace(cur_text), ""


def _read_pending_deploy_info() -> dict | None:
    if not os.path.exists(_PENDING_DEPLOY_FILE):
        return None
    try:
        with open(_PENDING_DEPLOY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _clear_pending_deploy_info():
    try:
        if os.path.exists(_PENDING_DEPLOY_FILE):
            os.remove(_PENDING_DEPLOY_FILE)
    except Exception:
        pass


def _save_pending_deploy_info(file_path: str, commit_sha: str):
    try:
        payload = {
            "file_path": file_path,
            "commit": commit_sha,
            "pushed_ts": time.time(),
            "pushed_at": datetime.now(MM_TZ).strftime("%Y-%m-%d %H:%M MMT"),
            "fail_notified": False,
        }
        with open(_PENDING_DEPLOY_FILE, "w", encoding="utf-8") as f:
            json.dump(payload, f)
    except Exception as e:
        print(f"Pending deploy save error: {e}")


async def _deploy_watchdog_loop(application):
    while True:
        try:
            info = _read_pending_deploy_info()
            if ADMIN_USER_ID and info and not info.get("fail_notified"):
                pushed_ts = float(info.get("pushed_ts") or 0)
                if pushed_ts > 0 and (time.time() - pushed_ts) >= 210:
                    info["fail_notified"] = True
                    try:
                        with open(_PENDING_DEPLOY_FILE, "w", encoding="utf-8") as f:
                            json.dump(info, f)
                    except Exception:
                        pass

                    f_path = info.get("file_path") or "code"
                    c_sha = info.get("commit") or ""
                    commit_part = f" (`{c_sha}`)" if c_sha else ""
                    now_str = datetime.now(MM_TZ).strftime("%Y-%m-%d %H:%M MMT")
                    await application.bot.send_message(
                        chat_id=ADMIN_USER_ID,
                        text=(
                            "⚠️ Render Deploy Fail / မတက်သေးပါရှင်!\n"
                            f"• File: `{f_path}`{commit_part}\n"
                            f"• Pushed: {info.get('pushed_at') or '-'}\n"
                            f"• Checked: {now_str}\n"
                            "Build/Start error သို့မဟုတ် Render deploy ကြန့်ကြာနေနိုင်လို့ Render Logs ကို စစ်ကြည့်ပေးပါဦးနော်။"
                        ),
                    )
        except Exception as e:
            print(f"Deploy watchdog error: {e}")

        await asyncio.sleep(25)


async def notify_render_deploy_if_pending(application):
    global _DEPLOY_WATCHDOG_TASK
    if not _DEPLOY_WATCHDOG_TASK or _DEPLOY_WATCHDOG_TASK.done():
        _DEPLOY_WATCHDOG_TASK = asyncio.create_task(_deploy_watchdog_loop(application))

    if not ADMIN_USER_ID:
        return

    info = _read_pending_deploy_info()
    now_str = datetime.now(MM_TZ).strftime("%Y-%m-%d %H:%M MMT")

    if not info:
        try:
            await application.bot.send_message(
                chat_id=ADMIN_USER_ID,
                text=f"🚀 သင်ဇာ (Thinzar) Render ပေါ်မှာ Auto-Deploy ပြီးစီးပြီး Online ဖြစ်သွားပါပြီရှင် ({now_str})",
            )
        except Exception as e:
            print(f"Startup deploy notify error: {e}")
        return

    _clear_pending_deploy_info()
    f_path = info.get("file_path") or "code"
    c_sha = info.get("commit") or ""
    commit_part = f" (`{c_sha}`)" if c_sha else ""

    try:
        if info.get("fail_notified"):
            await application.bot.send_message(
                chat_id=ADMIN_USER_ID,
                text=(
                    "🔄 Render Deploy နောက်ကျမှ ပြန်တက်လာပါပြီရှင်!\n"
                    f"• File: `{f_path}`{commit_part}\n"
                    f"• Pushed: {info.get('pushed_at') or '-'}\n"
                    f"• Live Time: {now_str}"
                ),
            )
        else:
            await application.bot.send_message(
                chat_id=ADMIN_USER_ID,
                text=(
                    "🚀 Render Deploy Success ဖြစ်သွားပါပြီရှင်!\n"
                    f"• File: `{f_path}`{commit_part}\n"
                    f"• Time: {now_str}"
                ),
            )
    except Exception as e:
        print(f"Deploy notify error: {e}")


async def handle_patch_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or update.effective_user.id != ADMIN_USER_ID:
        return

    raw_msg = _normalize_python_whitespace(update.message.text or "")
    body = raw_msg.split(None, 1)[1] if len(raw_msg.split(None, 1)) > 1 else ""
    if not body.strip():
        await update.message.reply_text(
            "Patch အသုံးပြုပုံ:\n"
            "/patch voice_handler.py | commit message\n"
            "<<<<<<< SEARCH\nမူရင်း code\n=======\nအသစ်ပြင်မယ့် code\n>>>>>>> REPLACE"
        )
        return

    first_line = body.splitlines()[0].strip()
    header_parts = [x.strip() for x in first_line.split("|", 1)]
    target_file = _normalize_target_filename(header_parts[0])
    commit_msg = header_parts[1] if len(header_parts) > 1 and header_parts[1] else f"Patch {target_file}"
    patch_body = body.split("\n", 1)[1] if "\n" in body else ""

    hunks = _parse_patch_hunks(patch_body)
    if not hunks:
        await update.message.reply_text("❌ SEARCH / REPLACE (သို့မဟုတ် OLD / NEW) block ကို မှန်ကန်စွာ ထည့်ပေးပါရှင်")
        return

    cur_file = await asyncio.to_thread(get_github_file, target_file, GITHUB_BRANCH)
    if not cur_file.get("ok"):
        await update.message.reply_text(f"❌ {cur_file.get('error')}")
        return

    ok_apply, preview_full, apply_err = apply_hunks_to_source(cur_file.get("content") or "", hunks)
    if not ok_apply:
        await update.message.reply_text(f"❌ {apply_err}")
        return

    if target_file.endswith(".py"):
        ok_fix, fixed_full, fix_err = _auto_fix_python_syntax(preview_full, target_file)
        if not ok_fix:
            await update.message.reply_text(f"❌ {fix_err}")
            return
        preview_full = fixed_full

    req_id = uuid.uuid4().hex[:10]
    _PENDING_PATCHES[req_id] = {
        "file_path": target_file,
        "updated_content": preview_full,
        "commit_message": commit_msg,
        "branch": GITHUB_BRANCH,
        "created_ts": time.time(),
    }

    old_chars = sum(len(o) for o, _ in hunks)
    new_chars = sum(len(n) for _, n in hunks)
    preview_msg = (
        f"⚠️ GitHub Patch အတည်ပြုချက် လိုအပ်ပါတယ်ရှင်\n"
        f"• File: `{target_file}` (`{GITHUB_BRANCH}`)\n"
        f"• Commit: `{commit_msg}`\n"
        f"• Hunks: {len(hunks)} ခု | OLD: {old_chars} chars → NEW: {new_chars} chars"
    )
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Confirm & Push", callback_data=f"patchok:{req_id}"),
            InlineKeyboardButton("❌ Cancel", callback_data=f"patchno:{req_id}"),
        ]
    ])
    await update.message.reply_text(preview_msg, reply_markup=kb)


async def handle_patch_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query or not query.from_user:
        return
    if query.from_user.id != ADMIN_USER_ID:
        await query.answer("Unauthorized.", show_alert=True)
        return

    await query.answer()
    data = query.data or ""
    if ":" not in data:
        return

    action_key, req_id = data.split(":", 1)
    pending = _PENDING_PATCHES.pop(req_id, None)
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass

    if not pending:
        await query.message.reply_text("ဒီ Patch request က သက်တမ်းကုန်သွားပါပြီ (သို့မဟုတ်) ပြီးသွားပါပြီရှင်")
        return

    if (time.time() - float(pending.get("created_ts") or 0)) > 900:
        await query.message.reply_text("⏰ ၁၅ မိနစ်ကျော်သွားလို့ ဒီ Patch request ကို ပယ်ဖျက်လိုက်ပါပြီရှင်")
        return

    if action_key == "patchno":
        await query.message.reply_text(f"❌ `{pending.get('file_path')}` patch ကို ပယ်ဖျက်လိုက်ပါပြီရှင်")
        return

    await context.bot.send_chat_action(chat_id=query.message.chat_id, action="typing")
    res = await asyncio.to_thread(
        upsert_github_file,
        pending["file_path"],
        pending["updated_content"],
        pending["commit_message"],
        pending["branch"],
    )
    if res.get("ok"):
        await query.message.reply_text(
            f"✅ `{res['path']}` ကို `{res['branch']}` ပေါ် push တင်ပြီးပါပြီရှင် (`{res['commit']}`)\n"
            "Render deploy ပြီးရင် Success / Fail အသိပေးချက် ပြန်ပို့ပေးပါမယ်ရှင်"
        )
    else:
        await query.message.reply_text(f"❌ Push မအောင်မြင်ပါရှင်:\n{res.get('error')}")


async def getfile_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or update.effective_user.id != ADMIN_USER_ID:
        return

    raw_msg = update.message.text or ""
    arg = raw_msg.split(None, 1)[1].strip() if len(raw_msg.split(None, 1)) > 1 else ""
    if arg:
        target_file = _normalize_target_filename(arg)
        res = await asyncio.to_thread(get_github_file, target_file, GITHUB_BRANCH)
        if not res.get("ok"):
            await update.message.reply_text(f"❌ {res.get('error')}")
            return
        bio = requests.utils.io.BytesIO((res.get("content") or "").encode("utf-8"))
        bio.name = target_file
        await update.message.reply_document(document=bio, filename=target_file, caption=f"📄 {target_file} ({GITHUB_BRANCH})")
        return

    req_id = uuid.uuid4().hex[:8]
    _PENDING_GETFILES[req_id] = GITHUB_BRANCH
    sorted_files = sorted(ALLOWED_GITHUB_FILES)
    rows = [
        [InlineKeyboardButton(f"📄 {fn}", callback_data=f"getfile:{req_id}:{fn}")]
        for fn in sorted_files
    ]
    await update.message.reply_text("ဆွဲချလိုသည့် ဖိုင်ကို ရွေးပါရှင်:", reply_markup=InlineKeyboardMarkup(rows))


async def handle_getfile_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query or not query.from_user:
        return
    if query.from_user.id != ADMIN_USER_ID:
        await query.answer("Unauthorized.", show_alert=True)
        return

    await query.answer()
    parts = (query.data or "").split(":", 2)
    if len(parts) != 3:
        return
    _, req_id, fn = parts
    branch = _PENDING_GETFILES.get(req_id, GITHUB_BRANCH)
    res = await asyncio.to_thread(get_github_file, fn, branch)
    if not res.get("ok"):
        await query.message.reply_text(f"❌ {res.get('error')}")
        return

    bio = requests.utils.io.BytesIO((res.get("content") or "").encode("utf-8"))
    bio.name = fn
    await query.message.reply_document(document=bio, filename=fn, caption=f"📄 {fn} ({branch})")


async def handle_py_file_upload(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """ဖိုင်အလိုက် (.py) document ပို့တင်လျှင် GitHub သို့ တိုက်ရိုက် commit တင်ပေးသည်။"""
    msg = update.message
    if not msg or not msg.document:
        return False
    if update.effective_user.id != ADMIN_USER_ID:
        return False

    file_name = (msg.document.file_name or "").strip()
    if not file_name.endswith(".py"):
        return False

    if file_name not in ALLOWED_GITHUB_FILES:
        await msg.reply_text(
            f"❌ '{file_name}' ကို ခွင့်ပြုထားသော ဖိုင်စာရင်းထဲ ထည့်မထားပါရှင် ({', '.join(sorted(ALLOWED_GITHUB_FILES))})"
        )
        return True

    await context.bot.send_chat_action(chat_id=msg.chat_id, action="typing")
    try:
        tg_file = await msg.document.get_file()
        raw_bytes = bytes(await tg_file.download_as_bytearray())
        code_text = raw_bytes.decode("utf-8")
    except Exception as e:
        await msg.reply_text(f"❌ ဖိုင်ဖတ်ရာမှာ အဆင်မပြေ ဖြစ်သွားပါတယ်ရှင်: {type(e).__name__}")
        return True

    commit_msg = (msg.caption or "").strip()[:120] or f"Upload {file_name} via Thinzar"
    res = await asyncio.to_thread(upsert_github_file, file_name, code_text, commit_msg, GITHUB_BRANCH)
    if res.get("ok"):
        await msg.reply_text(
            f"✅ `{res['path']}` ကို `{res['branch']}` ပေါ် တင်ပြီးပါပြီရှင် (`{res['commit']}`)\n"
            "Render deploy ပြီးရင် Success / Fail အသိပေးချက် ပြန်ပို့ပေးပါမယ်ရှင်"
        )
    else:
        await msg.reply_text(f"❌ GitHub တင်ရာမှာ မအောင်မြင်ပါရှင်:\n{res.get('error')}")
    return True
