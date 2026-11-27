import re
import time
import html
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
import requests
from bs4 import BeautifulSoup
from tavily import TavilyClient

from config import (
    MM_TZ,
    MYANMAR_WEEKDAYS,
    NEWS_CACHE_TTL_SECONDS,
    MAX_GROUP_MEDIA_AI_PER_HOUR,
    TELEGRAM_NEWS_CHANNELS,
    LOCAL_RSS_FEEDS,
    INTL_RSS_FEEDS,
    LOCAL_TRUSTED_DOMAINS,
    INTL_TRUSTED_DOMAINS,
    TAVILY_API_KEY,
)
from database import supabase
from ai_engine import call_ai_with_3tier_fallback
from memory_manager import _format_mm_timestamp, extract_search_terms

tavily_client = TavilyClient(api_key=TAVILY_API_KEY) if TAVILY_API_KEY else None

# --- Simple In-Memory Cache & Rate-Limit State for Group Media & News ---
_NEWS_CACHE: dict[str, tuple[float, str]] = {}
_GROUP_MEDIA_AI_TIMESTAMPS: list[float] = []


def _now_mmt() -> datetime:
    """မြန်မာစံတော်ချိန် (MMT / +06:30) လက်ရှိအချိန်ကို အမြဲပြန်ပေးသည်။"""
    return datetime.now(MM_TZ)


def _allow_group_media_ai() -> bool:
    now_ts = time.time()
    global _GROUP_MEDIA_AI_TIMESTAMPS
    _GROUP_MEDIA_AI_TIMESTAMPS = [t for t in _GROUP_MEDIA_AI_TIMESTAMPS if now_ts - t < 3600]
    if len(_GROUP_MEDIA_AI_TIMESTAMPS) >= MAX_GROUP_MEDIA_AI_PER_HOUR:
        return False
    _GROUP_MEDIA_AI_TIMESTAMPS.append(now_ts)
    return True


def _get_cached_news(cache_key: str) -> str | None:
    entry = _NEWS_CACHE.get(cache_key)
    if not entry:
        return None
    ts, val = entry
    if time.time() - ts <= NEWS_CACHE_TTL_SECONDS:
        return val
    _NEWS_CACHE.pop(cache_key, None)
    return None


def _set_cached_news(cache_key: str, val: str):
    _NEWS_CACHE[cache_key] = (time.time(), val)


def cleanup_old_group_messages():
    """raw_logs မှ ရက် ၃၀ ကျော်သော record များကို ဖျက်၍ storage ထိန်းညှိသည်။"""
    try:
        cutoff_dt = _now_mmt() - timedelta(days=30)
        supabase.table("raw_logs").delete().lt("created_at", cutoff_dt.isoformat()).execute()
    except Exception as e:
        print(f"Error cleaning up old raw_logs: {e}")


def save_group_message(chat_id: int, chat_title: str, sender_name: str, message_text: str):
    """Group မှ message များကို raw_logs table ထဲ သိမ်းဆည်းသည်။"""
    try:
        clean_title = (chat_title or f"Group {chat_id}").strip()
        clean_sender = (sender_name or "Member").strip()
        clean_text = (message_text or "").strip()
        if not clean_text:
            return
        supabase.table("raw_logs").insert({
            "group_name": clean_title,
            "sender_name": clean_sender,
            "content_summary": clean_text,
            "is_read": False,
        }).execute()
    except Exception as e:
        print(f"Error saving raw_logs: {e}")


def get_available_group_names(limit: int = 1500) -> list:
    """raw_logs ထဲမှ ရက် ၃၀ အတွင်း group_name များကို စုစည်းပေးသည်။"""
    try:
        cutoff_iso = (_now_mmt() - timedelta(days=30)).isoformat()
        res = (
            supabase.table("raw_logs")
            .select("group_name")
            .gte("created_at", cutoff_iso)
            .limit(limit)
            .execute()
        )
        return sorted(list({
            (r.get("group_name") or "").strip()
            for r in (res.data or [])
            if (r.get("group_name") or "").strip()
        }))
    except Exception as e:
        print(f"Error fetching group names from raw_logs: {e}")
        return []


