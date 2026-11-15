Kế hoạch xử lý 5 lỗi trong chế độ bình luận Facebook tự động

Ngày lập: 15/11/2026, múi giờ Asia/Bangkok. Tiến độ thực hiện được ghi riêng trong NHAT-KY-TRIEN-KHAI-COMMENT-TU-DONG.md; tài liệu này giữ các yêu cầu để đối chiếu nghiệm thu.

**1. Mục tiêu và căn cứ**

Kế hoạch dựa trên 5 tình huống đã tái hiện bằng SQLite tạm và phản hồi Meta giả lập:

| Mã | Mức ưu tiên | Lỗi đã xác nhận | Điều kiện hoàn thành |
| --- | --- | --- | --- |
| F1 | P1 | 10 tác vụ của Page lỗi giải mã token chiếm toàn bộ lượt lấy hàng đợi | Page lỗi được cô lập trong DB; tác vụ của Page khỏe tiếp tục được xử lý |
| F2 | P1 | Trả lời tự động trong hộp thư vượt hạn mức ngày và khoảng cách gửi | Mọi bình luận, trả lời do tool gửi dùng chung hạn mức và cơ chế chống gửi đồng thời trên một Page |
| F3 | P2 | Bình luận đơn gửi nguyên Spintax và biến tên Page | Nội dung thực tế đã được xử lý trước khi gửi và ổn định qua các lần đồng bộ/retry |
| F4 | P2 | Meta báo rate limit nhưng các tác vụ tiếp theo của cùng Page vẫn gửi | Page bị giới hạn phải chờ tới mốc lưu trong DB, kể cả sau khởi động lại |
| F5 | P2 | Khôi phục tác vụ PAUSED có thể gửi nội dung Sheet cũ | Bình luận đơn được cập nhật theo cấu hình hợp lệ trước khi trở lại hàng đợi |

Kết quả kiểm thử của lượt review: 11/11 test multi-Page đạt; tổng hai bộ test có 35 đạt, 17 lỗi. Các test lỗi cần phân loại và cập nhật theo hành vi hiện tại. Không coi tất cả 17 lỗi là lỗi vận hành mới, cũng không bỏ qua toàn bộ bộ test cũ.

Ảnh chụp trạng thái ở lượt review: schema SQLite phiên bản 5; 65 Page đều tắt tự động; trạng thái toàn hệ thống là paused; localhost 8080 không kết nối được. Đây là thông tin tại thời điểm kiểm tra, phải đọc lại khi triển khai.

**2. Quy tắc hành vi dùng để thiết kế và nghiệm thu**

- Hạn mức ngày dùng chung cho bình luận bài viết và trả lời bình luận do tool xác nhận gửi thành công, bao gồm thao tác thủ công qua tool. Bộ đếm tuân theo UTC+7 đang cấu hình. Bình luận gửi từ ứng dụng Facebook bên ngoài tool nằm ngoài bộ đếm hiện tại.
- Khoảng cách gửi tối thiểu áp dụng theo Page cho mọi luồng gửi chữ. Giữ giá trị cấu hình hiện tại; mặc định trong mã là 45 giây. Ẩn/hiện bình luận không tiêu thụ hạn mức bình luận.
- Một Page chỉ có một lượt gửi bình luận đang thực hiện. Page khác có thể tiếp tục khi một Page đang chờ, lỗi token hoặc bị giới hạn.
- Tạm dừng toàn hệ thống chặn việc bắt đầu gửi bình luận mới. Một yêu cầu đã tới Meta vẫn phải ghi nhận kết quả hoặc chuyển sang NEED_CHECK.
- Bình luận đơn chưa gửi sử dụng cấu hình Sheet hợp lệ mới nhất đã được áp dụng vào DB. Mốc chốt nội dung là lúc tác vụ được nhận để gửi; sửa Sheet sau mốc đó áp dụng cho các tác vụ còn chờ.
- Một nội dung Spintax đã được tạo và lưu sẽ được sử dụng lại khi cấu hình không đổi. Đồng bộ định kỳ, restart và retry không tự quay lại câu chữ.
- Chuỗi đã tạo giữ nội dung, số bước và khoảng cách đã lưu. Việc đổi chế độ giữa bình luận đơn và chuỗi áp dụng cho bài mới; không tự biến tác vụ cũ thành chuỗi mới.
- SENDING, SUCCESS và NEED_CHECK giữ nội dung đã gắn với lần gửi. NEED_CHECK cần quyết định đối chiếu rõ ràng trước khi được gửi lại.
- Việc sửa lỗi và nâng schema không tự bật Page hoặc tự khôi phục hàng loạt tác vụ tạm dừng.

