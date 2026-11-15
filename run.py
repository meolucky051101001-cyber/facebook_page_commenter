import os
import sys
import webbrowser
import threading
import time
from urllib.parse import urlsplit
import uvicorn
import config

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def _pick_port(default_port: int = 8080) -> int:
    env_port = os.getenv("COMMENTER_PORT")
    if env_port and env_port.isdigit():
        return int(env_port)
    return default_port


def _open_browser_when_ready(port: int):
    time.sleep(1.2)
    try:
        callback = urlsplit(config.META_OAUTH_REDIRECT_URI)
        host = "localhost" if callback.hostname == "localhost" and callback.port == port else "127.0.0.1"
        webbrowser.open(f"http://{host}:{port}")
    except Exception:
        pass


if __name__ == "__main__":
    is_dev = os.getenv("ENV", "production").lower() == "development"
    port = _pick_port()
    print("=" * 65)
    print("  Facebook Page Multi-Account Commenter Hub v2.0 (Antidetect Hybrid)")
    print(f"  Trang quản trị (Dashboard): http://127.0.0.1:{port}")
    print(f"  Chế độ: {'DEVELOPMENT (Reload)' if is_dev else 'PRODUCTION (Single-Process 24/7)'}")
    print("=" * 65)
    threading.Thread(target=_open_browser_when_ready, args=(port,), daemon=True).start()
    uvicorn.run("app:app", host="127.0.0.1", port=port, reload=is_dev, workers=1, access_log=False)


