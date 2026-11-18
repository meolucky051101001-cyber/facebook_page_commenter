# Facebook Page Multi-Account Commenter Hub

Hệ thống quản lý Fanpage đa tài khoản độc lập, định tuyến kịch bản bình luận riêng cho từng Page và tự động lên lịch comment sau một khoảng thời gian ($X$ phút) kể từ khi bài viết mới được đăng.

Cập nhật 17/11/2026: triển khai **chọn Graph API hoặc Browser theo từng Page**. Page có kết nối Graph hợp lệ có thể dùng Graph; Page còn lại có thể dùng phiên đăng nhập sẵn trong profile Antidetect của tài khoản quản lý. Không tự đổi cách gửi khi Graph lỗi. Xem [luồng Graph và Browser](docs/KE-HOACH-VAN-HANH-GRAPH-VA-BROWSER.md).

Đã sửa source, đồng bộ DB và bổ sung test an toàn; **chưa bật AUTO hàng loạt hay khởi chạy dịch vụ thật**. Mỗi lần khởi động đều tạm dừng (`paused`), kể cả DB từng lưu trạng thái running. Maintenance và đồng bộ Sheet tự động không chạy khi pause; kiểm tra kết nối chủ động không mở quyền gửi. DB workspace hiện đã đồng bộ và xác minh giải mã đầy đủ danh mục **66 Page = 19 HYBRID + 47 ANTIDETECT** (giữ nguyên toàn bộ 45 tác vụ lịch sử). Graph và Browser đều chịu chính sách, quyền và giới hạn của Meta.

---

## 🎯 Tính năng nổi bật

1. **Quản lý nhiều tài khoản và Page:**
   - Hỗ trợ dùng chung App Meta; quyền hữu hiệu và điều kiện truy cập phải được xác minh cho từng Page/thao tác.
   - Mỗi Fanpage được lưu trữ và sử dụng `Page Access Token` độc lập.
   - Phân loại lỗi theo phạm vi ảnh hưởng. Giới hạn cấp tài khoản hoặc ứng dụng có thể ảnh hưởng nhiều Page; kế hoạch Graph yêu cầu cooldown/dừng đúng phạm vi, không bảo đảm cô lập mọi hạn chế.

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

Các lệnh dưới đây là hướng dẫn chung. Đợt tích hợp hiện chưa được phép khởi chạy thật; cần nghiệm thu và được duyệt thao tác vận hành trước khi dùng DB hoặc phiên đăng nhập thật.

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

Token lỗi giải mã sẽ được đánh dấu INVALID_SECRET và tác vụ Graph đang chờ của Page đó chuyển PAUSED. Kết nối lại và kiểm tra đúng danh tính Page trước khi khôi phục. Scheduler chọn rõ `GRAPH_API` hoặc `BROWSER` cho từng Page; lỗi Graph không tự chuyển sang trình duyệt. Inbox và xuất bản vẫn cần Graph API.

### Bình luận bằng trình duyệt Antidetect

Browser sử dụng phiên đăng nhập cục bộ của từng tài khoản trong profile Antidetect; không cần nhập hoặc xuất cookie thô. Các Page cùng tài khoản dùng chung profile và ngân sách thao tác. Source đã bổ sung chốt gửi sau DOM cuối, nhận diện thông báo hạn chế và phục hồi dấu thao tác sau gián đoạn. Kiểm thử giả lập chưa thay thế nghiệm thu Facebook thật trên Page được phép; chưa bật AUTO hoặc tự đổi mode trong DB vận hành.

Giao diện Commenter trong Antidetect tại `/commenter/` có danh sách Fanpage lọc theo profile/cách gửi/trạng thái, nguồn Google Sheet, hàng đợi tác vụ và khu vực **Profile & an toàn**. Chi tiết Page hiển thị chuỗi theo thứ tự, giới hạn riêng của Page và hạn mức thao tác chung của profile Browser. Các thao tác cập nhật tại chỗ; tác vụ NEED_CHECK cần người vận hành đối chiếu trước khi gửi lại hoặc đánh dấu đã đăng. Nút Pause vẫn nhận yêu cầu dừng khi kiểm tra kết nối hoặc Resume đang chờ. Tải trang chỉ đọc trạng thái, không tự quét bài, đồng bộ Sheet hoặc thay đổi trạng thái chạy. Proxy chỉ trả các trường dashboard cần thiết, không chuyển token/cookie ra giao diện. Bản `integration/antidetect` là source workspace, chưa áp dụng vào thư mục Antidetect cài đặt bên ngoài.