**3. Chuẩn bị điểm khôi phục và môi trường kiểm thử**

Ghi lại trạng thái Git gồm thay đổi đang có và tệp chưa được Git theo dõi. Tạo bản chụp mã nguồn hiện tại có thể khôi phục được, bao gồm operations.py, operations.html và phần integration. Dùng trạng thái đang làm việc làm nền vì nó đã chứa các thay đổi multi-Page và chuỗi bình luận.

Khi bắt đầu triển khai thật, đọc lại trạng thái tiến trình, cổng và run state. Sao lưu SQLite bằng SQLite Backup API, chạy quick_check trên bản sao và ghi nhận số Page, rule, task theo trạng thái cùng các mốc quét. Khóa mã hóa phải còn sử dụng được với bản sao; không ghi bí mật vào báo cáo hoặc Git.

Tạo bộ fixture dùng chung trong conftest.py: DB tạm, khóa Fernet thử nghiệm, đồng hồ điều khiển được, HTTP giả lập và trạng thái scheduler được khôi phục sau mỗi test. Chặn HTTP thật ở lớp transport; vẫn cho phép cơ chế loopback nội bộ mà asyncio trên Windows cần. Không import ứng dụng theo cách khiến lifespan làm việc với DB vận hành.

Chuyển 5 kịch bản tái hiện thành test hồi quy trước khi sửa. Các test phải kiểm tra nội dung và số lần gọi Meta, trạng thái DB và tiến trình của Page khỏe; không chỉ kiểm tra giá trị trả về của helper.

**4. Thay đổi cấu trúc dữ liệu và trách nhiệm mã nguồn**

Nâng schema từ 5 lên 6 bằng một migration có transaction và dữ liệu mặc định tương thích. Dự kiến thêm các trường sau:

| Bảng | Trường | Mục đích |
| --- | --- | --- |
| pages | send_cooldown_until, nullable | Mốc sớm nhất được bắt đầu gửi lại sau rate limit, lưu UTC |
| pages | send_backoff_attempts, mặc định 0 | Số lần rate limit liên tiếp để tăng khoảng chờ theo Page |
| page_rules | content_revision, mặc định rỗng | Nhận diện phiên bản các đầu vào ảnh hưởng tới nội dung/lịch |
| tasks | template_source, nullable | Mẫu gốc được chọn để tạo bình luận tự động |
| tasks | content_revision, nullable | Phiên bản đã dùng để tạo nội dung đang lưu |

content_revision chỉ thay đổi khi dữ liệu có ý nghĩa thay đổi: danh sách mẫu có thứ tự, thời gian chờ, chế độ/chuỗi và tên Page được dùng để điền biến. Không dùng thời gian đồng bộ làm phiên bản, vì Sheet không đổi vẫn được đọc lại thường xuyên.

Tác vụ cũ có metadata rỗng được xử lý theo trạng thái: tác vụ đơn AUTO ở PENDING/PAUSED được bổ sung metadata khi đồng bộ hợp lệ; các bản ghi lịch sử hoặc cần đối chiếu giữ nguyên. Chuỗi cũ dùng snapshot đã lưu.

Tạo module comment_dispatch.py làm nơi điều phối kiểm tra trước gửi, khóa theo Page và phân loại kết quả gửi. Tạo comment_content.py để chọn mẫu, điền biến, xử lý Spintax và tính phiên bản nội dung. database.py sở hữu các cập nhật nguyên tử và bộ đếm; các module mới không import scheduler hoặc operations để tránh vòng phụ thuộc.