def resolve_group_name_from_text(query_text: str, available_groups: list) -> str | None:
    """User မေးခွန်းထဲက group နာမည်ကို available_groups ထဲက အနီးစပ်ဆုံး group နာမည်နှင့် ညှိပေးသည်။"""
    if not available_groups:
        return None
    q_clean = re.sub(r"[^\w\u1000-\u109F]+", " ", (query_text or "").lower()).strip()
    q_compact = re.sub(r"\s+", "", q_clean)

    for g in available_groups:
        g_low = g.lower()
        g_compact = re.sub(r"[^\w\u1000-\u109F]+", "", g_low)
        if g_low in q_clean or (g_compact and g_compact in q_compact):
            return g

    best_group = None
    best_score = 0
    q_tokens = [t for t in q_clean.split() if len(t) >= 2]
    for g in available_groups:
        g_tokens = [t for t in re.sub(r"[^\w\u1000-\u109F]+", " ", g.lower()).split() if len(t) >= 2]
        score = sum(1 for qt in q_tokens if any(qt in gt or gt in qt for gt in g_tokens))
        if score > best_score:
            best_score = score
            best_group = g
    return best_group if best_score >= 1 else None


def _get_date_bounds_mmt(day_mode: str):
    now_mm = _now_mmt()
    target_date = (now_mm - timedelta(days=1)).date() if day_mode == "yesterday" else now_mm.date()
    label = f"{'Yesterday' if day_mode == 'yesterday' else 'Today'} ({target_date})"
    start_dt = datetime(target_date.year, target_date.month, target_date.day, 0, 0, 0, tzinfo=MM_TZ)
    end_dt = start_dt + timedelta(days=1)
    return start_dt.isoformat(), end_dt.isoformat(), label


def _filter_logs_by_mmt_range(logs: list, start_iso: str, end_iso: str) -> list:
    """Timestamp များကို MMT (+06:30) သို့ ပြောင်းပြီး ရက်စွဲအတိအကျ စစ်ထုတ်သည်။"""
    s_dt = datetime.fromisoformat(start_iso)
    e_dt = datetime.fromisoformat(end_iso)
    out = []
    for l in logs:
        raw_ts = l.get("created_at")
        if not raw_ts:
            continue
        try:
            dt_obj = datetime.fromisoformat(str(raw_ts).replace("Z", "+00:00"))
            dt_mm = dt_obj.replace(tzinfo=MM_TZ) if dt_obj.tzinfo is None else dt_obj.astimezone(MM_TZ)
            if s_dt <= dt_mm < e_dt:
                out.append(l)
        except Exception:
            pass
    return out


def fetch_group_messages(
    group_name: str | None = None,
    query_text: str = "",
    limit: int = 120,
    period: str = "recent",
) -> list:
    """raw_logs မှတ်တမ်းများကို group_name / period (MMT) / query_text ဖြင့် ရှာဖွေသည်။"""
    try:
        cutoff_iso = (_now_mmt() - timedelta(days=30)).isoformat()
        rows = list(reversed(
            supabase.table("raw_logs")
            .select("*")
            .gte("created_at", cutoff_iso)
            .order("id", desc=True)
            .limit(500)
            .execute()
            .data or []
        ))
        if period in ("today", "yesterday"):
            start_iso, end_iso, _ = _get_date_bounds_mmt(period)
            rows = _filter_logs_by_mmt_range(rows, start_iso, end_iso)
    except Exception as e:
        print(f"Error reading raw_logs: {e}")
        return []

    normalized = []
    for r in rows:
        title = (r.get("group_name") or "").strip() or "Group"
        txt = r.get("content_summary") or r.get("message_text") or ""
        normalized.append({
            "chat_title": title,
            "sender_name": r.get("sender_name") or "Member",
            "message_text": txt,
            "created_at": r.get("created_at") or "",
        })

    if group_name:
        g_low = group_name.lower()
        g_comp = re.sub(r"[^\w\u1000-\u109F]+", "", g_low)
        filtered = [
            item for item in normalized
            if g_low in item["chat_title"].lower()
            or (g_comp and g_comp in re.sub(r"[^\w\u1000-\u109F]+", "", item["chat_title"].lower()))
        ]
        if filtered:
            normalized = filtered

    terms = extract_search_terms(query_text)
    raw_tokens = [
        tok for tok in re.findall(r"[\w\u1000-\u109F]+", (query_text or "").lower())
        if len(tok) >= 2 and tok not in {"ဘာလဲ", "ဘာပြော", "ပြောသွား", "ပြောထား", "group", "report", "summary"}
    ]
    search_terms = list(dict.fromkeys(list(terms or []) + raw_tokens))

    if search_terms and period not in ("today", "yesterday"):
        scored = []
        for item in normalized:
            hay = f"{item['chat_title']} {item['sender_name']} {item['message_text']}".lower()
            hits = sum(1 for t in search_terms if t.lower() in hay)
            if hits > 0:
                scored.append((hits, item))
        if scored:
            scored.sort(key=lambda x: x[0], reverse=True)
            top_matched = [x[1] for x in scored[:limit]]
            seen_keys = set((x["chat_title"], x["sender_name"], x["message_text"], x["created_at"]) for x in top_matched)
            for item in normalized[:25]:
                k = (item["chat_title"], item["sender_name"], item["message_text"], item["created_at"])
                if k not in seen_keys and len(top_matched) < limit:
                    seen_keys.add(k)
                    top_matched.append(item)
            return top_matched

    return normalized[:limit]


