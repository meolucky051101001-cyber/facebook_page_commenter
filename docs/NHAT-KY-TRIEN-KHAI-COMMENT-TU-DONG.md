Nhật ký triển khai kế hoạch sửa 5 lỗi comment tự động — 15/11/2026

**Chặng A: hoàn tất.** Bản chụp trước sửa nằm tại backups/auto-comment-20261115-834cb6ce. Có source.zip, bản vá so với HEAD, trạng thái Git, manifest băm tệp, bản sao DB nhất quán và cấu hình cục bộ. DB schema 5 có 65 Page, 65 rule, 4 tác vụ lịch sử, không có publication/inbox; tất cả Page tắt, run state paused. quick_check đạt; 9 token Page trong bản sao giải mã được. Năm test hồi quy ban đầu đều thất bại đúng nguyên nhân đã review.

**Chặng B: hoàn tất.** F1 ghi INVALID_SECRET vào DB với điều kiện token mã hóa chưa thay đổi; task PENDING chuyển PAUSED. Executor lấy thêm ứng viên với giới hạn 100 ứng viên/10 lần xác minh mỗi lượt. Page khỏe sau 10 task hỏng được xử lý ngay. Có test chống kết quả kiểm tra cũ ghi đè token vừa kết nối lại.

**Chặng C: hoàn tất.** Schema 6 bổ sung cooldown/backoff, metadata nội dung. comment_dispatch dùng khóa theo Page và chung hạn mức, khoảng cách, cooldown cho comment/reply. Bộ đếm được cập nhật nguyên tử cùng kết quả, chống cộng đôi. Có cooldown cấp APP/ACCOUNT khi mã lỗi xác định phạm vi rộng hơn Page; giữ Retry-After. service_lock khóa DB trước recovery, hệ điều hành giải phóng khóa khi tiến trình kết thúc.

**Chặng D: hoàn tất.** comment_content xử lý Spintax lồng nhau, tên Page như dữ liệu, phát hiện nhánh rỗng/biến lạ/độ dài. Nội dung được tạo ổn định theo bài, bước và phiên bản cấu hình. Đồng bộ cập nhật task đơn PENDING/PAUSED; các API khôi phục dùng chung bước đọc lại Sheet. Nhận việc gửi kiểm tra và cập nhật nội dung trong transaction; chuỗi và tác vụ cần đối chiếu giữ snapshot.

Điều chỉnh kỹ thuật có căn cứ: thêm tasks.content_delay_minutes ngoài các cột dự kiến để biết delay đã dùng cho task. Cột này giúp retry FAILED/NEED_CHECK tính lại đúng lịch khi Sheet đổi delay lúc task không còn PENDING/PAUSED. Test riêng xác nhận task không gửi sớm sau khi delay tăng từ 10 lên 30 phút.

**Chặng E: hoàn tất kiểm thử cục bộ.** Dashboard hiển thị INVALID_SECRET, hạn mức dùng/còn lại, lý do chờ và mốc thử tiếp. Hộp thư trả trạng thái chờ có cấu trúc; nội dung nhập tay được giữ trong bộ nhớ trang khi gửi bị chặn. Bản đối chiếu integration/antidetect được cập nhật thông tin hạn mức; chưa áp dụng sang thư mục cài đặt Antidetect bên ngoài project.

Phân loại 17 test cũ:

| Nhóm test cũ | Số test | Cách xử lý |
| --- | --- | --- |
| Lifecycle, min gap, single flight, pause giữa batch, tắt auto giữ manual | 5 | Chuẩn bị Page đã xác minh, bật auto khi cần và giả lập nguồn Sheet/kiểm tra bài |
| Debug token thất bại và token hết hạn | 1 | Kiểm tra graph_ready bị chặn và task PAUSED, thay kỳ vọng is_active phải bằng 0 |
| Retry/resolve và hai endpoint manual sync | 3 | Dùng localhost và khóa dashboard thử nghiệm; kiểm tra status guard hiện tại |
| Reactivate Hybrid, ghép Antidetect, fallback scanner, maintenance Hybrid | 4 | Kiểm tra Graph API hợp lệ, ghép theo ID, không tự fallback CDP; giữ riêng test đơn vị CDP |
| Timeout reconciliation | 1 | Xác nhận NEED_CHECK và không tự suy diễn thành công |
| Maintenance khi paused | 1 | Xác nhận nguồn được refresh nhưng không gửi |
| Page thiếu trong Sheet | 1 | Kiểm tra importer multi-Page hiện tại đặt source_error, giữ quyền bật/tắt |
| Sheet tắt rule rõ ràng | 1 | Bật rule rõ ràng trong fixture trước khi kiểm tra việc tắt |