Giữ mô hình một tiến trình vận hành của ứng dụng. Khóa theo Page bảo vệ các thread trong tiến trình đó; quy trình khởi động cần chặn tiến trình thứ hai trước khi chạy lifespan và recovery trên cùng DB. Kiểm tra cổng đơn thuần chưa đủ nếu hai tiến trình dùng hai cổng khác nhau.

Khóa tiến trình gắn với đường dẫn DB đã chuẩn hóa, dùng cơ chế khóa được hệ điều hành giải phóng khi tiến trình kết thúc. Đặt bước lấy khóa ở đầu lifespan, trước init_db, để cả run.py, run_background.pyw và cách chạy trực tiếp bằng Uvicorn đều được bảo vệ. Kiểm thử việc khởi động lại sau khi tiến trình bị dừng đột ngột để tránh khóa tồn đọng.

**5. Sửa F1 — cô lập Page lỗi giải mã token**

Vị trí chính: database.py:get_page_with_decrypted_token, update_page_status, get_due_tasks; scheduler.py:_execute_due_tasks; thông tin Page trên dashboard.

Thêm thao tác DB chuyên biệt để đánh dấu token không giải mã được. Thao tác này ghi token_status=INVALID_SECRET, ghi lỗi đã loại bí mật và chuyển các tác vụ PENDING của Page sang PAUSED trong một transaction. Giữ nguyên dữ liệu token mã hóa để có thể phục hồi bằng khóa đúng hoặc kết nối lại.

Chỉ cập nhật nếu token mã hóa trong DB vẫn giống token vừa giải mã thất bại. Điều kiện này ngăn một lượt kiểm tra cũ đánh dấu hỏng token mới vừa được kết nối lại.

Gọi thao tác cô lập ở các đường chuẩn bị chạy tự động. Hàm đọc dữ liệu để hiển thị vẫn có thể trả lỗi rõ ràng mà không tự sửa DB. Truy vấn hàng đợi loại Page đã bị đánh dấu khỏi danh sách đủ điều kiện.

Trong executor, sau khi loại một nhóm tác vụ của Page hỏng, tiếp tục lấy ứng viên chưa xét trong ngân sách lượt chạy. Đặt giới hạn số ứng viên/đợt lấy để không tạo vòng lặp vô hạn. Page khỏe sau 10 tác vụ hỏng cần được xử lý ngay trong lượt đó hoặc theo giới hạn hữu hạn được kiểm chứng, không bị lặp mãi ở đầu hàng đợi.

Sau khi token được sửa và xác minh đúng Page, người vận hành có thể khôi phục tác vụ qua đường xử lý F5. Việc token trở lại OK không tự bật quyền tự động.

Nghiệm thu: 10 tác vụ cũ của Page A hỏng token chuyển PAUSED; tác vụ Page B gửi được; không gọi Meta với token hỏng; DB và dashboard cùng hiện INVALID_SECRET; kết nối lại đồng thời không bị ghi đè bởi lỗi của token cũ.

**6. Sửa F2 — kiểm soát chung việc gửi bình luận và trả lời**

Vị trí chính: comment_dispatch.py; scheduler.py:_execute_due_tasks; operations.py:apply_moderation_rules và moderate; các hàm cập nhật kết quả trong database.py.

Luồng chung: lấy khóa gửi của Page, đọc lại trạng thái mới, kiểm tra tạm dừng/kết nối/hạn mức/khoảng cách/cooldown, nhận tác vụ hoặc đánh dấu lượt trả lời, gọi Meta, rồi ghi kết quả cùng bộ đếm trước khi nhả khóa. Chỉ giữ transaction SQLite trong các bước đọc/ghi ngắn; không giữ transaction trong lúc chờ mạng.

Định nghĩa kết quả chờ có cấu trúc: lý do, mốc được thử tiếp và thông báo. Thiếu hạn mức hoặc chưa đủ khoảng cách là trạng thái chờ, không phải lỗi gửi Facebook.

Đối với bình luận bài viết, executor dời lịch theo mốc cần chờ. Đối với phản hồi tự động trong hộp thư, giữ bình luận khách ở trạng thái chưa trả lời, kết thúc lượt trả lời của Page đang bị chặn và tiếp tục Page khác. Vòng hộp thư sau sẽ thử tiếp, có kiểm tra lại quy tắc và quyền bật tự động. Không đặt action_pending='reply' khi chưa thực sự nhận lượt gửi.

