# Facebook Page Multi-Account Commenter Hub

Hệ thống quản lý Fanpage đa tài khoản độc lập, định tuyến kịch bản bình luận riêng cho từng Page và tự động lên lịch comment sau một khoảng thời gian ($X$ phút) kể từ khi bài viết mới được đăng.

---

## 🎯 Tính năng nổi bật

1. **Quản lý Đa tài khoản cô lập (Anti-Chain Ban):**
   - Chỉ dùng 1 App Meta duy nhất (thêm các nick làm App Tester).
   - Mỗi Fanpage được lưu trữ và sử dụng `Page Access Token` độc lập.
   - Khi 1 nick bị checkpoint hoặc mất quyền, chỉ Page của nick đó dừng, các Page ở nick khác vẫn hoạt động 100% bình thường.

2. **Kịch bản Bình luận riêng biệt theo Page:**
   - Cài đặt thời gian chờ tùy chỉnh ($X$ phút) cho từng Page.
   - Hỗ trợ **Spintax** dạng `{Chào|Hello} {bạn|anh/chị}` và biến `{page_name}`. Câu đã tạo được lưu ổn định qua đồng bộ, restart và retry khi cấu hình không đổi.
   - Công tắc Bật/Tắt tự động độc lập cho từng Page.

3. **Cơ chế Hàng đợi Tác vụ (Task Queue) an toàn:**
   - Khóa chống trùng theo Page/bài và từng bước chuỗi; gửi thủ công có mã batch riêng.
   - Bộ nhận diện sự cố: Tự động phân loại mã lỗi Meta: `SUCCESS`, `FAILED`, `TOKEN_EXPIRED`, và `NEED_CHECK` (trường hợp mất mạng hoặc phản hồi không rõ ràng, yêu cầu kiểm tra thực tế để không comment đúp).

4. **Giao diện Dashboard trực quan (FastAPI Web App):**
   - Quản lý Nick & Page.
   - Cấu hình kịch bản & thời gian trễ.
   - Soạn và hẹn giờ bình luận thủ công.
   - Theo dõi trạng thái, nhật ký và nút hủy lịch đang chờ.

---

## 🚀 Hướng dẫn khởi chạy

### 1. Cài đặt thư viện phụ thuộc (nếu chưa có)
```bash
pip install -r requirements.txt
```

### 2. Cấu hình biến môi trường
Sao chép `.env.example` thành `.env` rồi điền các giá trị cần thiết. Nếu chưa có `.env`, hệ thống tự tạo từ file mẫu ở lần khởi động đầu tiên.

### 3. Khởi chạy ứng dụng
```bash
cd "C:\Projects\Tools\facebook comment\facebook_page_commenter"
python run.py
```

