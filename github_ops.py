import io
import os
import re
import json
import base64
import requests
from datetime import datetime
from telegram import Update, InputFile, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from config import (
    ADMIN_USER_ID,
    GITHUB_TOKEN,
    GITHUB_REPO,
    GITHUB_BRANCH,
    ALLOWED_GITHUB_FILES,
    MM_TZ,
)
from database import supabase

_PENDING_PATCHES: dict[str, dict] = {}
_PENDING_DEPLOY_MEM: dict | None = None
_PATCH_SEQ = 0


def _github_headers() -> dict:
    return {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _github_get_file(file_path: str) -> tuple[str, str | None]:
    """GitHub repo ထဲမှ file content (utf-8) နှင့် sha ကို ဖတ်ယူသည်။"""
    if not GITHUB_TOKEN or not GITHUB_REPO:
        raise RuntimeError("GITHUB_TOKEN သို့မဟုတ် GITHUB_REPO environment variable မထည့်ရသေးပါ။")
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{file_path}"
    params = {"ref": GITHUB_BRANCH}
    resp = requests.get(url, headers=_github_headers(), params=params, timeout=15)
    if resp.status_code != 200:
        raise RuntimeError(f"GitHub GET {file_path} failed ({resp.status_code}): {resp.text[:180]}")
    data = resp.json()
    sha = data.get("sha")
    raw_b64 = (data.get("content") or "").replace("\n", "")
    content = base64.b64decode(raw_b64).decode("utf-8")
    return content, sha


def _github_put_file(file_path: str, new_content: str, sha: str | None, commit_message: str) -> dict:
    """GitHub repo ထဲသို့ file content အသစ်ကို commit & push လုပ်သည်။"""
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{file_path}"
    encoded = base64.b64encode(new_content.encode("utf-8")).decode("utf-8")
    payload = {
        "message": commit_message,
        "content": encoded,
        "branch": GITHUB_BRANCH,
    }
    if sha:
        payload["sha"] = sha
    resp = requests.put(url, headers=_github_headers(), json=payload, timeout=20)
    if resp.status_code not in (200, 201):
        raise RuntimeError(f"GitHub PUT {file_path} failed ({resp.status_code}): {resp.text[:200]}")
    return resp.json()


def _parse_unified_or_search_replace_patch(patch_text: str) -> list[tuple[str, str]]:
    """
    Patch format နှစ်မျိုးလုံးကို ပံ့ပိုးပေးသည်:
    1) SEARCH / REPLACE blocks:
       <<<<<<< SEARCH
       old code
       =======
       new code
       >>>>>>> REPLACE
    2) Unified diff hunks (@@ ... @@ with '-' and '+' lines)
    """
    hunks: list[tuple[str, str]] = []

    sr_pattern = re.compile(
        r"<<<<<<< SEARCH\s*\n(.*?)\n=======\s*\n(.*?)\n>>>>>>> REPLACE",
        re.DOTALL,
    )
    for m in sr_pattern.finditer(patch_text):
        hunks.append((m.group(1), m.group(2)))
    if hunks:
        return hunks

    lines = patch_text.splitlines()
    old_buf: list[str] = []
    new_buf: list[str] = []
    in_hunk = False

    for line in lines:
        if line.startswith("@@"):
            if old_buf or new_buf:
                hunks.append(("\n".join(old_buf), "\n".join(new_buf)))
                old_buf, new_buf = [], []
            in_hunk = True
            continue
        if not in_hunk:
            continue
        if line.startswith("---") or line.startswith("+++"):
            continue
        if line.startswith("-"):
            old_buf.append(line[1:])
        elif line.startswith("+"):
            new_buf.append(line[1:])
        elif line.startswith(" "):
            old_buf.append(line[1:])
            new_buf.append(line[1:])

    if old_buf or new_buf:
        hunks.append(("\n".join(old_buf), "\n".join(new_buf)))

    return hunks


def _apply_hunks_to_content(original_content: str, hunks: list[tuple[str, str]]) -> tuple[str, int, list[str]]:
    updated = original_content
    applied = 0
    failed = []

    for idx, (old_block, new_block) in enumerate(hunks, start=1):
        if not old_block and new_block:
            updated = updated.rstrip() + "\n\n" + new_block.lstrip("\n") + "\n"
            applied += 1
            continue

        if old_block in updated:
            updated = updated.replace(old_block, new_block, 1)
            applied += 1
        else:
            norm_old = re.sub(r"[ \t]+", " ", old_block.strip())
            norm_upd = re.sub(r"[ \t]+", " ", updated)
            if norm_old and norm_old in norm_upd:
                failed.append(f"Hunk #{idx}: whitespace/indentation mismatch")
            else:
                failed.append(f"Hunk #{idx}: target block not found")

    return updated, applied, failed


def _save_pending_deploy_info(info: dict):
    """GitHub push ပြီးနောက် Render deploy ပြီးလျှင် အသိပေးရန် pending deploy info သိမ်းသည်။"""
    global _PENDING_DEPLOY_MEM
    _PENDING_DEPLOY_MEM = info
    try:
        payload_str = json.dumps(info, ensure_ascii=False)
        supabase.table("daily_reports").insert({
            "report_content": f"[RENDER_DEPLOY_PENDING] {payload_str}"
        }).execute()
    except Exception as e:
        print(f"Warning: could not persist pending deploy marker to DB: {e}")


def _pop_pending_deploy_info() -> dict | None:
    """Startup ချိန်တွင် pending deploy info ရှိလျှင် ယူပြီး ရှင်းလင်းသည်။"""
    global _PENDING_DEPLOY_MEM
    found = _PENDING_DEPLOY_MEM
    _PENDING_DEPLOY_MEM = None
    try:
        res = (
            supabase.table("daily_reports")
            .select("id, report_content")
            .like("report_content", "[RENDER_DEPLOY_PENDING]%")
            .order("id", desc=True)
            .limit(10)
            .execute()
        )
        rows = res.data or []
        for r in rows:
            raw_txt = str(r.get("report_content") or "")
            json_part = raw_txt.replace("[RENDER_DEPLOY_PENDING]", "", 1).strip()
            if not found and json_part:
                try:
                    found = json.loads(json_part)
                except Exception:
                    pass
            try:
                supabase.table("daily_reports").delete().eq("id", r["id"]).execute()
            except Exception:
                pass
    except Exception as e:
        print(f"Warning: could not read/clear pending deploy marker from DB: {e}")
    return found


async def notify_render_deploy_if_pending(app):
    """Render deploy ပြီး၍ bot startup တက်လာချိန်တွင် pending deploy ရှိပါက Telegram ၌ အသိပေးသည်။"""
    info = _pop_pending_deploy_info()
    if not info or not ADMIN_USER_ID:
        return
    file_name = info.get("file", "code")
    sha = info.get("sha", "")
    hunks_info = info.get("hunks", "")
    now_mm = datetime.now(MM_TZ).strftime("%Y-%m-%d %H:%M MMT")
    lines = [
        "🚀 Render Deploy Success ဖြစ်သွားပါပြီရှင်!",
        f"• File: `{file_name}`",
    ]
    if sha:
        lines.append(f"• Commit: `{sha}`")
    if hunks_info:
        lines.append(f"• Hunks: {hunks_info}")
    lines.append(f"• Time: {now_mm}")
    try:
        await app.bot.send_message(chat_id=ADMIN_USER_ID, text="\n".join(lines), parse_mode="Markdown")
    except Exception as e:
        print(f"Error sending Render deploy notification: {e}")


async def _send_github_file(target_file: str, reply_target):
    content, _ = _github_get_file(target_file)
    bio = io.BytesIO(content.encode("utf-8"))
    bio.name = target_file
    await reply_target.reply_document(
        document=InputFile(bio, filename=target_file),
        caption=f"📄 `{target_file}` (`{GITHUB_REPO}@{GITHUB_BRANCH}`)",
        parse_mode="Markdown",
    )


async def handle_patch_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /patch <filename> ဖြင့် patch ကို dry-run စစ်ပြီး Confirm / Cancel button ဖြင့် အတည်ပြုခိုင်းသည်။
    """
    global _PATCH_SEQ
    user_id = update.effective_user.id
    if user_id != ADMIN_USER_ID:
        return

    raw_msg = update.message.text or ""
    lines = raw_msg.splitlines()
    first_line_parts = lines[0].strip().split(maxsplit=1) if lines else []
    target_file = first_line_parts[1].strip() if len(first_line_parts) > 1 else "main.py"
    target_file = os.path.basename(target_file)

    if target_file not in ALLOWED_GITHUB_FILES:
        allowed_str = ", ".join(sorted(ALLOWED_GITHUB_FILES))
        await update.message.reply_text(
            f"'{target_file}' ကို patch ခွင့်မပြုထားပါဘူးရှင်\nခွင့်ပြုထားသော ဖိုင်များ: {allowed_str}"
        )
        return

    patch_body = "\n".join(lines[1:]).strip()
    if not patch_body and update.message.reply_to_message and update.message.reply_to_message.text:
        patch_body = update.message.reply_to_message.text.strip()

    if not patch_body:
        await update.message.reply_text(
            "Patch အချက်အလက် ထည့်ပေးပါရှင်\n"
            "ဥပမာ:\n"
            "/patch main.py\n"
            "<<<<<<< SEARCH\nold code\n=======\nnew code\n>>>>>>> REPLACE"
        )
        return

    hunks = _parse_unified_or_search_replace_patch(patch_body)
    if not hunks:
        await update.message.reply_text("SEARCH/REPLACE block သို့မဟုတ် diff hunk ကို ဖတ်မရပါဘူးရှင်၊ format ပြန်စစ်ပေးပါ")
        return

    try:
        await update.message.reply_text(
            f"🔍 `{target_file}` ({GITHUB_REPO}@{GITHUB_BRANCH}) အတွက် patch preview စစ်နေပါတယ်ရှင်...",
            parse_mode="Markdown",
        )
        current_content, sha = _github_get_file(target_file)
        new_content, applied_count, failed_list = _apply_hunks_to_content(current_content, hunks)

        if applied_count == 0:
            fail_detail = "\n".join(failed_list) if failed_list else "No matching hunks"
            await update.message.reply_text(f"❌ Patch မအောင်မြင်ပါရှင်:\n{fail_detail}")
            return

        _PATCH_SEQ += 1
        patch_id = f"p{_PATCH_SEQ}"
        _PENDING_PATCHES[patch_id] = {
            "target_file": target_file,
            "new_content": new_content,
            "sha": sha,
            "applied_count": applied_count,
            "total_hunks": len(hunks),
            "failed_list": failed_list,
        }

        preview_lines = [
            f"🧩 Patch Confirm လိုအပ်ပါတယ်ရှင် — `{target_file}`",
            f"• Branch: `{GITHUB_BRANCH}`",
            f"• Matched hunks: {applied_count}/{len(hunks)}",
        ]
        if failed_list:
            preview_lines.append("⚠️ မကိုက်ညီသည့် hunk များ: " + ", ".join(failed_list))
        preview_lines.append("\nGitHub သို့ တင်ပြီး Render deploy ဆက်လုပ်မလားရှင်?")

        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Confirm & Push", callback_data=f"patchok:{patch_id}"),
                InlineKeyboardButton("❌ Cancel", callback_data=f"patchno:{patch_id}"),
            ]
        ])
        await update.message.reply_text("\n".join(preview_lines), reply_markup=kb, parse_mode="Markdown")
    except Exception as e:
        print(f"GitHub /patch preview error: {e}")
        await update.message.reply_text(f"❌ Patch စစ်ဆေးရာမှာ error တက်ပါတယ်ရှင်:\n`{str(e)[:240]}`", parse_mode="Markdown")


async def handle_patch_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Patch Confirm / Cancel inline button များကို ကိုင်တွယ်သည်။"""
    query = update.callback_query
    if not query or not query.from_user:
        return
    if query.from_user.id != ADMIN_USER_ID:
        await query.answer()
        return

    data = query.data or ""
    await query.answer()

    if data.startswith("patchno:"):
        patch_id = data.split(":", 1)[1].strip()
        _PENDING_PATCHES.pop(patch_id, None)
        await query.message.edit_text("🛑 Patch တင်ခြင်းကို Cancel လုပ်လိုက်ပါပြီရှင်။")
        return

    if data.startswith("patchok:"):
        patch_id = data.split(":", 1)[1].strip()
        item = _PENDING_PATCHES.pop(patch_id, None)
        if not item:
            await query.message.edit_text("⚠️ ဒီ patch က သက်တမ်းကုန်သွားပါပြီ (သို့မဟုတ်) ပြီးသွားပါပြီရှင်။")
            return

        target_file = item["target_file"]
        new_content = item["new_content"]
        sha = item["sha"]
        applied_count = item["applied_count"]
        total_hunks = item["total_hunks"]
        failed_list = item["failed_list"]

        try:
            await query.message.edit_text(f"⏳ `{target_file}` ကို GitHub (`{GITHUB_BRANCH}`) သို့ push လုပ်နေပါတယ်ရှင်...", parse_mode="Markdown")
            commit_msg = f"bot(/patch): update {target_file} ({applied_count} hunk(s) via သင်ဇာ)"
            res_json = _github_put_file(target_file, new_content, sha, commit_msg)
            commit_sha = ((res_json.get("commit") or {}).get("sha") or "")[:7]

            _save_pending_deploy_info({
                "file": target_file,
                "sha": commit_sha,
                "hunks": f"{applied_count}/{total_hunks}",
            })

            status_msg = (
                f"✅ `{target_file}` ကို GitHub (`{GITHUB_BRANCH}`) မှာ update တင်ပြီးပါပြီရှင်\n"
                f"• Applied hunks: {applied_count}/{total_hunks}\n"
                f"• Commit: `{commit_sha}`\n"
                f"• Render auto-deploy ပြီးသွားရင် ထပ်အသိပေးပါ့မယ်ရှင်"
            )
            if failed_list:
                status_msg += "\n⚠️ မကိုက်ညီသည့် hunk များ:\n" + "\n".join(failed_list)

            await query.message.edit_text(status_msg, parse_mode="Markdown")
        except Exception as e:
            print(f"GitHub patch push error: {e}")
            await query.message.edit_text(f"❌ GitHub push လုပ်ရာမှာ error တက်ပါတယ်ရှင်:\n`{str(e)[:240]}`", parse_mode="Markdown")


async def getfile_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /getfile (သို့မဟုတ် /getfile <filename>) ဖြင့် ဖိုင်ရွေးရန် button ပြခြင်း သို့မဟုတ် ဖိုင်ကို document အဖြစ် ပို့ပေးခြင်း။
    """
    if not update.message or not update.effective_user:
        return
    if update.effective_user.id != ADMIN_USER_ID:
        return

    raw = (update.message.text or "").strip()
    parts = raw.split(maxsplit=1)
    files_sorted = sorted(ALLOWED_GITHUB_FILES)

    if len(parts) < 2 or not parts[1].strip():
        if not files_sorted:
            await update.message.reply_text("ခွင့်ပြုထားသော ဖိုင်စာရင်း မရှိသေးပါဘူးရှင်။")
            return
        rows = []
        for i in range(0, len(files_sorted), 2):
            pair = files_sorted[i:i + 2]
            rows.append([
                InlineKeyboardButton(f"📄 {fname}", callback_data=f"getfile:{fname}")
                for fname in pair
            ])
        await update.message.reply_text(
            "ယူလိုသည့် ဖိုင်ကို ရွေးနှိပ်ပါရှင်:",
            reply_markup=InlineKeyboardMarkup(rows),
        )
        return

    target_file = os.path.basename(parts[1].strip())
    if target_file not in ALLOWED_GITHUB_FILES:
        allowed_str = ", ".join(files_sorted)
        await update.message.reply_text(
            f"'{target_file}' ကို ဖတ်ခွင့်မပြုထားပါဘူးရှင်\nခွင့်ပြုထားသော ဖိုင်များ: {allowed_str}"
        )
        return

    try:
        await _send_github_file(target_file, update.message)
    except Exception as e:
        print(f"GitHub /getfile error: {e}")
        await update.message.reply_text(f"❌ ဖိုင်ယူရာမှာ error တက်ပါတယ်ရှင်:\n`{str(e)[:240]}`", parse_mode="Markdown")


async def handle_getfile_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /getfile button နှိပ်သည့်အခါ သက်ဆိုင်ရာ ဖိုင်ကို ပြန်ပို့ပေးသည်။
    """
    query = update.callback_query
    if not query or not query.from_user:
        return
    if query.from_user.id != ADMIN_USER_ID:
        await query.answer()
        return

    data = query.data or ""
    if not data.startswith("getfile:"):
        return

    target_file = os.path.basename(data.split(":", 1)[1].strip())
    await query.answer("ဖိုင်ယူနေပါတယ်...")

    if target_file not in ALLOWED_GITHUB_FILES:
        await query.message.reply_text(f"'{target_file}' ကို ဖတ်ခွင့်မပြုထားပါဘူးရှင်။")
        return

    try:
        await _send_github_file(target_file, query.message)
    except Exception as e:
        print(f"GitHub getfile callback error: {e}")
        await query.message.reply_text(f"❌ ဖိုင်ယူရာမှာ error တက်ပါတယ်ရှင်:\n`{str(e)[:240]}`", parse_mode="Markdown")