Phản hồi thủ công trong hộp thư cũng đi qua cơ chế chung. Nếu bị chặn, API trả lý do và thời điểm thử tiếp; UI giữ nội dung người dùng đang soạn và hiển thị thông báo thay vì báo thành công. Ẩn/hiện bình luận giữ cách đếm riêng, nhưng không được tạo đường vòng để gửi phản hồi.

Tách helper cập nhật bộ đếm để dùng trong cùng transaction với việc ghi SUCCESS/reply_id. Chỉ tăng bộ đếm khi bản ghi thực sự chuyển sang thành công lần đầu. Áp dụng cùng nguyên tắc khi người dùng xác nhận một tác vụ NEED_CHECK đã đăng.

Quy định thứ tự khóa và kiểm tra deadlock: không giữ khóa Sheet/hộp thư trong lúc chờ khóa gửi Page theo chiều ngược lại. Các đường kiểm tra và nhận lượt gửi đều phải đọc lại trạng thái sau khi có khóa.

Nghiệm thu: hết hạn mức thì không có lời gọi POST mới; một lần gửi thành công làm các luồng còn lại của Page phải chờ đủ khoảng cách; chạy đồng thời bình luận bài và phản hồi hộp thư không vượt phần hạn mức cuối; Page khác vẫn tiến hành; xử lý lại cùng kết quả không cộng bộ đếm hai lần.

**7. Sửa F4 — rate limit có hiệu lực theo Page và tồn tại qua restart**

Vị trí chính: comment_dispatch.py, meta_client.py, scheduler.py, operations.py và truy vấn nhận tác vụ trong database.py.

Ngay khi một lượt gửi hoặc bước xác minh trước gửi bị Meta báo rate limit, lưu send_cooldown_until và tăng send_backoff_attempts của Page trước khi xét tác vụ tiếp theo. Tác vụ chưa được thử không tăng retry_count chỉ vì phải chờ Page.

Giữ nhịp lùi hiện có 2, 4, 8, 16 phút; những lần liên tiếp sau dùng mức trần được cấu hình, đề xuất 30 phút. Nếu Meta có Retry-After hợp lệ, chọn mốc muộn hơn giữa chính sách của tool và mốc Meta yêu cầu. Dùng thời điểm nhận lỗi để tính, không dùng thời điểm bắt đầu cả batch.

Luôn áp dụng cooldown của Page kể cả khi tác vụ vừa lỗi đã hết số lần retry và chuyển FAILED. Việc hết retry của một tác vụ không cho phép tác vụ khác gửi ngay. Chỉ reset chuỗi backoff khi có lần gửi thành công được xác nhận; đồng bộ Sheet, bấm retry hoặc restart không xóa cooldown.

Kiểm tra cooldown tại cả truy vấn lấy việc và bước kiểm tra cuối trước gửi, vì batch có thể đã được lấy trước khi cooldown xuất hiện. Phản hồi hộp thư sử dụng cùng mốc chờ. Nếu metadata xác định lỗi giới hạn ở cấp ứng dụng, phạm vi chặn phải mở rộng đúng cấp đó; không suy diễn mọi rate limit đều chỉ thuộc một Page.

Nghiệm thu: ba tác vụ cùng Page gặp lỗi ngay tác vụ đầu thì chỉ có một lần thử gửi trong lượt; Page B vẫn được gửi khi giới hạn chỉ thuộc A; khởi động lại giữa cooldown vẫn chặn A; tới hạn mới có lượt thử tiếp; sửa Sheet và retry không rút ngắn mốc chờ.

**8. Sửa F3 — tạo nội dung Spintax một lần và lưu ổn định**

Vị trí chính: comment_content.py; scheduler.py:build_post_schedule; sheets_sync.py:pilot_comment_for_post và sync_comment_sheet_rows; spintax.py.