def get_group_report(user_query: str = "", exact_group_name: str = None, day_mode: str = None) -> str:
    """Maya version အတိုင်း raw_logs မှ MMT ရက်စွဲအလိုက် Group Summary Report ထုတ်ပေးသည်။"""
    cleanup_old_group_messages()
    start_iso, end_iso, date_label = (None, None, None)
    if day_mode in ["today", "yesterday"]:
        start_iso, end_iso, date_label = _get_date_bounds_mmt(day_mode)
    elif "မနေ့" in user_query.lower() or "yesterday" in user_query.lower():
        start_iso, end_iso, date_label = _get_date_bounds_mmt("yesterday")
    elif "ဒီနေ့" in user_query.lower() or "today" in user_query.lower() or "ယနေ့" in user_query.lower():
        start_iso, end_iso, date_label = _get_date_bounds_mmt("today")

    cutoff_iso = (_now_mmt() - timedelta(days=30)).isoformat()
    query = supabase.table("raw_logs").select("*").gte("created_at", cutoff_iso).order("id", desc=True)
    if not start_iso and not end_iso and not exact_group_name:
        query = query.eq("is_read", False)

    logs = list(reversed(query.limit(500).execute().data or []))
    if start_iso and end_iso:
        logs = _filter_logs_by_mmt_range(logs, start_iso, end_iso)

    matched_group = exact_group_name
    if exact_group_name:
        logs = [l for l in logs if l.get("group_name") == exact_group_name]
    elif logs:
        all_groups = list({l.get("group_name") for l in logs if l.get("group_name")})
        matched_group = resolve_group_name_from_text(user_query, all_groups)
        if matched_group:
            logs = [l for l in logs if l.get("group_name") == matched_group]

    if not logs:
        scope = f" [{matched_group}]" if matched_group else ""
        return f"{date_label or 'လတ်တလော'}{scope} အတွက် မှတ်တမ်း မရှိသေးပါဘူးရှင်"

    raw_text = "\n".join([
        f"[{l.get('group_name', '')} | {l.get('sender_name', 'Member')} | {_format_mm_timestamp(l.get('created_at'))}]: {l.get('content_summary', '')}"
        for l in logs[-220:]
    ])
    header_info = f"Group: {matched_group or 'All Groups'} | Period: {date_label or 'Unread'}"

    prompt = f"""
အောက်ပါတို့မှာ Telegram Group များမှ ရရှိထားသော မက်ဆေ့ခ်ျများနှင့် ပုံ/ဖိုင် အနှစ်ချုပ်များ ဖြစ်သည်။
({header_info})
မြန်မာမိန်းကလေးတစ်ယောက်လို သဘာဝကျကျ ရင်းနှီးဖော်ရွေစွာ ပြောပါ ('မောင်' ဟု မသုံးပါနှင့်)။
- ရိုးရိုးနှုတ်ဆက်စကားများကို ဖယ်ထုတ်ပါ။
- English စာသား/အချက်အလက်များကို မူရင်း English အတိုင်း ထား၍ report ပြန်ပေးပါ။
- Group တစ်ခုထက်ပိုလျှင် Group နာမည်တစ်ခုချင်းစီကို ခေါင်းစဉ်ခွဲ၍ စုစည်းရေးပါ။
- ** (bold asterisks) လုံးဝ မသုံးပါနှင့်။
1. ပုံမှန်မဟုတ်သော ပြဿနာများ (Issues / Blockers)
2. ဆုံးဖြတ်ချက် အသစ်များနှင့် ညွှန်ကြားချက်များ
3. အရေးကြီးသော လုပ်ငန်းတာဝန်များ (Action Items)

ဒေတာများ:
{raw_text[:14000]}
"""
    report_text = call_ai_with_3tier_fallback(prompt, use_google_search=False)
    try:
        supabase.table("daily_reports").insert({"report_content": f"[{header_info}]\n{report_text}"}).execute()
    except Exception:
        pass
    if not date_label and not exact_group_name:
        for l in logs:
            try:
                supabase.table("raw_logs").update({"is_read": True}).eq("id", l["id"]).execute()
            except Exception:
                pass
    return f"{header_info}\n\n{report_text}"


