# HANDOFF — Facebook Page Multi-Account Commenter Hub v2.0 (Hybrid Antidetect)

Tài liệu bàn giao toàn diện dành cho lập trình viên hoặc AI Agent tiếp nhận dự án.

---

## 1. Tóm tắt một câu về trạng thái hiện tại
Codebase hoàn chỉnh ở mức **production-ready**, 17/17 bài kiểm thử tự động trong `test_system.py` đều **PASSED (100%)**, đã kết nối sẵn **59 Fanpage** (8 Hybrid, 1 Graph API, 50 Antidetect CDP), hỗ trợ đồng bộ trực tiếp mẫu comment từ Google Sheet của người dùng (`gid=1405549804`), và đang được cấu hình **mặc định TẠM DỪNG (`paused_all: True`)** để đảm bảo an toàn tuyệt đối trước khi người dùng chủ động bấm chạy.

---

## 2. Thông tin môi trường & Đường dẫn cốt lõi
- **Thư mục mã nguồn:** `C:\Users\admin\.gemini\antigravity\scratch\facebook_page_commenter`
- **Môi trường Python:** Python 3.10.11 (`fastapi`, `uvicorn`, `httpx`, `cryptography`, `python-dateutil`, `websockets`, `pytest`)
- **Phần mềm Antidetect tích hợp:** `C:\Projects\Tools\Antidetect Python` (bảo tồn nguyên trạng, `profiles.json` hash `B26426E4...` không bị thay đổi).
- **Google Sheet cấu hình:** `https://docs.google.com/spreadsheets/d/1Qq1lkhVzlDe0FJ5k3HDDkBiGYDqTItO2ycQMDQHATD0/edit?usp=sharing` (tab `gid=1405549804`).
- **Cơ sở dữ liệu SQLite:** `C:\Users\admin\.gemini\antigravity\scratch\facebook_page_commenter\fb_commenter.db`

---

## 3. Kiến trúc hệ thống & Luồng xử lý (Hybrid Engine)

### 3.1. Phân loại 3 chế độ kết nối Fanpage:
1. **`GRAPH_API`:** Dành cho Page có `Page Access Token` hợp lệ từ App Meta Developer. Gửi comment qua Graph API v19.0 siêu nhẹ và chạy hoàn toàn ngầm.
2. **`ANTIDETECT` (`ANTIDETECT_READY`):** Dành cho 50 Fanpage ở các nick clone/via trong Antidetect Python không đăng ký được Meta Developer.
   - Quét bài mới & gửi comment bằng cách điều khiển trình duyệt qua giao thức Chrome DevTools Protocol (CDP).
   - Dùng `antidetect.human.HumanInput` để di chuột đường cong Bezier, delay phím lognormal theo kiểu người thật, gõ vào ô nhập comment và gửi phím `Enter`.
3. **`HYBRID`:** Dành cho 8 Fanpage của nick `Bóng Ma Đề Cương` vừa có Token Graph API vừa gắn Profile Antidetect. Bình thường ưu tiên bắn API ngầm; nếu Token hết hạn tự động chuyển sang chế độ CDP mà không làm gián đoạn lịch đăng.

### 3.2. Quy tắc an toàn trình duyệt (Browser Ownership):
- Nếu Profile Antidetect **đang mở sẵn**: Công cụ chỉ mở 1 tab tạm (`TRANSIENT_TARGET_IDS`), thực thi quét/bình luận rồi đóng tab tạm — **tuyệt đối không tắt trình duyệt của người dùng**.
- Nếu Profile Antidetect **đang đóng**: Công cụ tự gọi khởi động profile qua `BrowserLauncher` / FastAPI server của Antidetect, bình luận xong tự động đóng lại nếu `ANTIDETECT_AUTO_CLOSE_BROWSER=1`.

### 3.3. Các lớp bảo vệ Anti-Spam & Hàng đợi tác vụ:
- `UNIQUE(dedupe_key)`: Ngăn chặn triệt để tình trạng bình luận trùng lặp trên cùng một bài viết (`auto:{page_id}:{post_id}` hoặc `manual:{batch_id}:{post_id}`).
- `MIN_GAP_PER_PAGE_SEC = 45`: Đảm bảo khoảng cách tối thiểu giữa 2 lần comment trên cùng 1 Fanpage.
- `MAX_COMMENTS_PER_PAGE_PER_DAY = 20`: Giới hạn số lượng comment tối đa trong ngày của từng Page.
- Re-read fresh state: Trước mỗi tác vụ trong cùng 1 batch, hệ thống luôn đọc lại số lượng comment hôm nay và mốc thời gian gần nhất từ DB để chống bypass giới hạn.
- Cơ chế `NEED_CHECK`: Khi mạng bị ngắt hoặc gặp timeout lúc nhấn gửi, tác vụ không tự động retry để tránh spam lặp, mà chuyển sang `NEED_CHECK` kèm tự đối chiếu DOM/Graph API.