Giữ cách chọn mẫu ổn định theo bài đang có. Sau khi chọn mẫu, xử lý Spintax và điền tên Page bằng một helper dùng chung cho bình luận đơn và từng bước chuỗi. Giá trị tên Page phải được coi là dữ liệu để không vô tình xử lý dấu ngoặc/ký tự trong tên thành cú pháp Spintax.

Lưu template_source, content_revision và comment_content cuối cùng. Khi đồng bộ cùng phiên bản, giữ nguyên comment_content; chỉ tạo lại khi đầu vào liên quan thay đổi và tác vụ còn được phép cập nhật. Retry do lỗi mạng trước truyền hoặc rate limit dùng lại nội dung đã lưu nếu cấu hình không đổi.

Kiểm tra nội dung sau khi render: không rỗng sau trim, không vượt giới hạn, không còn biến chưa hỗ trợ. Khi mẫu có lỗi, cô lập cấu hình Page và đưa ra thông báo cụ thể; không để một mẫu hỏng làm dừng các Page khác. Không cam kết rằng Spintax tạo ra câu duy nhất cho mọi bài, vì tập biến thể là hữu hạn.

Nghiệm thu: mẫu '{Hello|Hi} {page_name}' cho ra một câu hợp lệ chứa tên Page; hai lần đồng bộ không đổi Sheet giữ cùng câu; restart/retry giữ cùng câu; mẫu lồng nhau hoạt động; nhánh rỗng, biến không hỗ trợ và nội dung quá dài được phát hiện; Page khác không bị ảnh hưởng.

**9. Sửa F5 — đồng bộ tác vụ tạm dừng và kiểm tra lại khi khôi phục**

Vị trí chính: sheets_sync.py:sync_comment_sheet_rows; database.py:retry_task và resume_paused_tasks_for_page; app.py:retry_task và resume_page_tasks; bước nhận tác vụ trong scheduler.py.

Cho phép cập nhật nội dung của tác vụ đơn AUTO ở cả PENDING và PAUSED khi Sheet hợp lệ. Cập nhật nội dung không đổi PAUSED thành PENDING. Dùng helper của F3 để tránh quay lại Spintax mỗi lần đọc Sheet.

Hai API khôi phục một tác vụ và khôi phục cả Page phải gọi chung một dịch vụ chuẩn bị khôi phục. Với tác vụ đơn AUTO, dịch vụ này buộc đọc lại Sheet, kiểm tra cấu hình của đúng Page, cập nhật nội dung/lịch/phiên bản và chỉ sau đó mới chuyển sang PENDING. Sheet lỗi toàn cục hoặc Page lỗi cấu hình thì trả thông báo và giữ tác vụ tạm dừng. Các tác vụ thủ công không phụ thuộc mẫu Sheet.

Kiểm tra content_revision lần cuối khi nhận tác vụ gửi. Nếu phiên bản của tác vụ đơn không khớp cấu hình hiện hành, cập nhật trong transaction hoặc đưa về bước chuẩn bị; tuyệt đối không lấy comment_content từ danh sách batch cũ rồi gửi sau khi cấu hình đã đổi.

Tính lại lịch từ post_created_time và delay mới, đồng thời giữ các ràng buộc chờ do retry, cooldown, khoảng cách gửi và hạn mức ngày. Không đặt tất cả tác vụ chạy ngay chỉ vì bấm khôi phục. Tác vụ quá hạn vẫn đi qua quy tắc xử lý quá hạn; không âm thầm đưa bài cũ thành bài mới.

Chuỗi đã lưu giữ snapshot. Với NEED_CHECK, thao tác xác nhận đã đăng giữ nội dung cũ; chỉ sau xác nhận chưa đăng mới được chuẩn bị một lần gửi lại. Đổi chế độ Sheet không bổ sung bước mới vào bài đã có lịch.

Nghiệm thu: trong cửa sổ cache 30 giây, Sheet đổi giá 100 thành 200 trong lúc tác vụ PAUSED thì khôi phục chỉ gửi 200; lỗi Sheet không làm tác vụ chạy; cả khôi phục đơn và hàng loạt có cùng kết quả; sửa nội dung trong lúc verify bài không dẫn tới việc gửi dữ liệu từ batch cũ; chuỗi cũ vẫn đúng số bước và thứ tự.

