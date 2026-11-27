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
    """Group message များကို ရက် ၃၀ (၁ လ) ကျော်လျှင် ဖျက်၍ database size ထိန်းညှိသည်။"""
    try:
        cutoff_dt = datetime.now(MM_TZ) - timedelta(days=30)
        res = supabase.table("group_messages").select("id, created_at").execute()
        for row in (res.data or []):
            ts_raw = row.get("created_at")
            if not ts_raw:
                continue
            try:
                dt_obj = datetime.fromisoformat(str(ts_raw).replace("Z", "+00:00"))
                dt_mm = dt_obj.replace(tzinfo=MM_TZ) if dt_obj.tzinfo is None else dt_obj.astimezone(MM_TZ)
                if dt_mm < cutoff_dt:
                    supabase.table("group_messages").delete().eq("id", row["id"]).execute()
            except Exception:
                pass
    except Exception as e:
        print(f"Error cleaning up old group messages: {e}")


def save_group_message(chat_id: int, chat_title: str, sender_name: str, message_text: str):
    """Group မှ message များကို group_messages table ထဲ သိမ်းဆည်းသည်။"""
    try:
        clean_title = (chat_title or f"Group {chat_id}").strip()
        clean_sender = (sender_name or "Member").strip()
        clean_text = (message_text or "").strip()
        if not clean_text:
            return
        try:
            supabase.table("group_messages").insert({
                "chat_id": chat_id,
                "chat_title": clean_title,
                "sender_name": clean_sender,
                "message_text": clean_text
            }).execute()
        except Exception as e1:
            print(f"Error saving group message (full): {e1}")
            supabase.table("group_messages").insert({
                "chat_id": chat_id,
                "sender_name": clean_sender,
                "message_text": f"[{clean_title}] {clean_text}"
            }).execute()
    except Exception as e:
        print(f"Error saving group message: {e}")


def get_available_group_names(limit: int = 200) -> list:
    """Group မှတ်တမ်းထဲတွင် ရှိနေသော group name များကို စုစည်းပေးသည်။"""
    names = []
    seen = set()
    try:
        res = (
            supabase.table("group_messages")
            .select("chat_title, message_text")
            .order("id", desc=True)
            .limit(limit)
            .execute()
        )
        for r in (res.data or []):
            title = (r.get("chat_title") or "").strip()
            txt = r.get("message_text") or ""
            if not title and txt.startswith("[") and "]" in txt:
                title = txt[1:txt.index("]")].strip()
            if title and title not in seen:
                seen.add(title)
                names.append(title)
    except Exception as e:
        print(f"Error fetching group names: {e}")
    return names


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