def generate_all_groups_summary(period: str = "today") -> str:
    """Today / Yesterday အလိုက် Group မှတ်တမ်းများကို စုစည်း၍ အနှစ်ချုပ် ထုတ်ပေးသည်။"""
    return get_group_report(f"{period} summary", exact_group_name=None, day_mode=period)


def handle_group_query(user_message: str, route: dict) -> str:
    """Group summary / sender / factual query ကို raw_logs မှ ဖြေကြားသည်။"""
    available_groups = get_available_group_names()
    group_name = route.get("group_name") or resolve_group_name_from_text(user_message, available_groups)
    period = route.get("period") or "recent"
    mode = route.get("group_mode") or "query"

    if mode == "summary" or any(w in user_message.lower() for w in ["summary", "အနှစ်ချုပ်", "ခြုံငုံ", "report"]):
        day_m = period if period in ["today", "yesterday"] else None
        return get_group_report(user_message, exact_group_name=group_name, day_mode=day_m)

    rows = fetch_group_messages(group_name=group_name, query_text=user_message, limit=150, period=period)
    if not rows:
        return f"\"{group_name or 'သက်ဆိုင်ရာ Group'}\" ထဲမှာ မှတ်တမ်း မရှိသေးပါဘူးရှင်"

    lines = [
        f"[{_format_mm_timestamp(r['created_at'])} | Group: {r['chat_title']} | {r['sender_name']}]: {r['message_text']}"
        for r in reversed(rows)
    ]
    logs_block = "\n".join(lines)
    now_mm = _now_mmt()
    now_str = now_mm.strftime(f"%Y-%m-%d ({MYANMAR_WEEKDAYS[now_mm.weekday()]}) %H:%M MMT (+06:30)")

    prompt = f"""
လက်ရှိအချိန်: {now_str}
အောက်ပါတို့မှာ Telegram Group ထဲမှ စကားပြောဆိုမှု မှတ်တမ်းများ ဖြစ်သည်။
အသုံးပြုသူ မေးထားသည့် အချက် (သို့မဟုတ် သက်ဆိုင်ရာ လူပြောထားသည့် အကြောင်းအရာ) တစ်ခုတည်းကိုသာ Focus ထား၍ ပြန်ပြောပြပေးပါ။
1. မြန်မာမိန်းကလေးတစ်ယောက်လို သဘာဝကျကျ ရင်းနှီးဖော်ရွေစွာ ပြောပါ ('မောင်' ဟု မသုံးပါနှင့်)။
2. အချက်အလက်တိုင်း၏ အနောက်တွင် [Verified]၊ [Deduced] သို့မဟုတ် [Unverified] တွဲထည့်ပေးပါ။
3. မေးထားသည့် အချက်/လူနှင့် မသက်ဆိုင်သော အခြားလူများ၏ စကားများကို လုံးဝ ထည့်မပြောပါနှင့်။

လက်ရှိ မေးခွန်း: {user_message}
Group မှတ်တမ်းများ:
{logs_block[:14000]}
"""
    return call_ai_with_3tier_fallback(prompt, use_google_search=False)


# --- Trusted News Fetchers (Telegram Channels + RSS + Domain Web Search + Tavily) ---
def _clean_html_text(raw_html: str) -> str:
    if not raw_html:
        return ""
    txt = html.unescape(raw_html)
    txt = re.sub(r"<[^>]+>", " ", txt)
    txt = re.sub(r"\s+", " ", txt).strip()
    return txt


