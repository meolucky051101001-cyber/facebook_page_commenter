# Luồng chọn Graph API và Browser theo từng Page

Cập nhật 17/11/2026, Asia/Bangkok. Người dùng cho phép triển khai source/test của phương án kết hợp. Tài liệu này thay thế quyết định chỉ triển khai Graph ngày 16/11; không cấp quyền khởi chạy dịch vụ, gửi thử hoặc bật AUTO thật.

## Phạm vi dữ liệu và cách gửi

- Giữ DB workspace; đồng bộ từ DB `.gemini` là bước riêng. Danh mục 66 Page = 19 HYBRID + 47 ANTIDETECT do người dùng báo, chưa đối chiếu DB vận hành trong lần triển khai này.
- Chọn `send_mode=GRAPH_API` hoặc `BROWSER` rõ ràng cho từng Page. `connection_mode=HYBRID` chỉ mô tả các kết nối đã liên kết, không tự chọn transport hoặc bật AUTO.
- Graph cần token/quyền/danh tính Page hợp lệ và proof kiểm tra kết nối của chính Page đó; Pilot cũng không được miễn.
- Browser dùng phiên đăng nhập sẵn của tài khoản Facebook trong profile Antidetect riêng. Cookie thuộc phiên tài khoản; các Fanpage do tài khoản đó quản lý được ánh xạ vào profile. Không thêm chức năng đọc, xuất hoặc dán cookie thô.
- Không fallback sang Browser khi Graph hết quyền, rate limit hoặc báo hạn chế. Inbox, ẩn/hiện, trả lời và xuất bản vẫn cần Graph; Browser phục vụ đọc bài và bình luận Page.

## Luồng gửi và dừng

1. Startup lấy khóa độc quyền DB, khôi phục tác vụ chưa rõ, cách ly các profile có thao tác dở và luôn lưu Pause trước khi chạy worker.
2. Khi Pause, không tự quét Facebook, kiểm tra Graph, đồng bộ Sheet hoặc nhận lượt ghi mới. Một thao tác đọc chủ động trên UI không tự Resume/AUTO.
3. Giữ khóa Page, kiểm tra transport, proof, nội dung, quota và cooldown. Browser giữ thêm khóa profile, ghi quota và dấu thao tác trước khi đọc metadata hoặc mở tab.
4. Browser xác minh numeric actor/Page, chủ bài, thời điểm bài và nội dung. Chuẩn bị Sheet một lần, sau đó đọc DOM cuối và kiểm tra policy bằng snapshot DB ngắn; nếu Sheet bận, DB lỗi/chậm hoặc bằng chứng DOM cũ thì hủy trước Enter.
5. Ticket phiên bản global/Page/AUTO bị vô hiệu khi Pause, đổi cấu hình hoặc tắt/bật AUTO. Chốt gửi chỉ kiểm tra cờ và phiên bản trong mutex ngắn, không giữ mutex qua DB, HTTP hoặc CDP.
6. Lệnh Pause thắng trước mốc chốt thì không gửi; sau mốc chốt, lượt đang diễn ra có thể hoàn tất. Chưa rõ kết quả sau POST/Enter giữ NEED_CHECK, không tự gửi lại.
7. Facebook báo checkpoint, đăng nhập lại hoặc hạn chế thì Browser chặn toàn profile. Latch bộ nhớ có hiệu lực trước khi ghi DB; ghi lỗi vẫn giữ nguyên lý do và marker để restart không làm mất chặn.
8. Có ID bình luận đã xác minh thì bảo toàn kết quả kể cả cleanup an toàn lỗi; các lượt Browser sau vẫn bị chặn. Kiểm tra lại chủ động có thể mở chặn đúng generation, nhưng không tự khôi phục Page hoặc hàng đợi cũ.
9. Graph báo hạn chế chính sách được phân loại riêng và dừng Page cùng tác vụ đang chờ. Các giới hạn cấp tài khoản/app tiếp tục dùng cooldown chung; không đổi tài khoản/transport để thử lại.

## Khả năng phục vụ 3 bài mỗi ngày

Mỗi Page có ba dòng chuỗi với Thứ tự 1/2/3; số phút là mốc từ thời điểm đăng bài. Ba bài mới dùng lại cùng chuỗi, tạo chín bình luận/ngày/Page. Bước sau chờ bước trước thành công và giữ khoảng cách thực tế; NEED_CHECK chặn bước tiếp theo.

Quota bình luận Page còn dùng chung với trả lời trong tool. Ngân sách Browser tính thêm check/scan/verify/send của mọi Page chung profile. Giữ mặc định 100 thao tác/profile/ngày và 60 giây giữa thao tác; không tự tăng để đáp ứng số Page. Với polling 240 giây, riêng scan liên tục có thể dùng hết 100 lượt sau khoảng 6 giờ 40 phút. Trước khi chạy cả ngày cần chọn lịch quét/ngân sách phù hợp số Page thực tế. Bài phát hiện muộn hơn MAX_POST_AGE_MIN (mặc định 120 phút) bị bỏ qua; chưa hỗ trợ URL rút gọn/pfbid ở Browser.

## Nghiệm thu và vận hành riêng sau

- Source/test dùng DB tạm, khóa và credential giả, HTTP/CDP mô phỏng; không đọc `.env`, DB vận hành, cookie, token hoặc khóa thật.
- Không đổi DB_PATH, PILOT_PAGE_ID, send_mode hay opt-in thật trong lần triển khai này.
- Sau đồng bộ do người dùng quản lý, chỉ đối chiếu metadata được phép: page_id, page_name, fb_profile_id, connection_mode, token_status. Không dump/export ciphertext hoặc yêu cầu dán token.
- Chỉ sau khi được cho phép mới kiểm tra kết nối một Page, trong trạng thái Pause. Với Browser, người dùng đăng nhập/xác minh trực tiếp trong profile đúng tài khoản.
- Pilot là một Page, bài/nội dung/thời điểm và số lượt được duyệt cụ thể. Xác minh ID bình luận và lưu kết quả trước khi đánh giá mở rộng.
- Khi rollback, giữ nguyên counter/history/NEED_CHECK/marker. Bản Browser cũ không hiểu state v2 phải chặn, không xóa marker để tiếp tục chạy.

Kết quả kiểm thử thực chạy được ghi trong nhật ký triển khai; không coi test giả lập là bằng chứng đủ quyền hoặc sẵn sàng của Page thật.
