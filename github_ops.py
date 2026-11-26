import ast
import base64
import re
import requests

from config import ALLOWED_GITHUB_FILES, GITHUB_BRANCH, GITHUB_REPO, GITHUB_TOKEN


def _github_headers() -> dict:
    return {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def get_github_file(file_path: str, branch: str | None = None) -> dict:
    clean_path = (file_path or "").strip().lstrip("/")
    if not GITHUB_TOKEN or not GITHUB_REPO:
        return {"ok": False, "error": "GITHUB_TOKEN သို့မဟုတ် GITHUB_REPO မသတ်မှတ်ရသေးပါရှင်"}
    if not clean_path:
        return {"ok": False, "error": "File path လိုအပ်ပါတယ်ရှင်"}

    ref = branch or GITHUB_BRANCH
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{clean_path}"
    try:
        resp = requests.get(url, headers=_github_headers(), params={"ref": ref}, timeout=10)
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
    commit_message: str = "Update file via Thinzar AI Agent",
    branch: str | None = None,
) -> dict:
    clean_path = (file_path or "").strip().lstrip("/")
    if not GITHUB_TOKEN or not GITHUB_REPO:
        return {"ok": False, "error": "GITHUB_TOKEN သို့မဟုတ် GITHUB_REPO မသတ်မှတ်ရသေးပါရှင်"}
    if clean_path not in ALLOWED_GITHUB_FILES:
        return {"ok": False, "error": f"'{clean_path}' ကို ခွင့်ပြုထားသည့် file စာရင်းထဲ ထည့်မထားပါရှင်"}

    if clean_path.endswith(".py"):
        try:
            ast.parse(new_content)
        except SyntaxError as se:
            return {"ok": False, "error": f"Python syntax error ရှိနေလို့ commit မတင်ခဲ့ပါဘူး — line {se.lineno}: {se.msg}"}

    ref = branch or GITHUB_BRANCH
    existing = get_github_file(clean_path, branch=ref)
    sha = existing.get("sha") if existing.get("ok") else None

    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{clean_path}"
    encoded = base64.b64encode(new_content.encode("utf-8")).decode("ascii")
    payload = {
        "message": commit_message[:120] or "Update file via Thinzar AI Agent",
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
        return {"ok": True, "path": clean_path, "commit": commit_sha, "branch": ref}
    except Exception as e:
        return {"ok": False, "error": f"GitHub commit error: {type(e).__name__}: {str(e)[:150]}"}


def _normalize_mobile_text(s: str) -> str:
    """ဖုန်းကနေ copy ကူးတဲ့အခါ ဖြစ်တတ်တဲ့ \r\n၊ NBSP (\u00a0)၊ smart quotes နဲ့ tabs တွေကို ညှိပေးသည်။"""
    if not s:
        return ""
    s = s.replace("\r\n", "\n").replace("\r", "\n").replace("\u00a0", " ")
    s = s.replace("\t", "    ")
    s = s.replace("“", '"').replace("”", '"').replace("‘", "'").replace("’", "'")
    return s


def parse_patch_hunks(raw_body: str) -> list[tuple[str, str]]:
    if not raw_body or "<<<OLD>>>" not in raw_body or "<<<NEW>>>" not in raw_body:
        return []

    hunks: list[tuple[str, str]] = []
    chunks = raw_body.split("<<<OLD>>>")
    for part in chunks[1:]:
        if "<<<NEW>>>" not in part:
            continue
        old_raw, new_raw = part.split("<<<NEW>>>", 1)
        old_clean = _normalize_mobile_text(old_raw).strip("\n")
        new_clean = _normalize_mobile_text(new_raw).strip("\n")
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
    target_indent = " " * target_indent_len

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


def _collapse_ws(line: str) -> str:
    return re.sub(r"\s+", " ", (line or "").strip())


def _find_matching_block_in_text(source_text: str, old_block: str) -> tuple[str | None, str]:
    src_norm = _normalize_mobile_text(source_text)
    old_norm = _normalize_mobile_text(old_block)

    if not old_norm.strip():
        return None, "အစားထိုးမယ့် မူရင်းစာသား (OLD) လိုအပ်ပါတယ်ရှင်"

    exact_cnt = src_norm.count(old_norm)
    if exact_cnt == 1:
        return old_norm, ""
    if exact_cnt > 1:
        return None, f"တူညီနေတဲ့ စာသား {exact_cnt} နေရာ တွေ့နေပါတယ်၊ ပိုတိကျအောင် ထည့်ပေးပါရှင်"

    old_lines = [ln.strip() for ln in old_norm.splitlines() if ln.strip()]
    src_lines = src_norm.splitlines()
    if not old_lines or len(src_lines) < len(old_lines):
        return None, "အစားထိုးမယ့် မူရင်းစာသားကို file ထဲမှာ မတွေ့ပါဘူးရှင်"

    # Non-empty line-by-line match (blank line ကွာချက်ကို ကျော်လွှားနိုင်ရန်)
    non_empty_indices = [idx for idx, ln in enumerate(src_lines) if ln.strip()]
    n_old = len(old_lines)
    if len(non_empty_indices) >= n_old:
        candidates: list[str] = []
        for pos in range(len(non_empty_indices) - n_old + 1):
            sub_idx = non_empty_indices[pos:pos + n_old]
            win_stripped = [src_lines[i].strip() for i in sub_idx]
            if win_stripped == old_lines:
                start_i = sub_idx[0]
                end_i = sub_idx[-1] + 1
                candidates.append("\n".join(src_lines[start_i:end_i]))

        if len(candidates) == 1:
            return candidates[0], ""
        if len(candidates) > 1:
            return None, f"တူညီနေတဲ့ စာသား {len(candidates)} နေရာ တွေ့နေပါတယ်၊ ပိုရှည်ပြီး တိကျတဲ့ line များ ထည့်ပေးပါရှင်"

        # Whitespace-collapsed match (ဖုန်းကနေ space/quote ပြောင်းသွားရင် ရှာနိုင်ရန်)
        old_collapsed = [_collapse_ws(ln) for ln in old_lines]
        ws_candidates: list[str] = []
        for pos in range(len(non_empty_indices) - n_old + 1):
            sub_idx = non_empty_indices[pos:pos + n_old]
            win_collapsed = [_collapse_ws(src_lines[i]) for i in sub_idx]
            if win_collapsed == old_collapsed:
                start_i = sub_idx[0]
                end_i = sub_idx[-1] + 1
                ws_candidates.append("\n".join(src_lines[start_i:end_i]))

        if len(ws_candidates) == 1:
            return ws_candidates[0], ""
        if len(ws_candidates) > 1:
            return None, f"တူညီနေတဲ့ စာသား {len(ws_candidates)} နေရာ တွေ့နေပါတယ်၊ ပိုတိကျအောင် ထည့်ပေးပါရှင်"

    return None, "အစားထိုးမယ့် မူရင်းစာသားကို file ထဲမှာ မတွေ့ပါဘူးရှင်"


def apply_hunks_to_source(source_text: str, hunks: list[tuple[str, str]]) -> tuple[bool, str, str]:
    cur_text = _normalize_mobile_text(source_text)
    for idx, (old_b, new_b) in enumerate(hunks, start=1):
        matched_old, err = _find_matching_block_in_text(cur_text, old_b)
        if not matched_old:
            return False, cur_text, f"Hunk #{idx}: {err}"
        aligned_new = _align_new_block_indent(matched_old, new_b)
        cur_text = cur_text.replace(matched_old, aligned_new, 1)
    return True, cur_text, ""


def replace_github_snippet(
    file_path: str,
    old_text: str,
    new_text: str,
    commit_message: str = "Patch via Thinzar AI Agent",
    branch: str | None = None,
) -> dict:
    clean_path = (file_path or "").strip().lstrip("/")
    if clean_path not in ALLOWED_GITHUB_FILES:
        return {"ok": False, "error": f"'{clean_path}' ကို ခွင့်ပြုထားသည့် file စာရင်းထဲ ထည့်မထားပါရှင်"}

    current = get_github_file(clean_path, branch=branch)
    if not current.get("ok"):
        return current

    original_content = current.get("content", "")
    hunks = parse_patch_hunks(old_text) if "<<<OLD>>>" in old_text else [(old_text, new_text)]
    if not hunks:
        return {"ok": False, "error": "အစားထိုးမယ့် မူရင်းစာသား (old_text) လိုအပ်ပါတယ်ရှင်"}

    ok, updated_content, err = apply_hunks_to_source(original_content, hunks)
    if not ok:
        return {"ok": False, "error": err}

    return upsert_github_file(clean_path, updated_content, commit_message=commit_message, branch=branch)