def _fetch_telegram_channel_posts(username: str, source_label: str, limit: int = 5) -> list[str]:
    """Public Telegram channel web preview (t.me/s/<username>) မှ နောက်ဆုံးပို့စ်များ ဖတ်ယူသည်။"""
    url = f"https://t.me/s/{username}"
    headers = {"User-Agent": "Mozilla/5.0"}
    items = []
    try:
        resp = requests.get(url, headers=headers, timeout=8)
        if resp.status_code != 200:
            return items
        soup = BeautifulSoup(resp.text, "html.parser")
        msg_divs = soup.select(".tgme_widget_message_text")
        for div in reversed(msg_divs):
            txt = _clean_html_text(str(div))
            if len(txt) >= 25:
                items.append(f"[{source_label} - TG] {txt[:260]}")
            if len(items) >= limit:
                break
    except Exception as e:
        print(f"TG channel fetch error ({username}): {e}")
    return items


def _fetch_rss_headlines(feed_name: str, feed_url: str, limit: int = 5) -> list[str]:
    """RSS / Atom feed မှ သတင်းခေါင်းစဉ်နှင့် အကျဉ်းများ ဖတ်ယူသည်။"""
    headers = {"User-Agent": "Mozilla/5.0"}
    items = []
    try:
        resp = requests.get(feed_url, headers=headers, timeout=8)
        if resp.status_code != 200 or not resp.content:
            return items
        root = ET.fromstring(resp.content)
        entries = root.findall(".//item") or root.findall(".//{http://www.w3.org/2005/Atom}entry")
        for entry in entries[:limit]:
            title_el = entry.find("title")
            if title_el is None:
                title_el = entry.find("{http://www.w3.org/2005/Atom}title")
            desc_el = entry.find("description")
            if desc_el is None:
                desc_el = entry.find("summary")
            if desc_el is None:
                desc_el = entry.find("{http://www.w3.org/2005/Atom}summary")

            title_txt = _clean_html_text(title_el.text if title_el is not None and title_el.text else "")
            desc_txt = _clean_html_text(desc_el.text if desc_el is not None and desc_el.text else "")
            if title_txt:
                line = f"[{feed_name}] {title_txt}"
                if desc_txt and desc_txt != title_txt:
                    line += f" — {desc_txt[:150]}"
                items.append(line[:280])
    except Exception as e:
        print(f"RSS fetch error ({feed_name}): {e}")
    return items


def _search_tavily_domains(query: str, domains: list[str], max_results: int = 5) -> list[str]:
    """Trusted domain list ဖြင့် Tavily search လုပ်သည်။"""
    if not tavily_client:
        return []
    out = []
    try:
        res = tavily_client.search(
            query=query,
            include_domains=domains,
            max_results=max_results,
            search_depth="basic",
        )
        for r in (res.get("results") or []):
            title = (r.get("title") or "").strip()
            content = re.sub(r"\s+", " ", (r.get("content") or "").strip())
            url = (r.get("url") or "").strip()
            if title or content:
                out.append(f"[Web] {title}: {content[:180]} ({url})")
    except Exception as e:
        print(f"Tavily domain search error: {e}")
    return out


def _collect_trusted_news_bundle(scope: str = "all", topic: str | None = None) -> str:
    """ပြည်တွင်း / နိုင်ငံတကာ သတင်းအရင်းအမြစ်များ (TG Channels + RSS + Trusted Domains) စုစည်းသည်။"""
    sections = []
    q_local = topic or "Myanmar news today"
    q_intl = topic or "world international news today"

    if scope in ("all", "local"):
        local_items = []
        for label, ch in TELEGRAM_NEWS_CHANNELS:
            local_items.extend(_fetch_telegram_channel_posts(ch, label, limit=4))
        for fname, furl in LOCAL_RSS_FEEDS:
            local_items.extend(_fetch_rss_headlines(fname, furl, limit=4))
        local_items.extend(_search_tavily_domains(q_local, LOCAL_TRUSTED_DOMAINS, max_results=5))
        if local_items:
            sections.append("=== ပြည်တွင်း သတင်းရင်းမြစ်များ ===\n" + "\n".join(local_items[:22]))

    if scope in ("all", "intl"):
        intl_items = []
        for fname, furl in INTL_RSS_FEEDS:
            intl_items.extend(_fetch_rss_headlines(fname, furl, limit=4))
        intl_items.extend(_search_tavily_domains(q_intl, INTL_TRUSTED_DOMAINS, max_results=5))
        if intl_items:
            sections.append("=== နိုင်ငံတကာ သတင်းရင်းမြစ်များ ===\n" + "\n".join(intl_items[:18]))

    return "\n\n".join(sections)