**10. Dashboard, lỗi vận hành và tài liệu**

Dashboard Page cần hiển thị đúng INVALID_SECRET, hạn mức đã dùng/còn lại, nguyên nhân đang chờ và thời điểm có thể gửi lại theo UTC+7. API và UI phải phân biệt đang chờ với lỗi cần can thiệp.

Hộp thư cần hiển thị phản hồi chưa gửi do hạn mức/khoảng cách/rate limit và giữ nội dung thao tác thủ công khi API từ chối gửi sớm. Tác vụ bình luận đơn cần cho người vận hành xem nội dung thực tế đã render, thời điểm đồng bộ cấu hình và lý do chờ; tên trường kỹ thuật chỉ xuất hiện nếu phục vụ chẩn đoán.

Cập nhật README, phần cấu hình môi trường và tài liệu bàn giao: Graph API là đường gửi tự động hiện tại; hạn mức chung; ý nghĩa cooldown; quy tắc snapshot của chuỗi; cách khôi phục tác vụ; hành vi khởi động thực tế. Bản integration/antidetect trong repo được cập nhật khi giao diện/API liên quan thay đổi. Việc áp dụng sang ứng dụng Antidetect đang cài phải kiểm tra riêng đúng vị trí triển khai.

**11. Ma trận kiểm thử bắt buộc**

| Nhóm | Tình huống | Kết quả phải quan sát được |
| --- | --- | --- |
| F1 | 10 task Page A hỏng token đứng trước Page B | A được lưu INVALID_SECRET, task A PAUSED, B có tiến triển |
| F1 | Token mới được lưu trong lúc kiểm tra token cũ thất bại | Token mới không bị đánh dấu lỗi bởi kết quả cũ |
| F1 | Nhiều Page cùng lỗi, Page cuối khỏe | Không bị kẹt ở nhóm ứng viên đầu; có giới hạn số lượt xử lý |
| F2 | Bộ đếm đã đạt giới hạn ngày | Bình luận bài và reply đều không POST |
| F2 | Còn một lượt, hai luồng cùng gửi trên một Page | Chỉ một lượt được gửi; bộ đếm tăng một |
| F2 | Đã gửi, chưa đủ 45 giây mặc định | Luồng còn lại của Page chờ; Page khác chạy |
| F2 | Thành công được ghi nhận lại hoặc xác nhận thủ công hai lần | Không cộng đôi bộ đếm |
| F2 | Qua nửa đêm UTC+7 | Ngày mới được tính đúng; khoảng cách gửi vẫn có hiệu lực |
| F4 | Ba task cùng Page, lần đầu rate limit | Không thử hai task còn lại trong cooldown |
| F4 | Hết số retry của task hoặc restart | Cooldown Page còn hiệu lực |
| F4 | Retry-After, Sheet sync, bấm retry | Mốc chờ không bị rút ngắn |
| F3 | Bình luận đơn có Spintax, tên Page, cú pháp lồng | Gửi nội dung đã render hợp lệ |
| F3 | Đồng bộ lặp, restart, retry cùng cấu hình | Nội dung đã render ổn định |
| F3 | Mẫu rỗng sau render, quá dài hoặc biến lạ | Không gửi nội dung lỗi; lỗi được giới hạn đúng Page |
| F5 | PAUSED, đổi Sheet, resume trong 30 giây cache | Gửi nội dung mới đúng phiên bản |
| F5 | Sheet không đọc được hoặc Page mất mẫu | Tác vụ giữ trạng thái chờ/tạm dừng phù hợp |
| F5 | Resume một task và resume cả Page | Cùng áp dụng kiểm tra và cập nhật nội dung |
| F5 | Đổi Sheet trong lúc xác minh bài/nhận tác vụ | Nội dung gửi đúng mốc chốt cấu hình đã quy định |
| Chuỗi | Bước trước NEED_CHECK hoặc bị hủy | Bước sau không vượt qua điều kiện thứ tự |
| Chống trùng | Timeout sau khi gửi, crash ở SENDING | NEED_CHECK, không tự POST lại |
| Migration | DB v5 có đủ các trạng thái và chuỗi | Giữ lịch sử, mốc quét, quyền bật/tắt; metadata mới hợp lệ |
| Migration | Chạy lại migration, mô phỏng gián đoạn | Không thêm cột trùng hoặc để schema ở trạng thái dở dang |
| API/UI | Host và khóa quản trị hợp lệ/không hợp lệ | Xác thực và thông báo trạng thái hoạt động đúng |
| Đồng thời | Pause, reconnect, Sheet sync và gửi chạy xen kẽ | Không deadlock, không cộng đôi, không dùng trạng thái đã cũ |
| Khởi động | Hai tiến trình cùng trỏ một DB | Tiến trình sau không chạy recovery hoặc vòng gửi trên DB đang được dùng |

