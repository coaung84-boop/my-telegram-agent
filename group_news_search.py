import html
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from urllib.parse import quote_plus

import requests

from ai_engine import call_ai_with_3tier_fallback
from config import (
    INTL_RSS_FEEDS,
    INTL_TRUSTED_DOMAINS,
    LOCAL_RSS_FEEDS,
    LOCAL_TRUSTED_DOMAINS,
    MAX_GROUP_MEDIA_AI_PER_HOUR,
    MM_TZ,
    NEWS_CACHE_TTL_SECONDS,
    TAVILY_API_KEY,
    TELEGRAM_NEWS_CHANNELS,
)
from database import supabase
from memory_manager import format_mm_timestamp

_NEWS_CACHE: dict[str, tuple[float, str]] = {}
_GROUP_MEDIA_AI_TIMESTAMPS: list[float] = []


def allow_group_media_ai() -> bool:
    now_ts = time.time()
    cutoff = now_ts - 3600.0
    while _GROUP_MEDIA_AI_TIMESTAMPS and _GROUP_MEDIA_AI_TIMESTAMPS[0] < cutoff:
        _GROUP_MEDIA_AI_TIMESTAMPS.pop(0)
    if len(_GROUP_MEDIA_AI_TIMESTAMPS) >= MAX_GROUP_MEDIA_AI_PER_HOUR:
        return False
    _GROUP_MEDIA_AI_TIMESTAMPS.append(now_ts)
    return True


def log_group_message(chat_id: int, chat_title: str, user_id: int | None, sender_name: str, msg_type: str, content: str):
    if not content or not content.strip():
        return
    try:
        supabase.table("group_messages").insert({
            "chat_id": int(chat_id),
            "chat_title": (chat_title or "")[:120],
            "user_id": int(user_id) if user_id else None,
            "sender_name": (sender_name or "Member")[:100],
            "msg_type": msg_type,
            "content": content.strip()[:2000],
        }).execute()
    except Exception as e:
        print(f"Error logging group message: {e}")


def cleanup_old_group_messages(days: int = 14):
    try:
        cutoff = (datetime.now(MM_TZ) - timedelta(days=days)).isoformat()
        supabase.table("group_messages").delete().lt("created_at", cutoff).execute()
    except Exception as e:
        print(f"Error cleaning group messages: {e}")


def get_recent_group_messages(chat_id: int, hours: int = 24, limit: int = 120) -> list:
    try:
        since_iso = (datetime.now(MM_TZ) - timedelta(hours=hours)).isoformat()
        res = (
            supabase.table("group_messages")
            .select("sender_name, msg_type, content, created_at")
            .eq("chat_id", int(chat_id))
            .gte("created_at", since_iso)
            .order("id", desc=True)
            .limit(limit)
            .execute()
        )
        rows = list(reversed(res.data or []))
        if rows:
            return rows
        res_fb = (
            supabase.table("group_messages")
            .select("sender_name, msg_type, content, created_at")
            .eq("chat_id", int(chat_id))
            .order("id", desc=True)
            .limit(min(limit, 50))
            .execute()
        )
        return list(reversed(res_fb.data or []))
    except Exception as e:
        print(f"Error reading group messages: {e}")
        return []


def search_group_messages_by_keyword(chat_id: int, keyword: str, limit: int = 25) -> list:
    kw = (keyword or "").strip()
    if not kw:
        return []
    try:
        res = (
            supabase.table("group_messages")
            .select("sender_name, msg_type, content, created_at")
            .eq("chat_id", int(chat_id))
            .ilike("content", f"%{kw}%")
            .order("id", desc=True)
            .limit(limit)
            .execute()
        )
        return list(reversed(res.data or []))
    except Exception as e:
        print(f"Error searching group messages: {e}")
        return []


def _format_group_rows_for_ai(rows: list) -> str:
    formatted = []
    for r in rows:
        ts = format_mm_timestamp(r.get("created_at"))
        sender = r.get("sender_name") or "Member"
        mtype = r.get("msg_type") or "text"
        txt = r.get("content") or ""
        formatted.append(f"[{ts}] {sender} ({mtype}): {txt}")
    return "\n".join(formatted)


