# Bàn giao hiện trạng Commenter Facebook

Cập nhật: 15/11/2026, Asia/Bangkok. Thư mục làm việc và chạy dịch vụ:
`C:\Projects\Tools\facebook comment\facebook_page_commenter`.

Đọc kế hoạch trong `docs/KE-HOACH-SUA-LOI-COMMENT-TU-DONG.md` và kết quả từng chặng trong `docs/NHAT-KY-TRIEN-KHAI-COMMENT-TU-DONG.md`. Tài liệu `docs/legacy` chỉ để tham khảo lịch sử.

## Trạng thái đã kiểm chứng

- Dịch vụ chạy nền qua `pythonw run_background.pyw` tại `127.0.0.1:8080`. PID ở `backups/auto-comment-20261115-834cb6ce/service.pid`; phải kiểm tra lại tiến trình và API khi bắt đầu phiên mới.
- DB schema 6, 8 tài khoản, 65 Page, 65 rule; 4 tác vụ lịch sử gồm 3 SUCCESS và 1 CANCELLED. Không có tác vụ PENDING, SENDING hoặc NEED_CHECK, không có publication hay inbox tại thời điểm bàn giao. Chín token Page còn giải mã được; điều này không thay thế kiểm tra quyền Meta trực tiếp.
- Toàn hệ thống đang `paused`; tất cả 65 Page tắt tự động. Đợt sửa lỗi này không gửi bình luận hoặc trả lời thật trên Facebook.
- Sheet đang dùng được chọn trong DB, ưu tiên hơn URL PILOT trong .env. Lần đọc thực tế thành công ở chế độ MULTI_PAGE: 3 Page hợp lệ, 5 mẫu. Có 57 Page chưa có dòng mẫu và 5 Page ở dòng 7–11 thiếu nội dung Comment; tổng 62 Page có lỗi nguồn. Không tự tạo nội dung để lấp những dòng này.
- Các API `/api/metrics`, `/api/pages`, `/api/tasks`, `/api/inbox` và hai trang `/`, `/operations` trả 200 với khóa hợp lệ; API trả 401 khi khóa sai. Chưa nghiệm thu giao diện bằng trình duyệt tương tác.

## Hành vi mới cần giữ

1. Page lỗi giải mã được lưu `INVALID_SECRET`; task PENDING của Page chuyển PAUSED. Lỗi token cũ không được ghi đè token mới. Page khỏe tiếp tục được xử lý.
2. Bình luận bài và reply dùng chung quota ngày UTC+7, khoảng cách gửi và khóa theo Page; thao tác thủ công qua tool cũng tuân theo. Ẩn/hiện bình luận không tính quota bình luận. Không tính bình luận tạo bên ngoài tool.
3. Nội dung Spintax được tạo ổn định theo bài, bước và phiên bản cấu hình. Tên Page là dữ liệu, không phải cú pháp. Tác vụ đơn PENDING/PAUSED cập nhật theo Sheet; nội dung được chốt khi claim, SENDING/NEED_CHECK giữ nguyên để đối chiếu.
4. Rate limit lưu cooldown theo Page, account hoặc app; restart, sync và retry không xóa mốc này. Giữ Retry-After dài hơn nếu Meta trả về.
5. Resume một task hoặc cả Page dùng cùng bước chuẩn bị; task đơn tự động buộc đọc lại Sheet. Chuỗi đã tạo giữ snapshot; đổi chế độ chỉ áp dụng bài mới. NEED_CHECK phải đối chiếu trước khi cho phép gửi lại.
6. Khóa DB được lấy trước init/recovery ở lifespan cho mọi entry point. Khi shutdown, ứng dụng chờ worker thread thực sự kết thúc mới nhả khóa. Trạng thái running/paused được lưu trong DB và khôi phục khi khởi động; không mặc định rằng restart luôn paused.

Scheduler gửi qua Graph API. Antidetect cung cấp danh mục/profile, không tự fallback sang CDP khi Graph bị chặn. Bản `integration/antidetect` đã cập nhật thông tin quota/lý do chờ; chưa chép sang thư mục Antidetect cài đặt bên ngoài project.

## Kiểm thử và bằng chứng

Kết quả kiểm thử mã gửi/migration: 91 passed, không bỏ qua test, một cảnh báo deprecation Starlette/AnyIO. Đã kiểm tra cú pháp 24 tệp Python và 2 khối JavaScript giao diện. Mọi lời gọi Meta trong test đều được giả lập; fixture chặn HTTP thật và dùng DB/khóa thử nghiệm.

```powershell
python -m pytest -q --tb=short -p no:cacheprovider --basetemp .pytest_cache/test-run
```

Thư mục `backups/auto-comment-20261115-834cb6ce` chứa:

- `source.zip`, `database.sqlite3`, `config.env`, `manifest.json`: điểm khôi phục đầu đợt, gồm thay đổi có sẵn trước khi sửa.
- `pre-deploy.sqlite3`: bản sao schema 5 ngay trước triển khai; `rehearsal.sqlite3`: bản nâng cấp thử.
- `migration-rehearsal.json`, `migration-deployment.json`: so sánh mọi cột cũ, schema mới, token giải mã và quick_check.
- `service-health.json`, `post-start-data-check.json`: API/khóa DB và bảo toàn lịch sử, mốc quét, quyền bật/tắt sau startup. Page rule được refresh từ Sheet như thiết kế.
- `deployed-source.zip`, `deployed-manifest.json`, `implementation.patch`: mã triển khai, băm tệp và diff so với điểm bắt đầu, chuẩn hóa CRLF/LF.
- `log-redaction.json`: ghi nhận che token trong log cũ. Entry point nền đặt httpx/httpcore ở WARNING để không ghi URL chứa token.

.env, DB, backup và log là dữ liệu cục bộ, không commit hoặc in ra chat. Đọc log qua bộ che bí mật trước khi đưa vào báo cáo.

## Chạy tiếp và khôi phục

Trước khi mở tự động, cần người dùng chỉ định Page, bài và nội dung nghiệm thu gửi thật theo mục 14 của kế hoạch. Kiểm tra cấu hình Sheet và quyền Meta của Page đó, sau đó mới bật Page và tiếp tục hệ thống. Phần sửa lỗi kỹ thuật và triển khai cục bộ đã hoàn tất; nghiệm thu gửi Facebook thật còn chờ phạm vi này.

Để khởi động nếu dịch vụ đã dừng, kiểm tra API/cổng/PID trước rồi dùng `pythonw run_background.pyw` hoặc `python run.py` trong project. Dashboard ở [http://127.0.0.1:8080](http://127.0.0.1:8080); API cần `X-Dashboard-Key` đọc cục bộ từ .env. Không khởi động thêm tiến trình vào DB đang được dùng.

Trước khi thay phiên bản, pause qua API, chờ lượt gửi hiện tại kết thúc, rồi dừng đúng tiến trình. Không xóa file khóa khi tiến trình còn chạy. Nếu chưa có lần gửi mới và cần rollback, phục hồi cặp mã trong source.zip và DB pre-deploy.sqlite3, giữ cấu hình mã hóa phù hợp. Nếu đã có lần gửi mới, giữ/đối chiếu toàn bộ lịch sử sau backup trước khi dùng DB cũ để tránh gửi trùng.

Nếu Git báo ownership, dùng `git -c safe.directory='C:/Projects/Tools/facebook comment/facebook_page_commenter' ...`. Workspace đã có nhiều thay đổi trước đợt này; không reset về HEAD để khôi phục riêng phần sửa lỗi.