def fetch_group_messages(
    group_name: str | None = None,
    query_text: str = "",
    limit: int = 80,
    period: str = "recent",
) -> list:
    """Group မှတ်တမ်းများကို group_name / period / query_text ဖြင့် ရှာဖွေသည်။"""
    try:
        res = (
            supabase.table("group_messages")
            .select("*")
            .order("id", desc=True)
            .limit(350)
            .execute()
        )
        rows = res.data or []
    except Exception as e:
        print(f"Error reading group_messages: {e}")
        return []

    normalized = []
    for r in rows:
        title = (r.get("chat_title") or "").strip()
        txt = r.get("message_text") or ""
        if not title and txt.startswith("[") and "]" in txt:
            title = txt[1:txt.index("]")].strip()
            txt = txt[txt.index("]") + 1:].strip()
        normalized.append({
            "chat_title": title or "Group",
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

    now_mm = datetime.now(MM_TZ)
    if period == "today":
        today_str = now_mm.strftime("%Y-%m-%d")
        today_rows = [
            item for item in normalized
            if _format_mm_timestamp(item["created_at"]).startswith(today_str)
        ]
        if today_rows:
            normalized = today_rows
    elif period == "yesterday":
        y_str = (now_mm - timedelta(days=1)).strftime("%Y-%m-%d")
        y_rows = [
            item for item in normalized
            if _format_mm_timestamp(item["created_at"]).startswith(y_str)
        ]
        if y_rows:
            normalized = y_rows

    terms = extract_search_terms(query_text)
    if terms and period not in ("today", "yesterday"):
        scored = []
        for item in normalized:
            hay = f"{item['chat_title']} {item['sender_name']} {item['message_text']}".lower()
            hits = sum(1 for t in terms if t.lower() in hay)
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


def handle_group_query(user_message: str, route: dict) -> str:
    """Group summary / group factual query ကို DB မှတ်တမ်းဖြင့် ဖြေကြားသည်။"""
    available_groups = get_available_group_names()
    group_name = route.get("group_name") or resolve_group_name_from_text(user_message, available_groups)
    period = route.get("period") or "recent"
    mode = route.get("group_mode") or "query"

    if any(w in user_message for w in ["summary", "အနှစ်ချုပ်", "ခြုံငုံ", "report", "ဘာတွေဆွေးနွေး", "ဘာတွေပြော"]):
        mode = "summary"

    rows = fetch_group_messages(group_name=group_name, query_text=user_message, limit=90, period=period)
    if not rows:
        if group_name:
            return f"\"{group_name}\" Group အတွက် သိမ်းထားတဲ့ မှတ်တမ်း မတွေ့သေးပါဘူးရှင်"
        return "Group မှတ်တမ်းထဲမှာ သက်ဆိုင်တဲ့ အချက်အလက် မတွေ့သေးပါဘူးရှင်"

    lines = [
        f"[{_format_mm_timestamp(r['created_at'])} | Group: {r['chat_title']} | {r['sender_name']}]: {r['message_text']}"
        for r in reversed(rows)
    ]
    logs_block = "\n".join(lines)

    prompt = f"""
သင်သည် 'သင်ဇာ (Thinzar)' အမည်ရှိ မြန်မာမိန်းကလေးဟန် ကိုယ်ပိုင် AI Assistant ဖြစ်သည်။
အောက်ပါ Telegram Group မှတ်တမ်းများကို အခြေခံ၍ အသုံးပြုသူ၏ မေးခွန်း/တောင်းဆိုချက်ကို မြန်မာဘာသာဖြင့် တိကျရှင်းလင်းစွာ ဖြေဆိုပါ။

စည်းမျဉ်းများ:
1. Group မှတ်တမ်းထဲတွင် ပါဝင်သော အချက်အလက်များကိုသာ အခြေခံပါ (မပါဝင်သည်ကို မလုပ်ကြံပါနှင့်)။
2. အချက်အလက်တစ်ခုစီ၏ ရှေ့တွင် သင့်လျော်သော tag ထည့်ပါ — Group log ထဲ တိုက်ရိုက်ပါလျှင် [Verified]၊ log ပါ အချက်များကို ဆက်စပ်သုံးသပ်လျှင် [Deduced]၊ မသေချာ/မပြည့်စုံလျှင် [Unverified] ဟု ဖော်ပြပါ။
3. Mode က "summary" ဖြစ်လျှင် အဓိက ဆွေးနွေးချက်/သတင်း/လုပ်ဆောင်ချက်များကို ကဏ္ဍအလိုက် သပ်သပ်ရပ်ရပ် အနှစ်ချုပ်ပေးပါ။
4. 'မောင်' ဟု လုံးဝ မသုံးပါနှင့်။ သဘာဝကျသော သင်ဇာဟန်ဖြင့် ဖြေပါ။

Target Group Filter: {group_name or "All Groups"}
Mode: {mode}
User Question: {user_message}

Group Logs:
{logs_block[:14000]}
"""
    return call_ai_with_3tier_fallback(prompt)


def generate_all_groups_summary(period: str = "today") -> str:
    """Today / Yesterday အလိုက် Group မှတ်တမ်းများကို စုစည်း၍ အနှစ်ချုပ် ထုတ်ပေးသည်။"""
    norm_period = "yesterday" if (period or "").lower() == "yesterday" else "today"
    label_mm = "ယနေ့ (Today)" if norm_period == "today" else "မနေ့က (Yesterday)"
    rows = fetch_group_messages(group_name=None, query_text="", limit=150, period=norm_period)
    if not rows:
        return f"📌 {label_mm} အတွက် Group မှတ်တမ်း မတွေ့သေးပါဘူးရှင်။"

    lines = [
        f"[{_format_mm_timestamp(r['created_at'])} | {r['chat_title']} | {r['sender_name']}]: {r['message_text']}"
        for r in reversed(rows)
    ]
    logs_block = "\n".join(lines)[:14000]

    prompt = f"""
သင်သည် 'သင်ဇာ (Thinzar)' အမည်ရှိ မြန်မာမိန်းကလေးဟန် ကိုယ်ပိုင် AI Assistant ဖြစ်သည်။
အောက်ပါ {label_mm} Telegram Group မှတ်တမ်းများကို အခြေခံ၍ Group အလိုက် အဓိက ဆွေးနွေးချက်/သတင်း/လုပ်ဆောင်ချက်များကို မြန်မာဘာသာဖြင့် အနှစ်ချုပ် ရေးပေးပါ။

စည်းမျဉ်းများ:
1. မှတ်တမ်းထဲ ပါဝင်သော အချက်အလက်များကိုသာ အခြေခံပါ။
2. Group ခေါင်းစဉ်အလိုက် သပ်သပ်ရပ်ရပ် ခွဲခြားဖော်ပြပါ။
3. အချက်တစ်ခုစီ၏ ရှေ့တွင် [Verified]၊ [Deduced]၊ [Unverified] သင့်လျော်သော tag ထည့်ပါ။
4. 'မောင်' ဟု လုံးဝ မသုံးပါနှင့်။

Group Logs ({label_mm}):
{logs_block}
"""
    return call_ai_with_3tier_fallback(prompt)


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
    """/news command ကို ယုံကြည်စိတ်ချရသော သတင်းရင်းမြစ်များမှ စုစည်း၍ မြန်မာလို အနှစ်ချုပ်ပေးသည်။"""
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

    now_str = datetime.now(MM_TZ).strftime("%Y-%m-%d %H:%M MMT")
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
    keywords = [
        "news", "သတင်း", "score", "match", "schedule", "fixture", "result", "ပွဲ", "ရလဒ်", "အချိန်ဇယား",
        "price", "rate", "exchange", "gold", "weather", "stock", "crypto", "ဈေး", "ငွေလဲ", "ရာသီဥတု",
        "today", "tonight", "tomorrow", "latest", "current", "live", "2026", "ယနေ့", "ဒီနေ့", "မနက်ဖြန်",
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
    """Tavily Web Search + Google Search Grounding ကို ပေါင်းစပ်၍ အဖြေထုတ်သည်။"""
    now_mm = datetime.now(MM_TZ)
    now_str = now_mm.strftime(f"%Y-%m-%d ({MYANMAR_WEEKDAYS[now_mm.weekday()]}) %H:%M MMT")

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