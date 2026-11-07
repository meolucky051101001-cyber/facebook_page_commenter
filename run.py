import os
import uvicorn

if __name__ == "__main__":
    is_dev = os.getenv("ENV", "production").lower() == "development"
    print("=" * 65)
    print("  Facebook Page Multi-Account Commenter Hub v2.0")
    print("  Trang quản trị (Dashboard): http://127.0.0.1:8000")
    print(f"  Chế độ: {'DEVELOPMENT (Reload)' if is_dev else 'PRODUCTION (Single-Process 24/7)'}")
    print("=" * 65)
    uvicorn.run("app:app", host="127.0.0.1", port=8000, reload=is_dev)
