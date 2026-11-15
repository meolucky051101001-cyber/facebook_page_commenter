# HANDOFF — Facebook Page Multi-Account Commenter Hub v2.0 (Hybrid Antidetect)

Tài liệu bàn giao toàn diện dành cho lập trình viên hoặc AI Agent tiếp nhận dự án.

---

## 1. Tóm tắt một câu về trạng thái hiện tại
Codebase đã đóng nốt **3 lỗi P0 cuối** (định danh Page qua CDP khớp tuyệt đối, sync Google Sheet không tự bật lại rule đã tắt, tạm dừng khẩn cấp chặn giữa batch), **41/41 bài kiểm thử tự động PASSED**, đã kết nối sẵn **59 Fanpage** (8 Hybrid, 1 Graph API, 50 Antidetect CDP), hỗ trợ đồng bộ trực tiếp mẫu comment từ Google Sheet của người dùng (`gid=1405549804`), và **vẫn đang TẠM DỪNG (`paused_all: True`)** với **0 comment thật đã gửi**.
Chưa được coi là production-ready cho tới khi chạy **pilot 1 Page có giám sát** và người dùng xác nhận (xem mục 6.2).

---

## 2. Thông tin môi trường & Đường dẫn cốt lõi
- **Thư mục mã nguồn:** `C:\Users\admin\Documents\Qoder\2026-11-08\9e7447c1\facebook_page_commenter`
- **Môi trường Python:** Python 3.10.11 (`fastapi`, `uvicorn`, `httpx`, `cryptography`, `python-dateutil`, `websockets`, `pytest`)
- **Phần mềm Antidetect tích hợp:** `C:\Projects\Tools\Antidetect Python` (bảo tồn nguyên trạng, `profiles.json` hash `B26426E4...` không bị thay đổi).
- **Google Sheet cấu hình:** `https://docs.google.com/spreadsheets/d/1Qq1lkhVzlDe0FJ5k3HDDkBiGYDqTItO2ycQMDQHATD0/edit?usp=sharing` (tab `gid=1405549804`).
- **Cơ sở dữ liệu SQLite:** `C:\Users\admin\Documents\Qoder\2026-11-08\9e7447c1\facebook_page_commenter\fb_commenter.db`

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
- `MAX_COMMENTS_PER_PAGE_PER_DAY = 20`: Giới hạn mặc định; mỗi Page có thể ghi đè bằng cột "mỗi ngày mấy comment" trên Sheet (1–1000).
- Re-read fresh state: Trước mỗi tác vụ trong cùng 1 batch, hệ thống luôn đọc lại số lượng comment hôm nay và mốc thời gian gần nhất từ DB để chống bypass giới hạn. Bộ đếm ngày theo UTC+7 (`DAILY_COUNTER_UTC_OFFSET_HOURS`).
- **Cổng định danh Page tuyệt đối (CDP):** trước khi gõ comment, tool chỉ chấp nhận khi mọi actor name đọc được khớp **chính xác** tên Page; menu chọn actor có >1 mục trùng tên chính xác sẽ bị chặn `INVALID_TARGET`. Không còn so khớp chuỗi con (tránh "Page Mẫu" khớp nhầm "Page Mẫu Outlet" hoặc tài khoản cá nhân).
- **Tạm dừng khẩn cấp chặn giữa batch:** mỗi task kiểm tra cờ pause trước khi xử lý, trước và sau khi lock; nếu pause rơi ngay sau lock, task được trả về `PENDING` (xóa `locked_at`) thay vì để `SENDING`.
- Cơ chế `NEED_CHECK`: Khi mạng bị ngắt hoặc gặp timeout lúc nhấn gửi, tác vụ không tự động retry để tránh spam lặp, mà chuyển sang `NEED_CHECK` kèm tự đối chiếu DOM/Graph API.

