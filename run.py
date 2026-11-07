import os
import socket
import uvicorn


def _pick_port(default_port: int = 8000, fallback_port: int = 8080) -> int:
    env_port = os.getenv("COMMENTER_PORT")
    if env_port and env_port.isdigit():
        return int(env_port)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        if s.connect_ex(("127.0.0.1", default_port)) != 0:
            return default_port
    return fallback_port


if __name__ == "__main__":
    is_dev = os.getenv("ENV", "production").lower() == "development"
    port = _pick_port(8000, 8080)
    print("=" * 65)
    print("  Facebook Page Multi-Account Commenter Hub v2.0 (Antidetect Hybrid)")
    print(f"  Trang quản trị (Dashboard): http://127.0.0.1:{port}")
    print(f"  Chế độ: {'DEVELOPMENT (Reload)' if is_dev else 'PRODUCTION (Single-Process 24/7)'}")
    print("=" * 65)
    uvicorn.run("app:app", host="127.0.0.1", port=port, reload=is_dev)