def summarize_group_chat(chat_id: int, chat_title: str, hours: int = 24) -> str:
    rows = get_recent_group_messages(chat_id, hours=hours, limit=140)
    if not rows:
        return f"**{chat_title or 'Group'}** အတွက် မှတ်တမ်းတင်ထားတဲ့ မက်ဆေ့ချ် မရှိသေးပါဘူးရှင်။"

    transcript = _format_group_rows_for_ai(rows)
    prompt = f"""
သင်သည် Group chat မှတ်တမ်းများကို စနစ်တကျ အကျဉ်းချုပ်ပေးမည့် သင်ဇာ (Thinzar) AI Assistant ဖြစ်သည်။
Group အမည်: {chat_title or 'Group'}
စုစုပေါင်း မက်ဆေ့ချ် အရေအတွက်: {len(rows)}

Group Chat Logs:
{transcript}

အောက်ပါအတိုင်း ရှင်းလင်းပြတ်သားစွာ မြန်မာလို အကျဉ်းချုပ်ရေးပေးပါ:
1. **အဓိက ဆွေးနွေးခဲ့သော အကြောင်းအရာများ** (bullet points)
2. **ဆုံးဖြတ်ချက်များ / Action Items / အရေးကြီး အချက်အလက်များ** (ရှိလျှင်)
3. **သတင်းအချက်အလက် ခိုင်မာမှု အဆင့်** — Group ထဲတွင် တိုက်ရိုက်ပြောဆိုထားသည်များကို `[Verified]`၊ ဆက်စပ်ကောက်ချက်များကို `[Deduced]`၊ မသေချာ/အတည်မပြုရသေးသည်များကို `[Unverified]` ဟု သင့်တော်သလို ခွဲခြားဖော်ပြပါ။
"""
    return call_ai_with_3tier_fallback(prompt)


def answer_group_question_from_logs(chat_id: int, chat_title: str, question: str) -> str:
    kw_matches = search_group_messages_by_keyword(chat_id, question, limit=25)
    recent_rows = get_recent_group_messages(chat_id, hours=72, limit=100)

    seen = set()
    combined = []
    for r in kw_matches + recent_rows:
        key = (r.get("created_at"), r.get("sender_name"), r.get("content"))
        if key not in seen:
            seen.add(key)
            combined.append(r)

    if not combined:
        return f"**{chat_title or 'Group'}** မှတ်တမ်းထဲမှာ သက်ဆိုင်ရာ အချက်အလက် မတွေ့ပါဘူးရှင်။"

    transcript = _format_group_rows_for_ai(combined[-110:])
    prompt = f"""
Group အမည်: {chat_title or 'Group'}
မေးခွန်း: {question}

Group မှတ်တမ်းများ:
{transcript}

အထက်ပါ Group မှတ်တမ်းများကို အခြေခံ၍ တိတိကျကျ ဖြေဆိုပါ။
- မှတ်တမ်းထဲမှာ တိုက်ရိုက်တွေ့ရှိချက်ကို `[Verified]`
- မှတ်တမ်းများမှ ဆက်စပ်သုံးသပ်ချက်ကို `[Deduced]`
- မှတ်တမ်းထဲမှာ မပါဝင်ဘဲ မသေချာသေးသည့်အရာကို `[Unverified]` ဟု ရှင်းလင်းစွာ ဖော်ပြပါ။
"""
    return call_ai_with_3tier_fallback(prompt)


def _clean_html_text(raw_html: str) -> str:
    txt = re.sub(r"<script.*?>.*?</script>", " ", raw_html or "", flags=re.DOTALL | re.IGNORECASE)
    txt = re.sub(r"<style.*?>.*?</style>", " ", txt, flags=re.DOTALL | re.IGNORECASE)
    txt = re.sub(r"<[^>]+>", " ", txt)
    txt = html.unescape(txt)
    txt = re.sub(r"\s+", " ", txt).strip()
    return txt


def fetch_telegram_channel_posts(username: str, limit: int = 5) -> list[dict]:
    url = f"https://t.me/s/{username}"
    try:
        resp = requests.get(
            url,
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=10,
        )
        if resp.status_code != 200:
            return []
        html_text = resp.text
        blocks = re.findall(
            r'<div class="tgme_widget_message_wrap.*?</div>\s*</div>\s*</div>',
            html_text,
            flags=re.DOTALL,
        )
        items = []
        for blk in blocks[-limit:]:
            msg_match = re.search(
                r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>',
                blk,
                flags=re.DOTALL,
            )
            link_match = re.search(
                r'href="(https://t\.me/[^"/]+/\d+)"',
                blk,
            )
            text_clean = _clean_html_text(msg_match.group(1)) if msg_match else ""
            link_clean = link_match.group(1) if link_match else f"https://t.me/{username}"
            if text_clean and len(text_clean) >= 15:
                items.append({
                    "title": text_clean[:140],
                    "summary": text_clean[:320],
                    "url": link_clean,
                })
        return items
    except Exception as e:
        print(f"[TG Channel Fetch Error @{username}]: {e}")
        return []