### 3.4. Cơ chế đồng bộ Google Sheet thông minh (`sheets_sync.py`):
- Hỗ trợ cả 2 định dạng:
  - **1 Bảng gộp duy nhất (như tab hiện tại của user `gid=1405549804`):** Cột A là `ID trang` (hỗ trợ cả ID số, tên có dấu, hoặc slug không dấu `Chidepthichve`, `Chidepthichdoll`, `Taphoadoll`), cột B là `Comment`. Các dòng cùng Page sẽ được tự động gom thành danh sách mẫu để Spintax xoay vòng ngẫu nhiên.
  - **2 Tab truyền thống (`Pages` + `Comments`):** Đầy đủ các cột cấu hình riêng.
- Hỗ trợ Spintax `{A|B|C}`, kiểm tra tính cân bằng của dấu ngoặc nhọn `{}` và loại bỏ các chuỗi số bị lỗi hàm mũ khoa học `1.029E+14`.
- **An toàn trạng thái rule:** nếu cột "Bật tự động" vắng mặt hoặc để trống, sync giữ nguyên `rule_enabled` hiện tại của Page; chỉ TRUE/FALSE (hoặc các từ đồng nghĩa hợp lệ) mới đổi trạng thái. Giá trị lạ bị báo lỗi dòng và bỏ qua — không âm thầm bật lại Page đã bị tắt.

---