Ngoài 17 test đó, test pause sau khi claim được nối vào claim_due_task hiện tại để kiểm tra nhánh thực sự được executor gọi.

Kết quả kiểm thử cuối: **91 test đạt**, không bỏ qua test; có một cảnh báo deprecation từ thư viện Starlette/AnyIO. Bổ sung kiểm tra đổi token khi xác minh bài, đổi quy tắc sau khi claim reply, đổi Sheet khi tác vụ đã SENDING, và chờ worker thread kết thúc khi shutdown. Migration đã có test giữ mọi trạng thái task, lặp migration, rollback khi lỗi giữa chừng. Các bài kiểm thử Meta đều dùng phản hồi giả. Kiểm tra cú pháp đạt cho 24 tệp Python và 2 khối JavaScript giao diện; `git diff --check` đạt.

**Chặng F: hoàn tất.** Bản sao `backups/auto-comment-20261115-834cb6ce/rehearsal.sqlite3` nâng từ schema 5 lên 6, chạy lặp được, `quick_check=ok`; tất cả giá trị trong các cột có sẵn giữ nguyên. Trước triển khai thật, `pre-deploy.sqlite3` được tạo bằng SQLite Backup API. Các báo cáo chi tiết nằm trong `migration-rehearsal.json` và `migration-deployment.json`.

**Chặng G: đã triển khai dịch vụ cục bộ; còn nghiệm thu gửi thật.** DB vận hành có schema 6, 8 account, 65 Page, 65 rule, 4 task lịch sử; 9 token giải mã được. Tất cả 65 rule tự động vẫn tắt và run state vẫn `paused`. Dịch vụ chạy nền qua `run_background.pyw`; PID nằm trong `backups/auto-comment-20261115-834cb6ce/service.pid`. HTTP localhost hoạt động và tiến trình giữ khóa DB trước recovery/các vòng worker. Bước shutdown chờ thread nền thực sự kết thúc rồi mới nhả khóa.

Kiểm tra sau restart lúc 12:27 ngày 15/11/2026 (UTC+7): `/api/metrics`, `/api/pages`, `/api/tasks`, `/api/inbox`, `/` và `/operations` trả 200 với khóa hợp lệ; khóa sai trả 401. Không có task PENDING/SENDING/NEED_CHECK, không có publication/inbox, không bật Page và không phát sinh bình luận thật. Các bảng account, setting, Page, task, publication, inbox và quyền bật/tắt giữ nguyên so với trước triển khai. Rule được refresh từ Sheet theo chức năng đã có. Bằng chứng nằm trong `service-health.json` và `post-start-data-check.json`.

Lần đọc Sheet vận hành thành công ở chế độ MULTI_PAGE, đồng bộ 3 Page với 5 mẫu. Có 57 Page không có mẫu trong tab hiện tại; thêm 5 Page có dòng 7–11 thiếu nội dung Comment. Tổng cộng 62 Page có `source_error`. Đây là cấu hình nguồn cần người vận hành bổ sung nếu muốn bật các Page đó; mã sửa không tự tạo nội dung hay thay đổi Sheet.

Phát hiện thêm từ log vận hành: logger HTTPX ở mức INFO từng ghi URL chứa token. Đã đặt HTTPX/httpcore ở mức WARNING trong entry point nền và che giá trị nhạy cảm trong log hiện hữu. Bản ghi làm sạch là `log-redaction.json`. Bản mã sau triển khai và manifest băm nằm trong `deployed-source.zip` và `deployed-manifest.json`; `implementation.patch` đối chiếu với source.zip trước sửa sau khi chuẩn hóa CRLF/LF.

Nghiệm thu gửi thật vẫn cần Page, bài và nội dung được người dùng chỉ định theo mục 14 của kế hoạch. Phần sửa mã, migration và kiểm tra cục bộ không gửi bình luận Facebook thật.
