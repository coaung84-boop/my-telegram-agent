import os
from datetime import timezone, timedelta
from dotenv import load_dotenv

load_dotenv()

# AI & Search Clients Initialization
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_API_KEY_2 = os.getenv("GEMINI_API_KEY_2")
GEMINI_API_KEYS = [k.strip() for k in [GEMINI_API_KEY, GEMINI_API_KEY_2] if k and k.strip()]
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ADMIN_USER_ID = int(os.getenv("MY_TELEGRAM_USER_ID", "0"))

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

# Model Configuration (Configurable via Env, with safe defaults)
GEMINI_FAST_MODELS = [
    m.strip()
    for m in os.getenv("GEMINI_FAST_MODELS", "gemini-3.5-flash-lite,gemini-3.8-flash").split(",")
    if m.strip()
]

GEMINI_THINKING_MODELS = [
    m.strip()
    for m in os.getenv("GEMINI_THINKING_MODELS", "gemini-3.8-flash,gemini-3.5-flash-lite").split(",")
    if m.strip()
]

OPENROUTER_FREE_MODELS = [
    m.strip()
    for m in os.getenv(
        "OPENROUTER_FREE_MODELS",
        "openrouter/free,google/gemma-4-26b-a4b-it:free,google/gemma-4-31b-it:free",
    ).split(",")
    if m.strip()
]

GROQ_FALLBACK_MODELS = [
    m.strip()
    for m in os.getenv("GROQ_FALLBACK_MODELS", "llama-3.3-70b-versatile").split(",")
    if m.strip()
]

# Myanmar Standard Time (UTC+6:30), Retention & Quota Savers
MM_TZ = timezone(timedelta(hours=6, minutes=30))
RETENTION_DAYS = 30
NEWS_CACHE_TTL_SECONDS = 1800  # 30 minutes cache for /news & text summary
MAX_GROUP_MEDIA_AI_PER_HOUR = 15

MYANMAR_WEEKDAYS = [
    "တနင်္လာနေ့",
    "အင်္ဂါနေ့",
    "ဗုဒ္ဓဟူးနေ့",
    "ကြာသပတေးနေ့",
    "သောကြာနေ့",
    "စနေနေ့",
    "တနင်္ဂနွေနေ့",
]

# --- Trusted News Sources (Telegram Channels + Website Feeds + Domain Search) ---
TELEGRAM_NEWS_CHANNELS = [
    ("The Irrawaddy", "theirrawaddy"),
    ("Myanmar Now", "MyanmarNowNews"),
    ("Khit Thit Media", "khitthitmedianews"),
]

LOCAL_RSS_FEEDS = [
    ("The Irrawaddy", "https://www.irrawaddy.com/feed"),
    ("Myanmar Now (MM)", "https://myanmar-now.org/mm/feed/"),
    ("Myanmar Now (EN)", "https://myanmar-now.org/en/feed/"),
    ("Mizzima", "https://www.mizzima.com/feed"),
    ("Frontier Myanmar", "https://www.frontiermyanmar.net/en/feed/"),
]

INTL_RSS_FEEDS = [
    ("BBC Burmese", "https://feeds.bbci.co.uk/burmese/rss.xml"),
    ("RFA Burmese", "https://www.rfa.org/burmese/rss2.xml"),
    ("VOA Burmese", "https://burmese.voanews.com/api/epiqq"),
]

LOCAL_TRUSTED_DOMAINS = [
    "irrawaddy.com",
    "myanmar-now.org",
    "mizzima.com",
    "frontiermyanmar.net",
    "khitthitnews.com",
]

INTL_TRUSTED_DOMAINS = [
    "bbc.com",
    "rfa.org",
    "voaburmese.com",
    "reuters.com",
]

DEFAULT_CORE_RULES = (
    "1. 'မောင်' ဟူသော အသုံးအနှုန်းကို ဆေးသောက်ရန် နှင့် မိသားစု/ကလေးများနှင့် ပတ်သက်သော Task/Reminder များတွင်သာ သုံးရမည်။\n"
    "2. အခြား Task များ၊ Group Summary များ၊ News Summary များ၊ Web Search များနှင့် ရိုးရိုးစကားပြောမှုများတွင် မြန်မာမိန်းကလေးတစ်ယောက်ကဲ့သို့ သဘာဝကျကျ ပုံမှန် သင်ဇာ (Thinzar) ဟန်ဖြင့်သာ ပြောရမည်။\n"
    "3. စကားလုံးတစ်ခုခုကို ပုံသေအတင်းမထည့်ဘဲ User Profile နှင့် Style Preferences ထဲမှ လေ့လာထားသည့် သဘာဝကျသော အပြောအဆိုဟန်အတိုင်း ပြောဆိုရမည်။\n"
    "4. Group မှတ်တမ်း၊ သတင်းနှင့် Web Search အချက်အလက်များကို ဖြေဆိုသည့်အခါ [Verified] (တိုက်ရိုက်တွေ့ရှိချက်)၊ "
    "[Deduced] (ဆက်စပ်သုံးသပ်ချက်) သို့မဟုတ် [Unverified] (မသေချာသေးသည့်အချက်) tag များ ထည့်သွင်းဖော်ပြရမည်။"
)

STRICT_MYANMAR_SYSTEM = (
    "သင်သည် 'သင်ဇာ (Thinzar)' အမည်ရှိ မြန်မာမိန်းကလေးဟန် ကိုယ်ပိုင် AI Assistant ဖြစ်သည်။ "
    "မြန်မာဘာသာ (Unicode) နှင့် လိုအပ်သော အင်္ဂလိပ်အသုံးအနှုန်းများဖြင့်သာ လူတစ်ယောက်ကဲ့သို့ သဘာဝကျကျ၊ ရင်းနှီးနွေးထွေးစွာ၊ ရှင်းလင်းပြတ်သားစွာ ဖြေဆိုပါ။ "
    "စကားအဆုံးတွင် စကားလုံးတစ်ခုခုကို ပုံသေအတင်းမထည့်ဘဲ အခြေအနေနှင့် လိုက်ဖက်သည့် သဘာဝကျသော အသုံးအနှုန်းများနှင့် "
    "User Profile / Style Preferences ထဲမှ လေ့လာမှတ်သားထားသည့် စကားပြောဟန်အတိုင်း ပြောဆိုပါ။ "
    "'မောင်' ဟူသော အသုံးအနှုန်းကို ဆေးသောက်ရန် နှင့် မိသားစု/ကလေးများနှင့် ပတ်သက်သော Task/Reminder များတွင်သာ သုံးပါ။ "
    "အခြား Task များ၊ News Summary များ၊ Group Summary များ၊ Web Search များနှင့် အခြားစကားပြောမှုများတွင် 'မောင်' ဟု လုံးဝ မသုံးပါနှင့်။ "
    "မိမိကိုယ်ကို 'ကျွန်တော်' ဟု လုံးဝ မသုံးပါနှင့်။ ထိုင်းစာ/တရုတ်စာ စသည့် မဆိုင်သော ဘာသာစကားစာလုံးများ ရောနှောခြင်းကို လုံးဝ ရှောင်ကြဉ်ပါ။ "
    "အသုံးပြုသူ၏ Locked Core Rules၊ Profile၊ Style Preferences၊ အတိတ်မှတ်တမ်းနှင့် မြန်မာပြက္ခဒိန်အချက်အလက်များကို "
    "သေချာမှတ်မိပြီး အလိုက်သိသိနှင့် ဖြေပါ။"
)