## 4. Danh mục File mã nguồn chính
- [`run.py`](file:///C:/Users/admin/Documents/Qoder/2026-11-08/9e7447c1/facebook_page_commenter/run.py): Entry point khởi chạy Dashboard, tự động chọn cổng 8000/8080 và tự mở trình duyệt.
- [`app.py`](file:///C:/Users/admin/Documents/Qoder/2026-11-08/9e7447c1/facebook_page_commenter/app.py): FastAPI app + Dashboard UI tiếng Việt tích hợp đầy đủ các chức năng quản lý, hẹn giờ thủ công và giám sát hàng đợi.
- [`scheduler.py`](file:///C:/Users/admin/Documents/Qoder/2026-11-08/9e7447c1/facebook_page_commenter/scheduler.py): 3 vòng lặp ngầm (`loop_post_scanner`, `loop_task_executor`, `loop_maintenance`) dùng `asyncio.to_thread`.
- [`antidetect_client.py`](file:///C:/Users/admin/Documents/Qoder/2026-11-08/9e7447c1/facebook_page_commenter/antidetect_client.py): Cầu nối CDP với `Antidetect Python`, tích hợp `HumanInput` và cơ chế quản lý tab tạm.
- [`sheets_sync.py`](file:///C:/Users/admin/Documents/Qoder/2026-11-08/9e7447c1/facebook_page_commenter/sheets_sync.py): Đồng bộ cấu hình và mẫu comment từ Google Sheet không để lộ token.
- [`database.py`](file:///C:/Users/admin/Documents/Qoder/2026-11-08/9e7447c1/facebook_page_commenter/database.py): SQLite CRUD, khóa gửi `SENDING`, trả lock khi pause, phục hồi stale task và quản lý trạng thái Page.
- [`migrations.py`](file:///C:/Users/admin/Documents/Qoder/2026-11-08/9e7447c1/facebook_page_commenter/migrations.py): Nâng cấp schema SQLite cho các bản cũ.
- [`meta_client.py`](file:///C:/Users/admin/Documents/Qoder/2026-11-08/9e7447c1/facebook_page_commenter/meta_client.py): Meta Graph API v19.0 client.
- [`crypto.py`](file:///C:/Users/admin/Documents/Qoder/2026-11-08/9e7447c1/facebook_page_commenter/crypto.py): Mã hóa token bằng Fernet (`ENCRYPTION_KEY` lưu trong `.env`).
- [`spintax.py`](file:///C:/Users/admin/Documents/Qoder/2026-11-08/9e7447c1/facebook_page_commenter/spintax.py): Trộn nội dung Spintax đa tầng `{A|B}`.
- [`test_system.py`](file:///C:/Users/admin/Documents/Qoder/2026-11-08/9e7447c1/facebook_page_commenter/test_system.py): Bộ kiểm thử tự động **41 test cases**, bao phủ queue, token encryption, Sheet sync, CDP actor gate, pause mid-batch và API dashboard.

---

## 5. Lệnh vận hành thường dùng

### 5.1. Chạy kiểm thử hệ thống:
```powershell
cd C:\Users\admin\Documents\Qoder\2026-11-08\9e7447c1\facebook_page_commenter
python -m pytest test_system.py -q
```
Kỳ vọng hiện tại: **41 passed** (1 cảnh báo deprecation của Starlette, không ảnh hưởng).

### 5.2. Khởi động Dashboard quản trị:
```powershell
python C:\Users\admin\Documents\Qoder\2026-11-08\9e7447c1\facebook_page_commenter\run.py
```
Dashboard mở tại: `http://127.0.0.1:8000` (hoặc `8080` nếu cổng 8000 bận). Mặc định khởi động ở trạng thái **TẠM DỪNG**.

### 5.3. Trình tự thao tác trên Dashboard:
1. Tab **Kết nối & Fanpage**: kiểm tra Page, token/profile, trạng thái active.
2. Tab **Cấu hình Rule**: chọn Page → delay, max/ngày, mẫu Spintax → Lưu.
3. Tab **Đồng bộ Google Sheet**: dán link Sheet → Đồng bộ ngay.
4. Tab **Soạn thủ công**: chọn Page/bài → nội dung → Đăng ngay hoặc Hẹn giờ.
5. Tab **Hàng đợi**: theo dõi `PENDING → SUCCESS`, xử lý `NEED_CHECK` thủ công.
6. Nút **Tạm dừng khẩn cấp** trên thanh menu: chặn scan + dừng batch ngay.

---

## 6. Trạng thái bàn giao, pilot và tồn đọng

### 6.1. Trạng thái bàn giao (2026-11-09)
- **HEAD:** `3889a78` — `fix: close 3 P0 gaps in actor identity, sheet sync, and pause` (5 file, +500/−73).
- **Git:** nhánh `master`, **đang ahead 3 commit so với `origin/master`** (chưa push). Working tree chỉ có `HANDOFF.md` được cập nhật trong lượt bàn giao này, chưa commit.
- **Kiểm thử:** `41 passed`; test dùng SQLite tạm cho từng test, không đụng DB vận hành.
- **Vận hành:** `paused_all = True`, **0 comment thật đã gửi**. Không có profile Antidetect nào bị mở/đóng bởi các lượt test.
- **Việc đã đóng gần nhất:** API reactivate Page, accounting token/usage, Sheet 2 định dạng + chống trùng tên, retry/resume, cảnh báo cấu hình, docs; 3 P0 cuối về actor identity, Sheet enable, pause giữa batch.

### 6.2. Pilot bắt buộc trước khi mở khóa toàn bộ
1. Xác nhận Dashboard đang **TẠM DỪNG**; backup `fb_commenter.db` (file `.bak` hiện có, kiểm tra dung lượng/thời gian).
2. Chọn **đúng 1 Page** đại diện (ưu tiên Graph API hoặc Page Antidetect đang mở sẵn): delay 10–15 phút, 2–3 mẫu khác nhau, trần/ngày 2–3.
3. Bấm **Tiếp tục chạy**, chỉ để chạy 1–2 bài mới; theo dõi tab Hàng đợi chuyển `PENDING → SUCCESS`.
4. Mở Facebook kiểm tra tay: đúng Page, đúng bài, đúng nội dung/spintax, không comment trùng, không comment bằng tài khoản cá nhân.
5. Test **tạm dừng giữa batch** khi có ≥2 task đến hạn: task đang gửi được hoàn tất, các task còn lại phải ở `PENDING` (không `SENDING`/`NEED_CHECK` giả).
6. Nếu tất cả đạt, mở rộng 3–5 Page; nếu xuất hiện `INVALID_TARGET` hoặc `NEED_CHECK`, dừng lại đối chiếu trước khi tăng số Page.
- **Không bật toàn bộ 59 Page ngay.**

### 6.3. Tồn đọng P1 đã biết (chưa vá)
- [`antidetect_client.py:672`](file:///C:/Users/admin/Documents/Qoder/2026-11-08/9e7447c1/facebook_page_commenter/antidetect_client.py#L672): `MetaAPIError` gọi sai chữ ký (`message` + `code=` + `category=`) → `TypeError`, bị `except Exception: continue` nuốt; lỗi checkpoint khi quét bài qua CDP không được báo đúng. Cần sửa theo đúng signature trong `meta_client.py`.
- Lỗi từng Page trong lúc scan bị nuốt im lặng (`except Exception: continue`), Dashboard không thấy lý do.
- `api_usage_percent >= 90` khiến Page bị bỏ quét vĩnh viễn vì chưa có cơ chế reset/giảm usage.
- Cần xác minh lại tham số `since` dạng ISO khi gọi Graph API (nguy cơ bỏ sót bài mới nếu Meta hiểu khác múi giờ).
- API localhost chưa kiểm tra `Origin`/`Host` → rủi ro CSRF từ một trang web khác đang mở trong cùng trình duyệt.
- `/api/pages/{id}/reactivate` không tự resume task `PAUSED`; phải gọi endpoint `/resume_tasks` riêng.
- Xác minh comment CDP chỉ dựa vào 40 ký tự đầu trong DOM → có thể `NEED_CHECK` nhầm nếu comment chưa render kịp.
- Một số endpoint trả `str(e)` thô, có thể lộ chi tiết nội bộ.
- Cổng actor CDP hiện **fail-closed**: nếu Facebook đổi DOM khiến không đọc được tên actor, tool chặn gửi. Cần theo dõi trong pilot; nếu bị chặn nhầm, bổ sung vùng DOM composer (không quay lại quét toàn bộ body).

### 6.4. P2 backlog (chưa làm)
- `DEFAULT_SHEET_URL`/GID còn hardcode trong `config.py`; các file `template_*.csv` chứa dữ liệu thật vẫn được track trong repo.
- SQLite chưa bật WAL; backup `.bak` có thể bị ghi đè, chưa có lịch sử backup.
- `requirements.txt` chưa pin phiên bản; `run.py` tự fallback cổng; chưa cảnh báo nếu chạy multi-worker.
- Trạng thái pause chỉ lưu trong RAM, restart sẽ trở về `PAUSED_BY_DEFAULT`.
- Chưa có file logging/rotation; metrics chỉ giữ last run/last error.
- Maintenance có thể tự bật lại Page có token hợp lệ nhưng người dùng cố ý tắt.
- Chưa có rate-limit/khung giờ hoạt động/jitter khi rải comment; chưa giãn cách giữa các Page.
- Roadmap dài hạn: Windows Service/Task Scheduler tự khởi động, lọc bài theo từ khóa.

---

## 7. Nguyên tắc an toàn khi tiếp nhận
1. **Repo private**, không commit `.env`, `fb_commenter.db`, `.bak`, log hoặc token.
2. **Không sửa/ghi** `data/profiles.json`, `secrets.dpapi` hay bất kỳ file nào trong `Antidetect Python`; chỉ đọc.
3. Không in proxy password/token/URL nhạy cảm ra chat/log; token trong DB đã mã hóa Fernet.
4. Không dừng/khởi động lại job đang chạy của người dùng; "dừng task" = hủy agent job, báo tab/profile còn sót.
5. Chỉ commit đúng phạm vi được yêu cầu; tách workstream; hỏi trước nếu có file bẩn không liên quan.
6. Không tuyên bố production-ready trước pilot và xác nhận của người dùng.