def fetch_rss_items(feed_url: str, source_name: str, limit: int = 5) -> list[dict]:
    try:
        resp = requests.get(
            feed_url,
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=10,
        )
        if resp.status_code != 200 or not resp.content:
            return []
        root = ET.fromstring(resp.content)
        items = []

        for item in root.findall(".//item")[:limit]:
            title = _clean_html_text(item.findtext("title") or "")
            desc = _clean_html_text(item.findtext("description") or "")
            link = (item.findtext("link") or "").strip()
            if title:
                items.append({
                    "source": source_name,
                    "title": title[:160],
                    "summary": desc[:280],
                    "url": link,
                })

        if not items:
            ns = {"atom": "http://www.w3.org/2005/Atom"}
            for entry in root.findall(".//atom:entry", ns)[:limit]:
                title = _clean_html_text(entry.findtext("atom:title", namespaces=ns) or "")
                summary = _clean_html_text(
                    entry.findtext("atom:summary", namespaces=ns)
                    or entry.findtext("atom:content", namespaces=ns)
                    or ""
                )
                link_el = entry.find("atom:link", ns)
                link = (link_el.attrib.get("href") if link_el is not None else "") or ""
                if title:
                    items.append({
                        "source": source_name,
                        "title": title[:160],
                        "summary": summary[:280],
                        "url": link,
                    })

        return items
    except Exception as e:
        print(f"[RSS Fetch Error {source_name}]: {e}")
        return []


def tavily_web_search(query: str, max_results: int = 6, include_domains: list[str] | None = None) -> list[dict]:
    if not TAVILY_API_KEY:
        return []
    try:
        payload = {
            "api_key": TAVILY_API_KEY,
            "query": query,
            "search_depth": "basic",
            "max_results": max_results,
            "include_answer": False,
        }
        if include_domains:
            payload["include_domains"] = include_domains
        resp = requests.post("https://api.tavily.com/search", json=payload, timeout=12)
        if resp.status_code != 200:
            return []
        data = resp.json()
        out = []
        for r in data.get("results") or []:
            out.append({
                "title": (r.get("title") or "")[:160],
                "url": r.get("url") or "",
                "content": (r.get("content") or "")[:350],
            })
        return out
    except Exception as e:
        print(f"[Tavily Search Error]: {e}")
        return []


def ddg_instant_search(query: str, max_results: int = 5) -> list[dict]:
    try:
        resp = requests.get(
            "https://api.duckduckgo.com/",
            params={"q": query, "format": "json", "no_html": 1, "skip_disambig": 1},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=10,
        )
        if resp.status_code != 200:
            return []
        data = resp.json()
        results = []
        if data.get("AbstractText"):
            results.append({
                "title": data.get("Heading") or query,
                "url": data.get("AbstractURL") or "",
                "content": data.get("AbstractText")[:350],
            })
        for topic in (data.get("RelatedTopics") or [])[:max_results]:
            if isinstance(topic, dict) and topic.get("Text"):
                results.append({
                    "title": topic.get("Text")[:120],
                    "url": topic.get("FirstURL") or "",
                    "content": topic.get("Text")[:320],
                })
        return results[:max_results]
    except Exception as e:
        print(f"[DDG Search Error]: {e}")
        return []


