import os
from pathlib import Path
from dotenv import load_dotenv
from cryptography.fernet import Fernet

BASE_DIR = Path(__file__).resolve().parent
ENV_FILE = BASE_DIR / ".env"

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
GRAPH_API_VERSION = os.getenv("GRAPH_API_VERSION", "v19.0").strip()


def has_app_credentials() -> bool:
    """Returns True if both META_APP_ID and META_APP_SECRET are configured."""
    return bool(META_APP_ID and META_APP_SECRET)


# 2. Encryption Key for Tokens
ENCRYPTION_KEY = os.getenv("ENCRYPTION_KEY", "").strip()
if not ENCRYPTION_KEY:
    # Auto-generate a Fernet key and persist to .env
    new_key = Fernet.generate_key().decode("utf-8")
    ENCRYPTION_KEY = new_key
    with open(ENV_FILE, "a", encoding="utf-8") as f:
        f.write(f"\nENCRYPTION_KEY={new_key}\n")

# 3. Intervals & Performance Settings
POST_SCAN_INTERVAL_SEC = int(os.getenv("POST_SCAN_INTERVAL_SEC", "240"))
TASK_TICK_SEC = int(os.getenv("TASK_TICK_SEC", "20"))
SHEET_SYNC_INTERVAL_SEC = int(os.getenv("SHEET_SYNC_INTERVAL_SEC", "300"))

# 4. Anti-spam / Safeguard Limits
MAX_POST_AGE_MIN = int(os.getenv("MAX_POST_AGE_MIN", "120"))
MIN_GAP_PER_PAGE_SEC = int(os.getenv("MIN_GAP_PER_PAGE_SEC", "45"))
MAX_COMMENTS_PER_PAGE_PER_DAY = int(os.getenv("MAX_COMMENTS_PER_PAGE_PER_DAY", "20"))

# 5. Antidetect Python Integration
ANTIDETECT_DIR = os.getenv("ANTIDETECT_DIR", r"C:\Projects\Tools\Antidetect Python").strip()
ANTIDETECT_AUTO_CLOSE_BROWSER = os.getenv("ANTIDETECT_AUTO_CLOSE_BROWSER", "1").strip() in ("1", "true", "yes", "on")
ANTIDETECT_AUTO_SCAN_POSTS = os.getenv("ANTIDETECT_AUTO_SCAN_POSTS", "0").strip() in ("1", "true", "yes", "on")

# 6. Default Google Sheet URL & Startup Mode
DEFAULT_SHEET_URL = os.getenv(
    "DEFAULT_SHEET_URL",
    "https://docs.google.com/spreadsheets/d/1Qq1lkhVzlDe0FJ5k3HDDkBiGYDqTItO2ycQMDQHATD0/edit?pli=1&gid=1405549804#gid=1405549804"
).strip()
PAUSED_BY_DEFAULT = os.getenv("PAUSED_BY_DEFAULT", "1").strip() in ("1", "true", "yes", "on")