Test thời gian dùng đồng hồ giả; test đồng thời dùng barrier/event, không dựa vào sleep tùy ý. HTTP giả lập phải kiểm tra cả endpoint, Page/token giả và nội dung dự kiến. Test HTTP của FastAPI dùng localhost cùng header quản trị thử nghiệm, để không thất bại chỉ do TrustedHostMiddleware.

**12. Xử lý 17 test cũ đang lỗi**

Lập bảng từng test, hành vi nó muốn bảo vệ, nguyên nhân thất bại và hành động sửa. Cập nhật fixture cho các điều kiện mới: Page mặc định tắt, kết nối phải được xác minh, nguồn Sheet hợp lệ và cấu hình xác thực dashboard.

Các test mong đợi timeout tự thành SUCCESS cần đổi sang kiểm tra NEED_CHECK và yêu cầu đối chiếu. Các test mong đợi tự ghép Page theo tên hoặc tự chuyển sang CDP cần được đối chiếu với đường Graph API hiện tại. Giữ test đơn vị của CDP nếu module còn được hỗ trợ, đồng thời không dùng các test đó làm bằng chứng rằng scheduler hiện có fallback CDP.

Các bảo vệ thực tế vẫn phải có test chạy: chống gửi trùng, token đúng Page, pause giữa batch, giới hạn ngày, khoảng cách gửi, cập nhật Sheet và thứ tự chuỗi. Đích cuối là toàn bộ test áp dụng cho sản phẩm hiện tại đều đạt; không chỉ chạy riêng test_multi_page.py và không đánh dấu bỏ qua hàng loạt để có kết quả xanh.

**13. Thứ tự thực hiện và đầu ra từng chặng**

Phạm vi tệp dự kiến:

| Tệp/module | Thay đổi chính |
| --- | --- |
| database.py | Cô lập token lỗi, điều kiện chọn/nhận tác vụ, cập nhật trạng thái và bộ đếm nguyên tử, khôi phục tác vụ |
| migrations.py | Migration v6, dữ liệu mặc định và bảo toàn bản ghi hiện hữu |
| comment_dispatch.py, mới | Khóa gửi theo Page, hạn mức, khoảng cách, cooldown và kết quả chờ |
| comment_content.py, mới | Mẫu gốc, biến nội dung, Spintax, phiên bản cấu hình |
| scheduler.py | Nối executor với kiểm soát gửi chung, xử lý ứng viên bị chặn và chốt nội dung đúng phiên bản |
| operations.py | Áp dụng hạn mức cho reply, cập nhật bộ đếm, chờ tự động đúng Page |
| sheets_sync.py | Cập nhật tác vụ đơn PENDING/PAUSED, giữ nội dung ổn định khi nguồn không đổi |
| meta_client.py | Trả metadata rate limit cần thiết như code, phạm vi và Retry-After khi có |
| app.py, operations.html | API khôi phục thống nhất, trạng thái chờ trên UI, khóa tiến trình trước startup recovery |
| conftest.py, mới; các tệp test | Fixture cô lập, đồng hồ/HTTP giả, hồi quy cho 5 lỗi và cập nhật test cũ |
| README.md, .env.example, tài liệu bàn giao | Hành vi vận hành, cấu hình chờ, giới hạn chung và cách khôi phục |

Các tên module mới là thiết kế dự kiến; tên hàm và trường API cuối cùng phải nhất quán với các kiểu dữ liệu đã chốt khi triển khai.