def handle_news_command(user_text: str) -> str:
    """/news command ကို ယုံကြည်စိတ်ချရသော သတင်းရင်းမြစ်များမှ စုစည်း၍ MMT အချိန်ဖြင့် အနှစ်ချုပ်ပေးသည်။"""
    lower = (user_text or "").lower()
    if any(k in lower for k in ["local", "ပြည်တွင်း", "မြန်မာ"]):
        scope = "local"
    elif any(k in lower for k in ["intl", "world", "international", "နိုင်ငံတကာ", "ကမ္ဘာ"]):
        scope = "intl"
    else:
        scope = "all"

    parts = (user_text or "").strip().split(maxsplit=1)
    topic = parts[1].strip() if len(parts) > 1 else None
    if topic and topic.lower() in {"local", "intl", "world", "international", "ပြည်တွင်း", "နိုင်ငံတကာ"}:
        topic = None

    cache_key = f"news:{scope}:{(topic or '').lower()}"
    cached = _get_cached_news(cache_key)
    if cached:
        return cached

    bundle = _collect_trusted_news_bundle(scope=scope, topic=topic)
    if not bundle:
        return "လောလောဆယ် သတင်းရင်းမြစ်များမှ သတင်းများ ရယူ၍ မရသေးပါဘူးရှင်၊ ခဏနေ ထပ်စမ်းကြည့်ပေးပါ"

    now_mm = _now_mmt()
    now_str = now_mm.strftime(f"%Y-%m-%d ({MYANMAR_WEEKDAYS[now_mm.weekday()]}) %H:%M MMT (+06:30)")
    prompt = f"""
သင်သည် 'သင်ဇာ (Thinzar)' အမည်ရှိ မြန်မာမိန်းကလေးဟန် ကိုယ်ပိုင် AI Assistant ဖြစ်သည်။
အချိန်: {now_str}
အောက်ပါ ယုံကြည်စိတ်ချရသော ပြည်တွင်း/နိုင်ငံတကာ သတင်းရင်းမြစ်များမှ ကောက်နုတ်ချက်များကို အခြေခံ၍
သတင်းအနှစ်ချုပ်ကို မြန်မာဘာသာဖြင့် ရှင်းရှင်းလင်းလင်း စုစည်းတင်ပြပါ။

စည်းမျဉ်းများ:
1. ဖော်ပြထားသော သတင်းရင်းမြစ်များထဲမှ အချက်အလက်များကိုသာ အခြေခံပါ။
2. အချက်တစ်ခုစီ၏ ရှေ့တွင် [Verified] (ရင်းမြစ်တွင် တိုက်ရိုက်ပါဝင်သော သတင်း) သို့မဟုတ် [Deduced] (သတင်းများကို ဆက်စပ်သုံးသပ်ချက်) သို့မဟုတ် [Unverified] (အတည်ပြုရန် လိုသေးသည့်အချက်) ထည့်ပါ။
3. ပြည်တွင်းသတင်း နှင့် နိုင်ငံတကာသတင်း ခေါင်းစဉ်ခွဲများဖြင့် သပ်သပ်ရပ်ရပ် ရေးပါ (သက်ဆိုင်ရာ scope အလိုက်)။
4. 'မောင်' ဟု လုံးဝ မသုံးပါနှင့်။ သဘာဝကျသော သင်ဇာဟန်ဖြင့် ရေးပါ။

News Raw Bundle:
{bundle[:14000]}
"""
    result = call_ai_with_3tier_fallback(prompt, use_google_search=True)
    if result and "ခဏအလုပ်များနေပါတယ်" not in result:
        _set_cached_news(cache_key, result)
    return result


