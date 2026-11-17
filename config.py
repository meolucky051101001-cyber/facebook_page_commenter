import os
from pathlib import Path
from dotenv import load_dotenv
from cryptography.fernet import Fernet

BASE_DIR = Path(__file__).resolve().parent
ENV_FILE = BASE_DIR / ".env"

# Tests disable dotenv before importing any application module. Honor that flag
# here too, including bootstrap writes (older python-dotenv versions ignore it).
DOTENV_DISABLED = os.getenv("PYTHON_DOTENV_DISABLED", "").lower() in ("1", "true", "yes", "on")
if not DOTENV_DISABLED:
    if not ENV_FILE.exists():
        EXAMPLE_FILE = BASE_DIR / ".env.example"
        if EXAMPLE_FILE.exists():
            ENV_FILE.write_text(EXAMPLE_FILE.read_text(encoding="utf-8"), encoding="utf-8")
        else:
            ENV_FILE.touch()
    load_dotenv(ENV_FILE)

# 1. Meta Credentials
META_APP_ID = os.getenv("META_APP_ID", "").strip()
META_APP_SECRET = os.getenv("META_APP_SECRET", "").strip()
META_OAUTH_REDIRECT_URI = os.getenv("META_OAUTH_REDIRECT_URI", "").strip()
GRAPH_API_VERSION = os.getenv("GRAPH_API_VERSION", "v26.0").strip()


def has_app_credentials() -> bool:
    """Returns True if both META_APP_ID and META_APP_SECRET are configured."""
    return bool(META_APP_ID and META_APP_SECRET)


# 2. Encryption Key for Tokens
ENCRYPTION_KEY = os.getenv("ENCRYPTION_KEY", "").strip()
if not ENCRYPTION_KEY:
    if DOTENV_DISABLED:
        raise RuntimeError("Thiếu khóa mã hóa bền vững; đã dừng khởi động.")
    # Bootstrap only when the generated key has been persisted successfully.
    new_key = Fernet.generate_key().decode("utf-8")
    try:
        with open(ENV_FILE, "a", encoding="utf-8") as f:
            f.write(f"\nENCRYPTION_KEY={new_key}\n")
            f.flush()
            os.fsync(f.fileno())
    except OSError:
        raise RuntimeError("Không lưu được khóa mã hóa; đã dừng khởi động.") from None
    ENCRYPTION_KEY = new_key
try:
    Fernet(ENCRYPTION_KEY.encode("utf-8"))
except (TypeError, ValueError):
    raise RuntimeError("Khóa mã hóa không hợp lệ; đã dừng khởi động.") from None

# 3. Intervals & Performance Settings
POST_SCAN_INTERVAL_SEC = int(os.getenv("POST_SCAN_INTERVAL_SEC", "240"))
TASK_TICK_SEC = int(os.getenv("TASK_TICK_SEC", "20"))
SHEET_SYNC_INTERVAL_SEC = int(os.getenv("SHEET_SYNC_INTERVAL_SEC", "300"))

# 4. Anti-spam / Safeguard Limits
MAX_POST_AGE_MIN = int(os.getenv("MAX_POST_AGE_MIN", "120"))
MIN_GAP_PER_PAGE_SEC = int(os.getenv("MIN_GAP_PER_PAGE_SEC", "45"))
MAX_COMMENTS_PER_PAGE_PER_DAY = int(os.getenv("MAX_COMMENTS_PER_PAGE_PER_DAY", "20"))
SEND_BACKOFF_MAX_MINUTES = max(16, int(os.getenv("SEND_BACKOFF_MAX_MINUTES", "30")))
# Daily comment counter follows this UTC offset (default 7 = giờ Việt Nam)
DAILY_COUNTER_UTC_OFFSET_HOURS = int(os.getenv("DAILY_COUNTER_UTC_OFFSET_HOURS", "7"))

# 5. Antidetect Python Integration
ANTIDETECT_DIR = os.getenv("ANTIDETECT_DIR", r"C:\Projects\Tools\Antidetect Python").strip()
ANTIDETECT_AUTO_CLOSE_BROWSER = os.getenv("ANTIDETECT_AUTO_CLOSE_BROWSER", "1").strip() in ("1", "true", "yes", "on")
ANTIDETECT_AUTO_SCAN_POSTS = os.getenv("ANTIDETECT_AUTO_SCAN_POSTS", "0").strip() in ("1", "true", "yes", "on")
BROWSER_MAX_OPERATIONS_PER_PROFILE_PER_DAY = max(1, int(os.getenv("BROWSER_MAX_OPERATIONS_PER_PROFILE_PER_DAY", "100")))
BROWSER_MIN_GAP_PER_PROFILE_SEC = max(1, int(os.getenv("BROWSER_MIN_GAP_PER_PROFILE_SEC", "60")))

# 6. Default Google Sheet URL & Startup Mode
DEFAULT_SHEET_URL = os.getenv("DEFAULT_SHEET_URL", "").strip()
DASHBOARD_ADMIN_KEY = os.getenv("DASHBOARD_ADMIN_KEY", "").strip()
MULTI_PAGE_SHEET_URL = os.getenv("MULTI_PAGE_SHEET_URL", "").strip()
PILOT_SHEET_URL = os.getenv("PILOT_SHEET_URL", "").strip()
PILOT_PAGE_ID = os.getenv("PILOT_PAGE_ID", "").strip()
PILOT_AUTO_SCAN = os.getenv("PILOT_AUTO_SCAN", "0").strip() == "1"
PAUSED_BY_DEFAULT = os.getenv("PAUSED_BY_DEFAULT", "1").strip() in ("1", "true", "yes", "on")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "").strip()




