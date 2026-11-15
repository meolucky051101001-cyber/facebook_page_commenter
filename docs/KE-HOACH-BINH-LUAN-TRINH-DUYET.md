# Triển khai bình luận bằng profile Antidetect

Mục tiêu: bổ sung cách gửi `BROWSER` cho Page không có token Meta, dùng cùng
tab **Comments - Multi-Page**, lịch gửi, hạn mức và lịch sử chống trùng.
Ngày bắt đầu: 15/11/2026. Không chuyển hoặc bật hàng loạt Page khi chưa xác minh ID.

## Các bước và điều kiện hoàn thành

1. **Dữ liệu và cách gửi.** Nâng schema 6 lên 7; mỗi Page mặc định `GRAPH_API`.
   Lưu trạng thái trình duyệt riêng với trạng thái token. Xác minh trình duyệt
   phải gắn với ID Page và profile hiện tại. Đổi cách gửi tạm dừng tác vụ đang chờ,
   giữ nguyên lịch sử và bộ đếm. Chặn đổi khi đang gửi.
2. **Kết nối trình duyệt.** Mỗi profile chỉ có một thao tác tại một thời điểm.
   Dùng phiên đăng nhập của profile đã cấu hình, mở tab riêng và dọn đúng tab/phiên
   do tác vụ tạo. Phát hiện đăng nhập/checkpoint và báo trạng thái cần xử lý.
   Không gửi chỉ dựa trên tên Page hoặc tên người bình luận.
3. **Quét và gửi.** Quét bài có ID chủ sở hữu và thời gian tuyệt đối xác minh được.
   Kiểm tra đúng Page, bài viết, trạng thái tạm dừng và cấu hình ngay trước Enter.
   Chỉ thành công khi quan sát được ID bình luận mới, nội dung và người đăng đúng.
   Nếu đã Enter nhưng kết quả chưa rõ, chuyển `NEED_CHECK`, không tự gửi lại.
4. **Lịch và điều phối.** Chọn đường gửi theo `send_mode`; lỗi Graph không tự chuyển
   sang trình duyệt. Profile bận được chờ lại. Hạn mức/ngày, khoảng cách gửi,
   chuỗi bình luận, Sheet và chống trùng dùng chung. Inbox/xuất bản vẫn cần Graph.
5. **Dashboard.** Chọn cách gửi từng Page; kiểm tra kết nối theo chế độ; đọc bài,
   tạo tác vụ thủ công và bật tự động theo chế độ đã chọn. Hiển thị đúng trạng thái
   đã kiểm tra, chưa kiểm tra, cần đăng nhập và lỗi xác minh.
6. **Kiểm thử.** Chạy test cô lập, kiểm thử các tình huống đổi chế độ/profile,
   pause sát thời điểm gửi, timeout sau Enter, sai actor, URL sai, profile bận,
   token hỏng và migration giữ nguyên lịch sử. Diễn tập migration trên bản sao DB.
7. **Triển khai.** Sao lưu DB, tạm dừng và đợi tác vụ kết thúc trước khi khởi động
   dịch vụ mới. Kiểm tra API/schema và lịch sử. Giữ cách gửi đang hoạt động của
   Chị Đẹp Thích Vẽ. Chỉ kích hoạt trình duyệt sau khi kiểm tra kết nối thực tế.

## Dữ liệu hiện có cần xử lý riêng

- Danh mục có 65 dòng, bảy nhóm profile; 25 dòng thiếu ID. Không ghép tự động theo tên.
- Nhật Kí Sân Trường (Profile 4) có hai Page trùng tên; Chị Đẹp Handmade chưa có
  tên khớp; Tạp Hóa My Bò chưa có trong danh mục local.
- Đã xử lý hai dòng thiếu phút của Chị Đẹp Thích Vẽ theo lựa chọn mới của người dùng:
  dòng 5/12/13 có thứ tự 1/2/3 và thời gian 30/35/40 phút. Đã lưu Sheet và đồng bộ
  vào tool, không còn lỗi nguồn cho Page này.

## Kết quả triển khai 15/11/2026

Các bước 1–6 đã hoàn thành với 192 kiểm thử đạt, một cảnh báo deprecation của thư viện.
Diễn tập migration trên bản sao giữ nguyên mọi cột cũ và lịch sử, kiểm tra toàn vẹn đạt.
Bước 7 đã triển khai dịch vụ schema 7 tại localhost:8080; cách gửi của 65 Page vẫn là
Graph API cho đến khi từng Page được lựa chọn và xác minh riêng.

Probe thực tế chỉ đọc Profile 2 trả `LOGIN_REQUIRED`. Cần đăng nhập Facebook lại trong
profile này để tiếp tục nghiệm thu giao diện và gửi qua trình duyệt. Chưa có bằng chứng
gửi Browser thật. Hiện chỉ nhận link numeric, chưa nhận `pfbid` hoặc link rút gọn.

Yêu cầu gửi riêng Chị Đẹp Thích Doll đã hoàn thành qua Graph: hai bài hôm nay 15/11
lúc 13:00 và 18:00, mỗi bài ba câu từ Sheet, tổng sáu bình luận đã GET xác minh
đúng nội dung và tác giả. Người dùng xác nhận không lấy bài hôm qua.

## Cách sử dụng sau khi triển khai

Đồng bộ danh mục Antidetect → chọn **Trình duyệt Antidetect** ở Page →
**Kiểm tra trình duyệt** → đồng bộ Sheet và xử lý lỗi dòng → **Bật tự động**.
Nếu Page đã tắt do kết nối cũ, dùng **Kích hoạt kết nối**. Tác vụ cũ đang tạm dừng
cần **Khôi phục tác vụ** riêng. Page mới bật bắt đầu từ thời điểm bật, không quét
ngược toàn bộ lịch sử. Hạn mức tính tổng comment/reply do tool ghi nhận.

Nếu trạng thái là `NEED_CHECK`, mở bài và đối chiếu trước khi xác nhận đã đăng
hoặc xác nhận chưa đăng để thử lại. Giao diện Facebook thiếu bằng chứng về ID
hoặc thời gian sẽ bị chặn và cần bổ sung hỗ trợ giao diện, không đoán thông tin.