# --- Hybrid Search (Tavily + Google Search Grounding) ---
def _is_live_or_time_sensitive_query(query: str) -> bool:
    q = (query or "").lower()
    cur_year = str(_now_mmt().year)
    keywords = [
        "news", "သတင်း", "score", "match", "schedule", "fixture", "result", "ပွဲ", "ရလဒ်", "အချိန်ဇယား",
        "price", "rate", "exchange", "gold", "weather", "stock", "crypto", "ဈေး", "ငွေလဲ", "ရာသီဥတု",
        "today", "tonight", "tomorrow", "latest", "current", "live", cur_year, "ယနေ့", "ဒီနေ့", "မနက်ဖြန်",
        "လက်ရှိ", "နောက်ဆုံး", "ဘယ်တော့", "ဘယ်နေ့", "ဘယ်အချိန်"
    ]
    return any(k in q for k in keywords)


def _tavily_search_summary(query: str, max_results: int = 5) -> str:
    if not tavily_client or not query:
        return ""
    try:
        res = tavily_client.search(
            query=query,
            max_results=max_results,
            search_depth="advanced" if _is_live_or_time_sensitive_query(query) else "basic",
        )
        lines = []
        for idx, r in enumerate((res.get("results") or [])[:max_results], start=1):
            title = (r.get("title") or "").strip()
            snippet = re.sub(r"\s+", " ", (r.get("content") or "").strip())
            url = (r.get("url") or "").strip()
            lines.append(f"{idx}. {title} — {snippet[:260]} (Source: {url})")
        return "\n".join(lines)
    except Exception as e:
        print(f"Tavily search error: {e}")
        return ""


def perform_hybrid_web_search(user_query: str, english_query: str | None = None, history_ctx: str = "") -> str:
    """Tavily Web Search + Google Search Grounding ကို MMT (Asia/Yangon) Date/Time ဖြင့် ပေါင်းစပ်၍ အဖြေထုတ်သည်။"""
    now_mm = _now_mmt()
    now_str = now_mm.strftime(f"%Y-%m-%d ({MYANMAR_WEEKDAYS[now_mm.weekday()]}) %H:%M MMT (+06:30)")

    tavily_blocks = []
    q1 = _tavily_search_summary(user_query, max_results=5)
    if q1:
        tavily_blocks.append(f"[Query 1: {user_query}]\n{q1}")
    if english_query and english_query.strip().lower() != user_query.strip().lower():
        q2 = _tavily_search_summary(english_query.strip(), max_results=5)
        if q2:
            tavily_blocks.append(f"[Query 2: {english_query.strip()}]\n{q2}")

    tavily_text = "\n\n".join(tavily_blocks)

    prompt = f"""
သင်သည် 'သင်ဇာ (Thinzar)' အမည်ရှိ မြန်မာမိန်းကလေးဟန် ကိုယ်ပိုင် AI Assistant ဖြစ်သည်။
လက်ရှိ အချိန်: {now_str}

Recent Context:
{history_ctx[-1200:] if history_ctx else "None"}

User Question: {user_query}
English Search Helper Query: {english_query or "N/A"}

Tavily Search Results:
{tavily_text or "No Tavily results returned."}

ညွှန်ကြားချက်များ:
1. Google Search grounding နှင့် အထက်ပါ Tavily search results များကို ပေါင်းစပ်၍ အမှန်ကန်ဆုံး၊ နောက်ဆုံးအခြေအနေနှင့် အကိုက်ညီဆုံး အဖြေကို မြန်မာဘာသာဖြင့် ပြန်ဖြေပါ။
2. ရက်စွဲ၊ အချိန်၊ ရလဒ်၊ ဈေးနှုန်း သို့မဟုတ် အချက်အလက်များ ဖော်ပြသည့်အခါ [Verified] (ရှာဖွေတွေ့ရှိချက်တွင် တိကျစွာပါဝင်)၊ [Deduced] (ဆက်စပ်တွက်ချက်/သုံးသပ်ချက်)၊ [Unverified] (အတည်မပြုနိုင်သေး/ပြောင်းလဲနိုင်သေး) tag များကို သင့်လျော်သလို ထည့်သွင်းပါ။
3. အချက်အလက် မသေချာပါက မသေချာကြောင်း ပွင့်ပွင့်လင်းလင်း ပြောပါ (မခန့်မှန်းလုပ်ကြံပါနှင့်)။
4. 'မောင်' ဟု လုံးဝ မသုံးပါနှင့်။ သဘာဝကျသော သင်ဇာဟန်ဖြင့် ဖြေပါ။
"""
    return call_ai_with_3tier_fallback(prompt, use_google_search=True)