### 3.4. Cơ chế đồng bộ Google Sheet thông minh (`sheets_sync.py`):
- Hỗ trợ cả 2 định dạng:
  - **1 Bảng gộp duy nhất (như tab hiện tại của user `gid=1405549804`):** Cột A là `ID trang` (hỗ trợ cả ID số, tên có dấu, hoặc slug không dấu `Chidepthichve`, `Chidepthichdoll`, `Taphoadoll`), cột B là `Comment`. Các dòng cùng Page sẽ được tự động gom thành danh sách mẫu để Spintax xoay vòng ngẫu nhiên.
  - **2 Tab truyền thống (`Pages` + `Comments`):** Đầy đủ các cột cấu hình riêng.
- Hỗ trợ Spintax `{A|B|C}`, kiểm tra tính cân bằng của dấu ngoặc nhọn `{}` và loại bỏ các chuỗi số bị lỗi hàm mũ khoa học `1.029E+14`.

---

## 4. Danh mục File mã nguồn chính
- [`run.py`](file:///C:/Users/admin/.gemini/antigravity/scratch/facebook_page_commenter/run.py): Entry point khởi chạy Dashboard, tự động chọn cổng 8000/8080 và tự mở trình duyệt.
- [`app.py`](file:///C:/Users/admin/.gemini/antigravity/scratch/facebook_page_commenter/app.py): FastAPI app + Dashboard UI tiếng Việt tích hợp đầy đủ các chức năng quản lý, hẹn giờ thủ công và giám sát hàng đợi.
- [`scheduler.py`](file:///C:/Users/admin/.gemini/antigravity/scratch/facebook_page_commenter/scheduler.py): 3 vòng lặp ngầm (`loop_post_scanner`, `loop_task_executor`, `loop_maintenance`) dùng `asyncio.to_thread`.
- [`antidetect_client.py`](file:///C:/Users/admin/.gemini/antigravity/scratch/facebook_page_commenter/antidetect_client.py): Cầu nối CDP với `Antidetect Python`, tích hợp `HumanInput` và cơ chế quản lý tab tạm.
- [`sheets_sync.py`](file:///C:/Users/admin/.gemini/antigravity/scratch/facebook_page_commenter/sheets_sync.py): Đồng bộ cấu hình và mẫu comment từ Google Sheet không để lộ token.
- [`database.py`](file:///C:/Users/admin/.gemini/antigravity/scratch/facebook_page_commenter/database.py): SQLite CRUD, khóa gửi `SENDING`, phục hồi stale task và quản lý trạng thái Page.
- [`meta_client.py`](file:///C:/Users/admin/.gemini/antigravity/scratch/facebook_page_commenter/meta_client.py): Meta Graph API v19.0 client.
- [`crypto.py`](file:///C:/Users/admin/.gemini/antigravity/scratch/facebook_page_commenter/crypto.py): Mã hóa token bằng Fernet (`ENCRYPTION_KEY` lưu trong `.env`).
- [`spintax.py`](file:///C:/Users/admin/.gemini/antigravity/scratch/facebook_page_commenter/spintax.py): Trộn nội dung Spintax đa tầng `{A|B}`.
- [`test_system.py`](file:///C:/Users/admin/.gemini/antigravity/scratch/facebook_page_commenter/test_system.py): Bộ kiểm thử tự động 17 test cases bao quát toàn bộ chức năng.

---

## 5. Lệnh vận hành thường dùng

### 5.1. Chạy kiểm thử hệ thống:
```powershell
pytest C:\Users\admin\.gemini\antigravity\scratch\facebook_page_commenter\test_system.py -v
```

### 5.2. Khởi động Dashboard quản trị:
```powershell
python C:\Users\admin\.gemini\antigravity\scratch\facebook_page_commenter\run.py
```
Dashboard mở tại: `http://127.0.0.1:8000` (hoặc `8080`).

---

## 6. Trạng thái vận hành & Nhiệm vụ tiếp theo
1. **Trạng thái hiện tại:** Hệ thống đang bật cờ `PAUSED_BY_DEFAULT=1` và `paused_all: True`. Tự động comment đang TẮT để người dùng kiểm tra.
2. **Kích hoạt tự động:** Trên thanh menu Dashboard, bấm nút **"Tiếp tục chạy"** (hoặc đặt `PAUSED_BY_DEFAULT=0` trong `.env`) khi người dùng muốn bắt đầu quét bài và gửi comment tự động.
3. **Mở rộng tương lai (Roadmap):**
   - Thiết lập chạy ngầm Windows Service hoặc Task Scheduler để tự khởi động cùng máy tính.
   - Thêm bộ lọc bài viết theo từ khóa (keywords filter) nếu không muốn comment vào một số bài viết đặc thù.