| Chặng | Công việc | Đầu ra để review | Điều kiện chuyển chặng |
| --- | --- | --- | --- |
| A | Ghi nhận nền mã/DB, fixture chung, test 5 lỗi | Bản chụp và test tái hiện có kết quả rõ | Không chạm dịch vụ thật khi test |
| B | F1 và trạng thái lỗi trên dashboard | Cô lập token lỗi, hàng đợi tiếp tục chạy | Test F1 và chống ghi đè token mới đạt |
| C | Migration v6, cơ chế gửi chung, F2 và F4 | Hạn mức/khoảng cách/cooldown thống nhất | Test đồng thời, restart và giới hạn đạt |
| D | Helper nội dung, phiên bản cấu hình, F3 và F5 | Render ổn định và khôi phục đúng mẫu | Test cache, retry, Sheet thay đổi và chuỗi đạt |
| E | API/UI, tài liệu, chỉnh test cũ | Thông báo dễ hiểu và bộ test phù hợp hiện trạng | Toàn bộ kiểm tra bắt buộc đạt |
| F | Nâng cấp thử trên bản sao DB vận hành | Báo cáo so sánh dữ liệu trước/sau | Lịch sử, mốc quét, trạng thái Page được bảo toàn |
| G | Triển khai vào dịch vụ và nghiệm thu | Phiên bản chạy được, phương án khôi phục sẵn sàng | Có phạm vi chạy thật được người dùng chỉ định |

Các thay đổi được tách thành những phần có thể review theo chặng. Chặng C phải xong trước khi nối phản hồi hộp thư vào cơ chế mới; F3 và F5 nên hoàn tất cùng nhau vì cùng thay đổi cách lưu nội dung.

**14. Triển khai, kiểm tra sau triển khai và khôi phục**

Khi được yêu cầu triển khai, hoàn tất A–F trước. Sau đó kiểm tra lại trạng thái dịch vụ, tạm dừng gửi, chờ các lượt đang thực hiện có kết quả; các lượt không xác định kết quả chuyển NEED_CHECK. Dừng đúng tiến trình dùng DB này, tạo bản sao nhất quán, áp dụng migration rồi khởi động đúng một tiến trình trong trạng thái tạm dừng.

Đọc API localhost để kiểm tra schema, số lượng bản ghi, mốc quét, trạng thái Page, lỗi cấu hình và các tác vụ chưa rõ kết quả. Kiểm tra kết nối Meta và đọc Sheet là các bước đọc; kiểm thử tự động trước đó chỉ dùng HTTP giả lập.

Nghiệm thu gửi thật chỉ dùng Page, bài và nội dung trong phạm vi người dùng chỉ định. Bắt đầu một Page, một bài, một bình luận; đối chiếu ID trên Facebook với DB và bộ đếm. Sau đó mới kiểm tra trả lời hộp thư và một chuỗi ngắn nếu thuộc phạm vi được chọn. Trường hợp rate limit, timeout và token hỏng được kiểm tra bằng mô phỏng, không cố tạo lỗi thật trên tài khoản.

Điều kiện hoàn thành: 5 tình huống lỗi không tái diễn; toàn bộ test áp dụng đạt; bản sao DB nâng cấp qua quick_check; UI nêu đúng lý do chờ; không có thay đổi ngoài ý muốn về quyền bật Page, lịch sử hoặc mốc quét; có ghi nhận phiên bản mã và schema đã triển khai.

Nếu phải khôi phục trước khi có hoạt động gửi mới, dừng dịch vụ và phục hồi cặp mã/DB đã kiểm tra tương thích. Nếu đã gửi bình luận sau nâng cấp, ưu tiên sửa tiến hoặc quay mã tương thích với DB hiện tại. Trước mọi phương án dùng DB cũ phải giữ và đối chiếu toàn bộ kết quả gửi phát sinh sau bản sao, vì khôi phục mất lịch sử chống trùng có thể khiến bài đã xử lý bị gửi lại. Trong suốt quá trình khôi phục, giữ dịch vụ tạm dừng cho tới khi đối chiếu xong.
