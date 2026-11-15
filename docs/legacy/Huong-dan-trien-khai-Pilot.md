# Triển khai chế độ pilot Graph API

Bản này giới hạn mọi lượt gửi qua đúng một Page. Khởi động luôn ở trạng thái tạm dừng. CDP có thể hiện trong thông tin kết nối cũ nhưng không được dùng để quét bài hoặc gửi bình luận từ Dashboard hoặc scheduler.

## Cấu hình trước khi mở dashboard

- Giữ nguyên khóa Fernet và token hiện có trong .env. Chỉnh GRAPH_API_VERSION thành v26.0 hoặc phiên bản đã xác nhận với app của bạn. File .env cũ có thể đang ghi v19.0.
- Thêm PILOT_PAGE_ID với ID Page đã kết nối Graph API. Đặt PILOT_AUTO_SCAN=0 để chỉ cho phép tác vụ thủ công trong pilot.
- Tạo DASHBOARD_ADMIN_KEY ngẫu nhiên dài tối thiểu 32 ký tự và ghi vào .env. Có thể dùng Python cục bộ với secrets.token_urlsafe(48). Khóa này dùng cho nút Mở khóa Dashboard, không phải token Meta.
- Dùng launcher run.py với đúng một tiến trình backend. Khởi động trong cửa sổ bảo trì sau khi dừng phiên cũ, rồi xác nhận trạng thái Tạm dừng và ID Page pilot trên dashboard.
- Nếu dùng Google Sheet, đồng bộ thủ công và xem kết quả trước khi mở lịch. Bản cập nhật giữ nguyên các Page không xuất hiện trong Sheet. Với cột Bật tự động để trống, trạng thái đang lưu được giữ nguyên.

## Pilot thật

1. Xác nhận token thuộc Page pilot và app có quyền đăng bình luận trên bài của Page.
2. Chọn một bài thử của Page và tạo một bình luận thủ công. Kiểm tra nội dung xem trước và hàng đợi khi hệ thống còn tạm dừng.
3. Khi bạn sẵn sàng đăng, bấm Tiếp tục chạy. Đối chiếu ID, bài, tên Page và nội dung trên Facebook, rồi tạm dừng lại.
4. Nếu có trạng thái Cần kiểm tra, mở bài trên Facebook. Chỉ xác nhận gửi lại sau khi đối chiếu thấy bình luận chưa xuất hiện.
5. Chỉ bật PILOT_AUTO_SCAN=1 sau khi duyệt riêng rule của Page pilot và các tác vụ tự động tồn đọng.

## Xác minh ngoại tuyến

Chạy python verify_pilot_offline.py trong một bản sao mã nguồn dành riêng cho kiểm tra. Kịch bản dùng SQLite tạm và giả lập phản hồi Meta. Bộ test_system.py cũ chứa kỳ vọng đối với fallback CDP, đối chiếu timeout tự động và đồng bộ tắt Page vắng mặt. Các kỳ vọng đó cần được cập nhật trước khi dùng suite cũ làm điều kiện phát hành.