def collect_myanmar_news_digest() -> str:
    cache_key = "myanmar_news_digest"
    cached = _NEWS_CACHE.get(cache_key)
    if cached and (time.time() - cached[0]) < NEWS_CACHE_TTL_SECONDS:
        return cached[1]

    collected_blocks = []

    tg_lines = []
    for label, uname in TELEGRAM_NEWS_CHANNELS:
        posts = fetch_telegram_channel_posts(uname, limit=4)
        for p in posts:
            tg_lines.append(f"- [TG/{label}] {p['title']} ({p['url']})")
    if tg_lines:
        collected_blocks.append("Telegram Channels:\n" + "\n".join(tg_lines[:12]))

    local_rss_lines = []
    for label, feed_url in LOCAL_RSS_FEEDS:
        items = fetch_rss_items(feed_url, label, limit=3)
        for it in items:
            local_rss_lines.append(f"- [RSS/{it['source']}] {it['title']} — {it['summary'][:140]} ({it['url']})")
    if local_rss_lines:
        collected_blocks.append("Local Myanmar RSS:\n" + "\n".join(local_rss_lines[:12]))

    intl_rss_lines = []
    for label, feed_url in INTL_RSS_FEEDS:
        items = fetch_rss_items(feed_url, label, limit=3)
        for it in items:
            intl_rss_lines.append(f"- [Intl RSS/{it['source']}] {it['title']} — {it['summary'][:140]} ({it['url']})")
    if intl_rss_lines:
        collected_blocks.append("International Burmese RSS:\n" + "\n".join(intl_rss_lines[:9]))

    tav_local = tavily_web_search("Myanmar news today", max_results=5, include_domains=LOCAL_TRUSTED_DOMAINS)
    tav_intl = tavily_web_search("Myanmar news BBC RFA VOA", max_results=4, include_domains=INTL_TRUSTED_DOMAINS)
    tav_lines = [f"- [Web] {r['title']}: {r['content'][:150]} ({r['url']})" for r in (tav_local + tav_intl) if r.get("title")]
    if tav_lines:
        collected_blocks.append("Web Search Results:\n" + "\n".join(tav_lines[:8]))

    raw_corpus = "\n\n".join(collected_blocks)
    now_mm = datetime.now(MM_TZ).strftime("%Y-%m-%d %H:%M")

    if raw_corpus.strip():
        prompt = f"""
အချိန်: {now_mm} (မြန်မာစံတော်ချိန်)
သင်သည် မြန်မာသတင်းများကို စိစစ်အကျဉ်းချုပ်ပေးမည့် သင်ဇာ (Thinzar) AI Assistant ဖြစ်သည်။

Collected Raw News Sources:
{raw_corpus}

အထက်ပါ သတင်းရင်းမြစ်များကို အခြေခံ၍ ယနေ့ မြန်မာ့သတင်း အကျဉ်းချုပ်ကို အောက်ပါအတိုင်း ပြုစုပေးပါ:
- **ပြည်တွင်းသတင်း အကျဉ်းချုပ်** (bullet points, source အမည်နှင့် link ပါလျှင် ထည့်ပါ)
- **နိုင်ငံတကာ / မြန်မာ့အရေး သတင်း** (bullet points)
- သတင်းရင်းမြစ် နှစ်ခုထက်ပို၍ တိုက်ဆိုင်ပါက `[Verified]`၊ ရင်းမြစ်တစ်ခုတည်းမှ လာသော/အတည်ပြုဆဲ သတင်းများကို `[Unverified]`၊ ဆက်စပ်သုံးသပ်ချက်များကို `[Deduced]` ဟု သင့်တော်သလို ဖော်ပြပါ။
- မြန်မာဘာသာဖြင့် ရှင်းလင်းပြေပြစ်စွာ ရေးပါ။
"""
        summary = call_ai_with_3tier_fallback(prompt, use_google_search=True)
    else:
        prompt = f"""
အချိန်: {now_mm} (မြန်မာစံတော်ချိန်)
ယနေ့ မြန်မာနိုင်ငံနှင့် ပတ်သက်သည့် နောက်ဆုံးရ သတင်းများကို ရှာဖွေ၍ ပြည်တွင်းသတင်းနှင့် နိုင်ငံတကာသတင်း ခေါင်းစဉ်များ အကျဉ်းချုပ်ကို မြန်မာလို ပြုစုပေးပါ။
သတင်းတစ်ခုချင်းစီတွင် `[Verified]` သို့မဟုတ် `[Unverified]` tag ထည့်ပါ။
"""
        summary = call_ai_with_3tier_fallback(prompt, use_google_search=True)

    _NEWS_CACHE[cache_key] = (time.time(), summary)
    return summary


def perform_web_search_summary(query: str) -> str:
    q = (query or "").strip()
    if not q:
        return "ရှာဖွေလိုသည့် အကြောင်းအရာလေး ရေးပေးပါရှင်။"

    tav_results = tavily_web_search(q, max_results=6)
    ddg_results = ddg_instant_search(q, max_results=4) if not tav_results else []
    merged = tav_results + ddg_results

    evidence_lines = []
    for idx, item in enumerate(merged, start=1):
        evidence_lines.append(
            f"{idx}. {item.get('title')} | {item.get('content')} | URL: {item.get('url')}"
        )
    evidence_block = "\n".join(evidence_lines) if evidence_lines else "(No external snippets returned from Tavily/DDG)"

    prompt = f"""
User Search Query: "{q}"

Search Evidence:
{evidence_block}

အထက်ပါ ရှာဖွေတွေ့ရှိချက်များနှင့် Google Search တို့ကို အသုံးပြု၍ အသုံးပြုသူ၏ မေးခွန်း/ရှာဖွေမှုကို မြန်မာဘာသာဖြင့် ရှင်းလင်းပြည့်စုံစွာ ဖြေဆိုပေးပါ။
- ခိုင်မာသော အချက်အလက်များကို `[Verified]`
- ဆက်စပ်သုံးသပ်ချက်များကို `[Deduced]`
- မသေချာသေးသော/အတည်မပြုနိုင်သေးသော အချက်များကို `[Unverified]` ဟု tag ထည့်ဖော်ပြပါ။
- သက်ဆိုင်ရာ link များ ရှိလျှင် ဖော်ပြပေးပါ။
"""
    return call_ai_with_3tier_fallback(prompt, use_google_search=True)