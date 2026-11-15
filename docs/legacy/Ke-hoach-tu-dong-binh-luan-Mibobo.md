# Kế hoạch bình luận tự động cho Page Mibobo

Mục tiêu là quản lý Mibobo, ID `1434778846383977`, trong mục **Bình luận Fanpage** của Antidetect. Antidetect hiển thị dữ liệu Page và hàng đợi. Commenter đọc Sheet riêng và đăng qua Meta Graph API với tư cách Mibobo.

## 1. Chuẩn bị dữ liệu và quyền

- Workbook được gửi có tab **Page** (`gid=1902888278`) để lưu thông tin Page và tab **Comments** (`gid=1405549804`) để lưu mẫu. Các vùng đã đọc không có Mibobo.
- Tạo một **file Google Sheets riêng tư riêng cho Mibobo**. Dùng tab `Comments` và hai cột `ID trang` và `Comment`. Đặt cột A là văn bản thuần. Mỗi dòng dùng đúng ID `1434778846383977`, một bình luận mỗi dòng. Ví dụ để kiểm thử định dạng:

  | ID trang | Comment |
  |---|---|
  | 1434778846383977 | Bạn xem thông tin sản phẩm và cách đặt hàng trong bài nhé. |

- Chỉ cấp quyền xem file mới cho danh tính dịch vụ dùng để đọc Sheet. Cấu hình trên máy chủ theo ID file, tên tab và phạm vi A:B. Quyền Sheets API áp dụng ở cấp file, nên một tab riêng trong workbook chứa token không tạo được ranh giới truy cập tương đương. [Quyền Sheets API](https://developers.google.com/workspace/sheets/api/scopes)
- Thu hồi các token đã để trong workbook có chia sẻ và cấp lại khi cần. Lưu token mới trong backend hoặc kho bí mật cục bộ. File bình luận chỉ chứa dữ liệu để đăng.

**Nghiệm thu:** Mibobo khớp ID của token Graph API, tài khoản đọc chỉ được cấp quyền trên file bình luận, đọc được tiêu đề và ít nhất một mẫu.

## 2. Gắn mục quản lý vào Antidetect

- Menu **Bình luận Fanpage** mở `/commenter/` trong dashboard Antidetect và dùng phiên đăng nhập hiện có.
- Antidetect gọi máy chủ Commenter qua `127.0.0.1:8080`, với khóa quản trị gửi từ máy chủ. Giao diện hiển thị Page pilot, trạng thái và số tác vụ. Giữ kết nối loopback và không đưa khóa vào URL hoặc HTML.
- Cho Commenter dùng cổng 8080 hoặc một cổng còn trống khác. Cấu hình `FACEBOOK_COMMENTER_PORT` của Antidetect tương ứng. Nếu Antidetect đang ở 8080, chọn một cổng riêng. Có thể chạy Commenter từ thư mục repo bằng `python -m uvicorn app:app --host 127.0.0.1 --port 8080 --workers 1`.
- Khi luồng Sheet sẵn sàng, thêm khu vực nguồn mẫu, xem trước và công tắc chạy của riêng Mibobo. Giữ kiểm tra phiên và Origin của Antidetect cho các thao tác ghi.

**Nghiệm thu:** mở mục từ Antidetect, thấy Mibobo và số tác vụ khi Commenter đang chạy. Khi Commenter dừng, giao diện báo mất kết nối. Người chưa đăng nhập không xem được trạng thái.

## 3. Nối Sheet vào rule của Mibobo

1. Đọc một vùng A1:B có giới hạn qua Google Sheets API bằng quyền đọc. Dùng đúng ID file và tab đã cấu hình. [Google Sheets values.get](https://developers.google.com/workspace/sheets/api/reference/rest/v4/spreadsheets.values/get)
2. Kiểm tra đủ tiêu đề, ID lưu nguyên vẹn và nội dung không rỗng. Nếu có dòng thuộc Page khác, dừng nhập và hiển thị số dòng lỗi. Bỏ qua dòng trống và gộp mẫu trùng hoàn toàn.
3. Hiển thị bản xem trước gồm Page, số mẫu, lỗi và thời điểm đọc. Chỉ áp dụng bộ mẫu khi cả lần đọc hợp lệ. Thao tác đồng bộ mẫu không đổi cờ bật hoặc tắt của Page.
4. Nếu file bị đổi tên tab, mất quyền, trống hoặc sai cột, báo lỗi. Giữ bản mẫu cũ để đối chiếu và chặn tạo hoặc gửi tác vụ tự động khi nguồn hiện tại chưa hợp lệ.

**Nghiệm thu:** thay nội dung trên Sheet cập nhật bộ mẫu của Mibobo. Một dòng sai ID hoặc một lần mất quyền đọc không ảnh hưởng Page khác và không tạo tác vụ mới.

## 4. Bật quy tắc tự động có kiểm soát

- Ghi mốc bắt đầu khi người vận hành bật rule. Chỉ xét bài mới của Mibobo xuất bản sau mốc này. Lần bật đầu không xử lý bài cũ.
- Lịch đề xuất cho pilot là đăng sau 10 phút, tối đa một bình luận cho mỗi bài, trần hai lần gửi mỗi ngày. Các thông số cần được chốt trong giao diện trước khi bật.
- Ghi nội dung cuối cùng vào tác vụ. Xác minh lại bài do Mibobo đăng và kiểm tra pause ngay trước khi gửi. Dùng một khóa chống trùng theo Page và bài viết.
- Nếu Meta không trả kết quả chắc chắn, đánh dấu **Cần kiểm tra**. Nếu bị mất quyền hoặc token hết hiệu lực, tạm dừng Page và yêu cầu kết nối lại.

## 5. Nghiệm thu theo ba lượt

1. **Xem trước:** tải Sheet và mô phỏng bài mới. Kiểm tra nội dung, Page, bài đích và giờ dự kiến khi luồng gửi vẫn tạm dừng.
2. **Pilot thật:** tạo một bài thử mới sau mốc bật. Xác nhận đúng một tác vụ, một bình luận xuất hiện dưới tên Mibobo và hàng đợi có ID Meta. Tạm dừng sau khi đối chiếu.
3. **Thử lỗi:** kiểm tra tình huống Sheet rỗng, sai Page ID, token mất hiệu lực, khởi động lại và phản hồi gửi bị gián đoạn. Chỉ chuyển sang tự động thường xuyên sau khi các trường hợp này cho kết quả đúng.