Trong **Profile & an toàn**, chốt an toàn chung có ba trạng thái: `READY`, `WAITING` (khoảng cách, hạn mức hoặc thao tác đang chạy) và `BLOCKED` (checkpoint, đăng nhập lại, hạn chế, metadata hỏng hoặc cần khôi phục). Số liệu chưa đọc được hiển thị `—`; không suy thành 0. Chặn profile chỉ áp dụng các Page chọn Browser cùng profile; Page Graph không bị đổi transport. Một Page kiểm tra thành công không xác minh các Page còn lại.

**Chạy preflight** chỉ đọc metadata đã lưu: nguồn Sheet, proof của từng Page, chốt profile, quota/cooldown và tổng số task trên toàn lịch sử, kể cả NEED_CHECK cũ hơn 500 task. Số “Page AUTO đã chuẩn bị” chỉ tính Page đã bật AUTO và không có điều kiện chặn/chờ tại lúc đọc; không phải quyền gửi hoặc kết quả kiểm tra Facebook trực tiếp. Pause vẫn giữ nguyên. Tác vụ NEED_CHECK giữ quy trình đối chiếu riêng và chặn các bước sau trong chuỗi của nó. Thời gian trên giao diện dùng UTC+7. Sau đổi cấu hình hoặc tải trạng thái mới, chạy lại preflight để có snapshot mới.

Có thể kiểm tra giao diện với dữ liệu giả bằng `python verify_commenter_ui.py` khi máy đã có Playwright/Chromium. Script tạo browser mới, intercept mọi request, không khởi động ứng dụng hoặc dùng profile Facebook; xuất ảnh desktop/mobile vào `.pytest_cache/commenter-ui/`.

AUTO theo dõi bài đăng mới từ lúc bật cho từng Page. Mốc bài đã thấy và hàng đợi được lưu cùng nhau trong DB, tiếp tục dùng sau khi khởi động lại. Bật lại sau khi tắt AUTO tạo mốc bắt đầu mới. Tool vẫn kiểm tra danh sách bài định kỳ để phát hiện bài mới, chỉ mở bài cần gửi khi tác vụ đến hạn. Bài phát hiện muộn hơn `MAX_POST_AGE_MIN` (mặc định 120 phút) được bỏ qua. Với Browser, bước xác minh bài và gửi bình luận dùng cùng một tab, có kiểm tra lại danh tính Page và nội dung trước khi gửi.

Quy trình tham khảo sau khi hoàn tất sửa, nghiệm thu và được phép mở lại Browser: đồng bộ danh mục Antidetect, chọn **Trình duyệt Antidetect** ở Page, bấm **Kiểm tra trình duyệt**, đồng bộ Sheet rồi **Bật tự động** trong phạm vi được duyệt. Nếu profile hết phiên, đăng nhập Facebook lại trong đúng profile rồi kiểm tra lại. Chế độ này dùng phiên trình duyệt, không yêu cầu Page Access Token.

Tool xác minh numeric ID Page, bài viết, người bình luận và nội dung ngay trước Enter. Tên Page giống nhau không đủ để xác minh. Mỗi profile chỉ xử lý một thao tác cùng lúc. Tool chỉ đóng phiên browser có `launch_id` do nó sở hữu. Đổi cách gửi tạm dừng tác vụ đang chờ; cần **Khôi phục tác vụ** riêng sau khi kiểm tra, quota và lịch sử vẫn giữ.

Mọi lượt kiểm tra kết nối, quét bài, xác minh bài nhập tay hoặc mở bài để gửi đều dùng chung ngân sách của profile: `BROWSER_MAX_OPERATIONS_PER_PROFILE_PER_DAY=100`, cách nhau ít nhất `BROWSER_MIN_GAP_PER_PROFILE_SEC=60` giây theo mặc định. Vòng quét nền chỉ quét các Page Browser theo chu kỳ `BROWSER_SCAN_INTERVAL_SEC=900` giây (15 phút), nhường lượt khi cùng profile có tác vụ gửi đến hạn, và giữ lại `BROWSER_RESERVED_SEND_OPERATIONS=25` lượt cuối trong ngày ưu tiên cho `send`/`check` để lượt quét không tiêu cạn toàn bộ hạn mức ngày. Lượt đã giữ vẫn được tính khi thao tác thất bại; tranh chấp khóa hoặc lượt bị chặn không tính thêm. Bộ đếm theo ngày UTC+7, lưu qua restart và đổi Page. Scanner luân phiên các Page cùng profile theo lần thử quét gần nhất, kể cả khi không có bài mới; thứ tự này cũng giữ qua restart. Hạn mức bình luận từng Page vẫn áp dụng riêng. Đây là giới hạn tải của tool, không phải ngưỡng Facebook bảo đảm không checkpoint; thao tác ngoài tool không được tính.