Ứng dụng sẽ chạy tại địa chỉ: **[http://127.0.0.1:8080](http://127.0.0.1:8080)**. Dashboard dùng khóa `DASHBOARD_ADMIN_KEY` và bind vào localhost.

### Chuỗi bình luận theo Page

Trong tab `Comments - Multi-Page`, thêm cột `Thứ tự` sau cột số phút. Mỗi dòng của một chuỗi có nội dung riêng và số phút tính từ lúc bài đăng. Ví dụ:

| ID trang | Tên Fanpage | Comment | Số phút chờ sau khi đăng | Thứ tự |
| --- | --- | --- | --- | --- |
| 100000000000001 | Page A | Xem thông tin sản phẩm trong bài. | 30 | 1 |
| 100000000000001 | Page A | Shop giao hàng toàn quốc. | 35 | 2 |
| 100000000000001 | Page A | Inbox Page nếu cần tư vấn thêm. | 40 | 3 |

Page có cột Thứ tự để trống ở mọi dòng vẫn dùng một mẫu cho mỗi bài như trước. Khi đã tạo chuỗi cho một bài, nội dung và khoảng cách của chuỗi đó được lưu vào hàng đợi. Thay đổi trong Sheet áp dụng cho các chuỗi tạo sau. Nếu một bình luận gửi muộn, tool dời mốc tiếp theo để giữ khoảng cách đã đặt. Tác vụ cần đối chiếu sẽ giữ các bước sau ở trạng thái chờ.

Mục **Hộp thư, xuất bản & báo cáo** có lịch đăng và nhập CSV, hộp thư bình luận thật, báo cáo lỗi, và bản nháp Gemini tùy chọn. Lịch ảnh hoặc Reel nhận URL HTTPS mà Meta có thể tải. Bình luận kèm riêng trong lịch đăng được ưu tiên cho bài đó. Để ô này trống nếu muốn dùng chuỗi của Page. Các thao tác trên Facebook dùng quyền Meta của Page đã kết nối.

---

## Kiểm thử
```bash
python -m pytest -q --basetemp .pytest_cache/test-run
```

Toàn bộ bộ kiểm thử dùng SQLite tạm, khóa mã hóa thử nghiệm và HTTP mô phỏng. Không dùng token thật hoặc gửi bình luận thật.

Hạn mức `max_comments_per_day` của mỗi Page được dùng chung cho bình luận bài viết và trả lời trong hộp thư, gồm thao tác thủ công qua tool. Mặc định khoảng cách tối thiểu là 45 giây, ngày tính theo UTC+7. Ẩn/hiện bình luận không dùng hạn mức này. Các bình luận đăng bằng ứng dụng Facebook bên ngoài tool không nằm trong bộ đếm.

Khi Meta báo rate limit, tool lưu mốc chờ theo Page, tài khoản hoặc ứng dụng tùy mã lỗi. Nhịp chờ tăng 2, 4, 8, 16 phút rồi tối đa `SEND_BACKOFF_MAX_MINUTES` (mặc định 30); `Retry-After` có thể kéo dài mốc này. Restart, sửa Sheet và bấm retry không xóa mốc chờ. Dashboard hiển thị nguyên nhân chờ và thời điểm thử tiếp.

Mẫu của bình luận đơn được cập nhật khi tác vụ đang PENDING hoặc PAUSED. Bấm chạy tiếp sẽ kiểm tra nguồn Sheet trước khi khôi phục. Sheet lỗi thì tác vụ tự động chưa được khôi phục. Nội dung được chốt khi nhận lượt gửi; tác vụ SENDING và NEED_CHECK giữ nội dung để đối chiếu. Chuỗi đã tạo giữ snapshot; đổi chế độ trên Sheet áp dụng cho bài mới. Tác vụ PAUSED quá hạn hơn hai giờ vẫn cần đối chiếu, không tự đặt lại lịch thành hiện tại.

Token lỗi giải mã sẽ được đánh dấu INVALID_SECRET và tác vụ chờ của Page đó chuyển PAUSED. Kết nối lại và kiểm tra đúng danh tính Page trước khi khôi phục. Scheduler dùng Graph API; dữ liệu Antidetect giúp ghép profile và quản lý danh mục, không tự thay thế token hết hạn bằng đường gửi CDP.

Ứng dụng khóa theo đường dẫn DB trước khi recovery, kể cả chạy qua `run.py`, `run_background.pyw` hoặc Uvicorn trực tiếp. Chỉ một tiến trình được dùng DB. Trạng thái chạy được lưu trong DB: chưa có trạng thái thì tạm dừng, đã có thì khôi phục trạng thái đã lưu. Để cập nhật dịch vụ, tạm dừng trước, sao lưu bằng SQLite Backup API và kiểm tra bản sao. Nếu đã gửi sau nâng cấp, phải giữ lịch sử gửi mới khi rollback để tránh mất khóa chống trùng.

---

## Kết nối tài khoản Facebook

1. Cấu hình `META_APP_ID`, `META_APP_SECRET` và `META_OAUTH_REDIRECT_URI` trong `.env`. URI phải kết thúc bằng `/facebook/callback`, ví dụ `http://localhost:8080/facebook/callback` nếu Meta chấp nhận URI đó cho app của bạn.
2. Đăng ký cùng URI trong cài đặt Facebook Login của Meta App. Mở dashboard bằng đúng host và cổng trong URI.
3. Bấm **Kết nối Facebook** trong **Quản lý Page & Nick** rồi cấp quyền cho các Page bạn quản lý. Chọn thêm quyền đọc bình luận khách hoặc xuất bản nếu dùng các mục đó.
4. Kiểm tra kết nối của từng Page trước khi bật lịch. Nếu Meta không cho phép tài khoản cấp quyền, kiểm tra chế độ app, vai trò của tài khoản và quyền được duyệt trong Meta App Dashboard.

Tool lấy Page Access Token qua danh sách do Meta trả về. Dashboard cũng nhận một User Access Token có sẵn để nhập danh sách Page của tài khoản đó.