Khi thấy đăng nhập lại, checkpoint hoặc thông báo hạn chế hoạt động được hỗ trợ, tool chặn toàn profile, làm mất hiệu lực kiểm tra Browser của các Page cùng profile và chuyển tác vụ PENDING sang PAUSED. Nếu đã nhấn Enter mà chưa xác nhận được kết quả, tác vụ giữ NEED_CHECK, không tự gửi lại. Khóa chặn tồn tại qua restart. Sau khi tự xử lý trên Facebook, dùng **Kiểm tra trình duyệt** để kiểm tra lại (vẫn chịu hạn mức/khoảng cách), rồi kiểm tra từng Page và **Khôi phục tác vụ** riêng. Kiểm tra thành công không tự khôi phục hàng đợi cũ. Các Page ở profile khác và luồng Graph không bị khóa theo profile này.

Trước khi bật AUTO Browser, chạy `python -m pytest -q`: test dùng DB tạm, dữ liệu giả, vô hiệu hóa đọc/ghi `.env` trước khi import cấu hình và chặn HTTP thật. Bộ test kiểm tra hạn mức dùng chung, đồng thời/restart, dừng trước Enter, kết quả chưa rõ sau Enter, đổi danh tính/nội dung/cấu hình và không gửi lại. Kiểm thử mô phỏng chưa thay thế nghiệm thu layout Facebook thật: phải xác minh đúng numeric actor, chủ bài, timestamp tuyệt đối và ID bình luận trên một Page được phép trước khi mở rộng. Không bật AUTO nếu các bằng chứng này chưa đủ.

Khi đổi cách gửi, lịch AUTO còn đối chiếu permalink trong cùng Page để tránh tạo lại chuỗi nếu Graph ID khác Reel ID. Chuỗi cũ giữ nguyên nội dung và thời gian. Việc đối chiếu link không thay thế bước xác minh bài và người đăng trước khi gửi.

Chỉ ghi SUCCESS khi thấy ID bình luận mới đúng Page/nội dung. Kết quả không rõ sau Enter chuyển NEED_CHECK và không tự gửi lại. Bộ trích xuất DOM hỗ trợ cả URL số và `pfbid...` khi đối chiếu được Relay JSON / React Fiber đúng chủ bài viết, timestamp tuyệt đối và numeric actor ID; các liên kết rút gọn (`fb.watch`, `/share/...`) hoặc layout thiếu bằng chứng định danh vẫn bị chặn an toàn. Cần nghiệm thu layout trên phiên Facebook đã đăng nhập trước khi bật nhiều Page. Xem `docs/KE-HOACH-BINH-LUAN-TRINH-DUYET.md`.

Ứng dụng khóa theo đường dẫn DB trước khi recovery, kể cả chạy qua `run.py`, `run_background.pyw` hoặc Uvicorn trực tiếp. Chỉ một tiến trình được dùng DB. **Startup luôn lưu trạng thái tạm dừng** và cách ly profile có thao tác dở trước khi khởi động worker. Pause dừng nhận lượt gửi mới; lượt đã chốt gửi có thể đang diễn ra và kết quả chưa rõ giữ NEED_CHECK. Để cập nhật dịch vụ, tạm dừng trước, sao lưu bằng SQLite Backup API và kiểm tra bản sao. Nếu đã gửi sau nâng cấp, phải giữ lịch sử gửi mới khi rollback để tránh mất khóa chống trùng.

---

## Kết nối tài khoản Facebook

1. Cấu hình `META_APP_ID`, `META_APP_SECRET` và `META_OAUTH_REDIRECT_URI` trong `.env`. URI phải kết thúc bằng `/facebook/callback`, ví dụ `http://localhost:8080/facebook/callback` nếu Meta chấp nhận URI đó cho app của bạn.
2. Đăng ký cùng URI trong cài đặt Facebook Login của Meta App. Mở dashboard bằng đúng host và cổng trong URI.
3. Bấm **Kết nối Facebook** trong **Quản lý Page & Nick** rồi cấp quyền cho các Page bạn quản lý. Chọn thêm quyền đọc bình luận khách hoặc xuất bản nếu dùng các mục đó.
4. Kiểm tra kết nối của từng Page trước khi bật lịch. Nếu Meta không cho phép tài khoản cấp quyền, kiểm tra chế độ app, vai trò của tài khoản và quyền được duyệt trong Meta App Dashboard.

Tool lấy Page Access Token qua danh sách do Meta trả về. Dashboard cũng nhận một User Access Token có sẵn để nhập danh sách Page của tài khoản đó.
