# Kế hoạch sửa bốn lỗi an toàn Browser

Ngày lập: 16/11/2026, Asia/Bangkok. Đây là kế hoạch triển khai, chưa phải ghi nhận đã sửa hoặc đã nghiệm thu. Mốc tham chiếu là working tree hiện tại và kết quả 347 test đạt ở phiên trước.

Cập nhật theo thông tin mới của người dùng: hỗ trợ 19 Page HYBRID có Graph API (9 Page của Bóng Ma Đề Cương + 10 Page của Lê Thị Quyên), cùng 47 Page ANTIDETECT. Chi tiết mapping và luồng bổ sung ở `docs/KE-HOACH-TICH-HOP-19-PAGE-GRAPH-HYBRID.md`. Đây là danh mục được báo từ DB `.gemini`, chưa xác nhận trong DB workspace. Người dùng chọn giữ DB workspace và đồng bộ riêng sau. Mọi đường chạy giữ pause cho đến khi được cho phép; token/HYBRID/Pilot không cấp quyền chạy.

**Ưu tiên mới nhất: Graph API cho 19 HYBRID; 47 ANTIDETECT nằm ngoài phạm vi chạy.** [Kế hoạch Graph](KE-HOACH-TICH-HOP-19-PAGE-GRAPH-HYBRID.md) là thứ tự triển khai chính. Lỗi P2 khóa cấu hình và các yêu cầu dùng chung (startup pause, proof từng Page, phạm vi Resume, chốt gửi/recovery Graph) vẫn bắt buộc. Ba lỗi P1 riêng Browser bên dưới chưa sửa, chuyển sang backlog; không cần hoàn tất chúng để nghiệm thu Graph khi test chứng minh Browser không thể được gọi trong đợt này. Không fallback Browser. Các bước DOM/profile và nghiệm thu bốn lỗi trong tài liệu này chỉ áp dụng khi sau này mở lại nhánh Browser; không được ghi là đã hoàn tất nhờ chuyển sang Graph.

1. **Mục tiêu, phạm vi và kết quả cần đạt**

| Lỗi | Kết quả bắt buộc sau sửa |
|---|---|
| P1 — Tạm dừng đến trong lần đọc DOM cuối vẫn gửi | Lệnh dừng có hiệu lực trước mốc chốt gửi phải ngăn Enter; tác vụ đã bắt đầu gửi được xử lý như thao tác đang diễn ra |
| P1 — Bỏ sót thông báo hạn chế | Hai thông báo đã tái hiện và các biến thể có kiểm thử phải chuyển sang RESTRICTED, chặn toàn profile |
| P1 — Ghi checkpoint thất bại làm mất trạng thái chặn | Giữ lỗi Facebook gốc, dừng profile ngay trong tiến trình và vẫn chặn sau crash/restart |
| P2 — Sinh khóa mã hóa tạm khi dotenv bị tắt | Thiếu khóa phải báo lỗi khởi động; không tạo token được mã hóa bằng khóa không thể khôi phục |

Phạm vi thực hiện là mã nguồn, test mô phỏng và tài liệu. Không đọc `.env`, `fb_commenter.db`, bản sao DB vận hành hoặc cookie thật. Không chạy Browser thật, gửi Facebook, restart dịch vụ vận hành hoặc bật AUTO trong quá trình sửa/test. Các thay đổi đang có và file legacy/untracked phải được giữ nguyên.

Với cập nhật Graph mới, chỉ dùng `page_id`, `page_name`, `fb_profile_id`, `connection_mode`, `token_status` khi đối chiếu danh mục/Pilot/UI. Không lấy, giải mã hoặc in giá trị token mã hóa/khóa thật; test cấu hình và crypto bên dưới chỉ dùng dữ liệu giả. Chưa đổi `DB_PATH` hoặc `PILOT_PAGE_ID`. Runtime tự lấy token nội bộ sau khi được cho phép đúng thao tác; không yêu cầu người dùng dán token.

Những nguyên tắc nghiệm thu xuyên suốt:

- Chưa bắt đầu gửi: không được có Enter hoặc ghi SUCCESS.
- Đã bắt đầu gửi nhưng chưa xác nhận: giữ NEED_CHECK; không tự gửi lại.
- Có ID bình luận đã xác minh: bảo toàn bằng chứng thành công, kể cả khi dọn trạng thái an toàn gặp lỗi.
- Checkpoint/hạn chế/đăng nhập lại chặn mọi Page Browser cùng profile; profile khác và Graph không bị vô hiệu theo lỗi profile này.
- Kiểm tra kết nối thành công không tự khôi phục hàng đợi cũ.
- Hạn mức, lịch sử, khóa chống trùng và generation không bị reset bởi recovery.
- Log chỉ dùng mã lỗi, loại thao tác và định danh nội bộ cần thiết; không ghi nội dung DOM, khóa, token hay cookie.

2. **Chuẩn bị và viết test tái hiện trước khi sửa**

Kiểm tra lại Git, xác định đúng các thay đổi chưa commit và các test hiện có. Không chạy formatter toàn repo hoặc gộp sửa ngoài bốn lỗi. Nếu tạo branch khi bắt đầu triển khai, dùng tên theo tiền tố `codex/`; không reset working tree để tạo baseline.

Đưa bốn tình huống đã tái hiện thành regression test chính thức:

- Đặt pause trong lần inspect cuối, sau callback chuẩn bị: phiên bản hiện tại còn gửi Enter.
- Chạy chính JavaScript nhận diện bằng Node với hai câu hạn chế tiếng Anh/Việt: hiện trả `ok`.
- Gây lỗi SQLite khi lưu checkpoint: hiện bị đổi thành NETWORK_RETRYABLE/ERROR và profile vẫn READY.
- Khởi tạo cấu hình hai lần với dotenv bị tắt, không có ENCRYPTION_KEY: hiện sinh hai khóa khác nhau và token giả không giải mã lại được.

Test ban đầu phải thất bại đúng assertion của lỗi cần sửa. Không chấp nhận thất bại do thiếu runtime, socketpair bị sandbox chặn, fixture sai hoặc truy cập mạng ngoài ý muốn. Test dùng thread Event/barrier để điều khiển thứ tự chạy; không dùng sleep để đoán thời điểm race.

3. **Sửa P2 — khóa mã hóa không được là khóa tạm ngoài ý muốn**

File chính: `config.py`, `crypto.py` nếu cần dùng chung hàm kiểm tra khóa, `conftest.py`, `test_runtime_isolation.py`.

Quy tắc cấu hình đề xuất:

| Chế độ dotenv | Khóa được cung cấp | Hành vi |
|---|---|---|
| Tắt | Thiếu, rỗng hoặc chỉ khoảng trắng | Báo lỗi cấu hình ngay; không tự sinh khóa, không mở DB hoặc khởi động worker |
| Tắt | Khóa Fernet hợp lệ | Dùng đúng khóa được cấp; không đọc/ghi `.env` |
| Tắt | Khóa sai định dạng | Báo lỗi tĩnh, không đưa giá trị khóa vào exception/log |
| Bật | Có khóa hợp lệ | Giữ hành vi dùng khóa hiện có |
| Bật | Chưa có khóa | Giữ luồng khởi tạo hiện tại; chỉ tiếp tục khi lưu khóa thành công |

Thực hiện kiểm tra khóa sớm, trước khi app khởi tạo DB/worker. Không sửa `.env` thật để đáp ứng cấu hình. Fixture vẫn tự tạo khóa giả và truyền rõ ràng trước khi import app.

Kiểm thử nhánh dotenv bật trên bản sao `config.py` trong thư mục tạm cùng `.env`/`.env.example` giả. Kiểm thử hai tiến trình với cùng khóa giả xác nhận dữ liệu giả vẫn giải mã được sau restart. Subprocess phải được cấp môi trường thử nghiệm chủ động; không dựa vào audit hook của tiến trình cha vì hook không tự kế thừa.

Tiêu chí xong: cấu hình thiếu khóa không thể khởi động dịch vụ, khóa hợp lệ ổn định qua restart, và không có đường đọc file bí mật thật trong test.

4. **Sửa P1 — chặn an toàn khi ghi checkpoint thất bại**

File chính: `browser_safety.py`, `antidetect_client.py`, `scheduler.py`, `app.py`; test safety/client/scheduler/API/runtime.

Chỉ bắt exception rồi giữ một cờ trong RAM chưa đủ: restart sẽ mất cờ. Phương án đề xuất là ghi dấu thao tác đang diễn ra trước Browser I/O, kết hợp cờ chặn trong bộ nhớ khi lưu trạng thái an toàn thất bại.

Mở rộng JSON trong `app_settings` lên phiên bản trạng thái mới. Có thể giữ schema SQL 7 nếu không thêm bảng/cột. Trạng thái cũ được đọc/nâng cấp có kiểm soát, giữ nguyên ngày, số lượt, thời điểm lượt cuối, trạng thái chặn và generation.

Dấu thao tác gồm `operation_id`, `owner_instance_id`, `page_id`, `kind` (check/scan/verify/send), `started_at`, generation; có thể thêm `task_id` cho thao tác gửi. Không lưu nội dung bình luận hoặc thông tin xác thực. ID thao tác phân biệt hai lượt ngay cả khi generation chưa đổi.

Luồng bắt đầu:

- Giữ khóa profile theo cơ chế hiện có.
- Kiểm tra cờ chặn trong bộ nhớ và dấu thao tác còn dở.
- Trong một transaction, kiểm tra hạn mức, tăng bộ đếm và ghi dấu thao tác.
- Chỉ đọc metadata/mở Browser sau khi transaction commit thành công.
- Ghi thất bại: không mở Browser, không Enter; trả lỗi an toàn có cấu trúc.

Luồng phát hiện checkpoint/hạn chế/đăng nhập lại:

- Ghi nhận trạng thái Facebook gốc vào cờ chặn bộ nhớ trước khi thử ghi DB.
- Trong một transaction: lưu trạng thái chặn, tăng generation, vô hiệu proof Browser của các Page cùng profile, pause PENDING và xóa đúng dấu thao tác tương ứng.
- Nếu transaction thất bại, giữ cờ bộ nhớ và dấu thao tác đã ghi từ đầu; không biến lỗi thành NETWORK_RETRYABLE.
- Chỉ đặt `profile_safety_recorded=true` sau commit thành công. Bổ sung tín hiệu riêng như `safety_persistence_failed` để phân biệt đã thấy cảnh báo với đã lưu được cảnh báo.
- Không tự xóa cờ vì hết timeout hoặc vì DB vừa đọc được trở lại.

Luồng kết thúc bình thường:

- Xóa dấu thao tác bằng đối chiếu operation ID, owner và generation; kết quả cũ không được xóa lượt mới.
- Refactor xử lý kết quả để exception trong cleanup không che mất lỗi chính hoặc ID bình luận đã xác minh.
- Nếu Browser đã xác nhận thành công nhưng cleanup lỗi: vẫn giữ comment ID và kết quả thành công nếu lưu được, đồng thời chặn lượt Browser tiếp theo. Nếu chưa ghi được kết quả task, recovery phải đưa task sang NEED_CHECK, tuyệt đối không tạo lượt gửi lại.
- Nếu đã Enter mà kết quả không rõ, giữ NEED_CHECK dù việc ghi block/cleanup có lỗi.

Luồng startup/recovery:

- Sau khi lấy khóa độc quyền DB và init/recover task, đối chiếu dấu thao tác còn dở trước khi khởi động worker. Theo yêu cầu tích hợp Graph mới, startup giữ pause, không tự khôi phục quyền chạy từ run_state=running đã lưu.
- Profile có thao tác dở chuyển sang trạng thái riêng như RECOVERY_REQUIRED, hiển thị “Cần kiểm tra sau gián đoạn”; vô hiệu proof và pause PENDING cùng profile. Không gán nhầm CHECKPOINT nếu chỉ biết có gián đoạn.
- Không đọc/ghi được bước đối chiếu thì worker không được chạy.
- Kiểm tra tại `begin_operation` nữa để các entry point tương thích không vượt qua bằng cách bỏ qua lifespan.
- Chỉ kiểm tra kết nối chủ động được bắt đầu recovery. Ghi dấu kiểm tra mới nhưng giữ trạng thái chặn cho đến khi đúng danh tính và generation được xác nhận, commit xóa chặn thành công.
- Kiểm tra cũ, kiểm tra thất bại hoặc lỗi lưu kết quả recovery không được mở khóa. Page/hàng đợi khác vẫn cần được kiểm tra/khôi phục theo luồng hiện có.

| Lỗi được tiêm vào | Hành vi nghiệm thu |
|---|---|
| Lỗi giữ quota/ghi dấu thao tác | 0 lần đọc metadata, 0 lần mở tab, 0 Enter |
| Checkpoint trước Enter, ghi block lỗi | Giữ CHECKPOINT, không retry gửi, chặn các Page cùng profile |
| Hạn chế sau Enter, ghi block lỗi | NEED_CHECK, giữ trạng thái RESTRICTED và dấu thao tác |
| Đọc trạng thái an toàn cuối bị lỗi | Không Enter; không bỏ dấu thao tác để giả vờ kết thúc sạch |
| Có comment ID nhưng cleanup lỗi | Bảo toàn ID; không biến SUCCESS thành tác vụ tự retry |
| Tiến trình chết sau khi ghi dấu | Restart cách ly profile trước worker |
| Đối chiếu startup bị lỗi | Worker không khởi động |
| Kết quả cũ đến sau block/lượt mới | Không xóa generation hoặc dấu thao tác mới |

Kiểm thử gồm lỗi lock, read-only/full giả lập, commit thất bại và crash bằng subprocess trên DB tạm. Chỉ terminate subprocess do chính test tạo. Thêm test nâng cấp JSON cũ, JSON hỏng, version không hỗ trợ và dữ liệu dấu thao tác thiếu trường; dữ liệu không hợp lệ phải chặn thao tác.

5. **Sửa P1 — lệnh dừng đến sát bước gửi**

File chính: `antidetect_client.py`, `scheduler.py`, `app.py`, `comment_dispatch.py`; có thể thêm module nhỏ `submit_control.py` không phụ thuộc DB để quản lý cờ/phiên bản dừng. Rà soát các điểm thay đổi quyền AUTO/hạn mức trong `database.py`.

Tách callback hiện tại thành hai nhiệm vụ:

- `prepare_for_submit`: tải Sheet một lần và thực hiện các công việc có thể chậm.
- `final_submit_gate`: chỉ kiểm tra trạng thái hiện tại và chốt quyền gửi; không HTTP, không tải Sheet lại, không điều hướng hoặc đọc metadata Browser.

Thứ tự xử lý đề xuất:

```text
Giữ lượt và dấu thao tác
  -> xác minh Page/bài/thời gian
  -> điền nội dung
  -> chuẩn bị nguồn và policy có thể chậm
  -> đọc DOM cuối: actor, bài, nội dung, focus, trùng lặp, cảnh báo
  -> đối chiếu mapping/profile
  -> final gate: Page/task/rule/quota + cờ dừng/phiên bản
  -> chốt gửi
  -> đánh dấu submitted và dispatch Enter
  -> xác minh ID bình luận / NEED_CHECK
```

Để bắt cả chu kỳ pause rồi resume, dùng số phiên bản tăng đơn điệu bên cạnh boolean. Ticket của lượt gửi lưu phiên bản global, Page và trạng thái an toàn liên quan. Tắt/bật lại trong lúc chuẩn bị làm ticket cũ mất hiệu lực.

Các nguyên tắc đồng bộ:

- Pause công bố cờ dừng và tăng phiên bản trước khi chờ ghi DB. Nếu ghi DB lỗi, cờ dừng tại tiến trình vẫn có hiệu lực.
- Tắt AUTO của Page làm mất quyền gửi AUTO; không vô tình coi đó là yêu cầu hủy mọi tác vụ MANUAL.
- Resume/enable chỉ gỡ cờ sau khi kiểm tra và ghi trạng thái thành công. Không hồi sinh ticket cũ.
- Final gate đọc policy bên ngoài mutex điều khiển, sau đó lấy mutex rất ngắn để đối chiếu cờ/phiên bản và đánh dấu ticket đã chốt.
- Đọc policy bằng một snapshot trên cùng kết nối SQLite, tránh ghép các lần đọc ở những thời điểm khác nhau. Dùng timeout chờ khóa ngắn cho đường kiểm tra cuối, đề xuất 200 ms; không kế thừa mặc định chờ 30 giây của kết nối thông thường. Hết thời gian chờ thì hủy/defer, không gửi với dữ liệu cũ.
- Không giữ mutex điều khiển trong khi đọc SQLite, đọc file, gọi mạng/CDP hoặc `await`.
- Sender giữ Page lock và profile lock như hiện tại. Final gate lấy Sheet lock theo chế độ không chờ; nếu đang refresh thì hủy/defer lượt chưa gửi. Hiện Sheet lock bao cả HTTP, nên không được chờ khóa này sau DOM cuối.
- Sheet lock giữ ổn định nguồn trong bước đọc policy/chốt; các writer cấu hình còn lại phải tuân thủ Page guard hoặc cơ chế phiên bản tương ứng. Endpoint sửa hạn mức cần được rà soát vì hiện có thể ghi trong lúc gửi.
- Đường pause/tắt AUTO không chờ Page lock, profile lock hoặc Sheet lock. Không thêm chiều lấy khóa ngược từ mutex điều khiển sang những khóa này.

Bằng chứng DOM phải có thời điểm monotonic, đánh mốc trước khi gửi yêu cầu inspect để tính cả độ trễ trả lời CDP. Đề xuất giới hạn ban đầu 2 giây giữa mốc đó và mốc chốt; tuổi bằng chứng và timeout đọc policy được đặt thành hằng nội bộ có test. Nếu DB chậm, khóa bận hoặc bằng chứng đã cũ, hủy/defer trước Enter. Không lặp đọc DOM vô hạn để cố gửi.

Hợp đồng dừng cần nêu đúng: lệnh dừng thắng trước mốc chốt thì không có Enter; sau mốc chốt, tác vụ đã được coi là đang gửi và có thể hoàn tất. Không thể hứa thu hồi gói CDP đã bắt đầu gửi. UI/API cần diễn đạt “dừng nhận lượt gửi mới”, đồng thời giữ NEED_CHECK khi kết quả lượt đang diễn ra chưa rõ.

Không mở rộng API hủy SENDING trong đợt sửa này. API hủy hiện chỉ nhận PENDING/PAUSED; giữ quy tắc đó và kiểm thử nó.

| Mốc thay đổi | Kết quả cần đạt |
|---|---|
| Pause trong DOM cuối | 0 Enter |
| Tắt AUTO trong DOM cuối | 0 Enter cho tác vụ AUTO |
| Pause rồi resume trước final gate | Ticket cũ bị hủy |
| Pause sau đọc policy, trước chốt | 0 Enter |
| Chốt gửi thắng trước pause | Tối đa 1 Enter; mất phản hồi -> NEED_CHECK |
| Sheet đang refresh | Hủy/defer nhanh, không tải Sheet lần hai |
| DB đọc policy lỗi/chậm | 0 Enter |
| DOM quá hạn hoặc profile generation đổi | 0 Enter |
| Chính lượt gửi đã giữ slot cuối ngày | Vẫn được dùng slot đã giữ, không tự chặn vì chính bộ đếm của nó |
| Page/profile khác bị dừng | Không hủy nhầm lượt độc lập |

6. **Sửa P1 — nhận diện thông báo hạn chế**

File chính: `_BROWSER_ACCESS_STATE_JS` trong `antidetect_client.py` và các fixture JavaScript trong `test_browser_client.py`.

Chuyển danh sách nhận diện thành nhóm mẫu có tên rõ ràng theo trạng thái, thay vì tiếp tục nối một regex dài. Chuẩn hóa Unicode NFC, khoảng trắng, chữ hoa/thường và dấu nháy; không loại bỏ nội dung đến mức mọi chữ “hạn chế” đều bị coi là cảnh báo.

Bổ sung chắc chắn hai thông báo đã tái hiện:

- “We restrict certain activity to protect our community.”
- “Chúng tôi hạn chế một số hoạt động để bảo vệ cộng đồng của mình.”

Thêm các biến thể có căn cứ và fixture cụ thể: dấu câu, xuống dòng, dấu nháy, thông báo đứng một mình không có tiêu đề “temporarily blocked”. Dùng cùng một hàm cho readiness, inspect trước gửi và inspect sau gửi để không bị lệch tập mẫu.

Chỉ dựa vào thông báo hệ thống đang hiển thị. Không quét toàn `document.body` và không suy ra hạn chế từ bài đăng/bình luận/draft đang trích dẫn cảnh báo. Với cảnh báo nằm trong bài viết, chỉ mở rộng selector khi có fixture phân biệt được phần thông báo hệ thống với nội dung người dùng; không bỏ điều kiện chống nhận nhầm một cách hàng loạt.

Node phải chạy chính JavaScript sản phẩm trên DOM giả, không chỉ test lại một bản regex viết riêng bằng Python. Trong lượt nghiệm thu cuối, thiếu Node là thiếu điều kiện chạy nhóm DOM test; không coi skip nhóm này là đã đạt.

Kiểm thử mỗi nhóm thông báo ở các mốc trước nhập, sau nhập, sau bước chuẩn bị và sau Enter. Trước Enter phải dừng; sau Enter phải giữ NEED_CHECK và chặn profile. Kiểm thử âm gồm bài/bình luận trích dẫn, ô soạn thảo, phần tử ẩn, thông báo thành công và nội dung không liên quan. Không log nguyên văn DOM đã đọc; nếu cần chẩn đoán chỉ trả mã mẫu nhận diện.

7. **Tích hợp scheduler/API và quy tắc ưu tiên kết quả**

Đây là bước bắt buộc sau các sửa riêng lẻ, vì handler chung hiện có thể che mất lỗi quan trọng.

| Bằng chứng cuối cùng | Trạng thái task / profile |
|---|---|
| Chưa Enter, policy bị hủy | Release theo nguyên nhân: PENDING/PAUSED/CANCELLED; không tăng số bình luận thành công |
| Chưa Enter, thấy cảnh báo Facebook | PAUSED; profile chặn, kể cả khi persistence lỗi |
| Đã bắt đầu gửi, chưa có kết quả chắc chắn | NEED_CHECK; không retry tự động |
| Có ID bình luận mới đúng Page/nội dung | SUCCESS nếu lưu được; nếu chưa lưu được thì recovery giữ NEED_CHECK |
| Đã có kết quả nhưng cleanup an toàn lỗi | Bảo toàn kết quả/bằng chứng; chặn lượt Browser tiếp theo |

Nhánh lỗi an toàn phải được xử lý trước nhánh retry mạng chung. Nếu DB vẫn lỗi đến mức không ghi được task, cờ bộ nhớ và dấu thao tác phải tiếp tục ngăn Browser; không phụ thuộc vào việc đổi được task sang PAUSED ngay lúc đó.

Áp dụng đồng nhất ở scan, kiểm tra kết nối, xác minh bài thủ công, gửi scheduler và entry point tương thích. API không trả READY nếu commit xác nhận chưa thành công; kiểm tra cũ không ghi đè block mới. HTTP có thể dùng 503 cho lỗi lưu trạng thái an toàn và 409 cho profile chưa đủ điều kiện, với thông báo tĩnh không chứa exception raw. UI chỉ cần hiển thị lý do dừng, trạng thái cần kiểm tra và thao tác khôi phục phù hợp.

8. **Thứ tự thực hiện và chia thay đổi để review**

Đối với hướng Graph đang được chọn, dùng thứ tự G0–G7 trong kế hoạch Graph; phần cấu hình B và yêu cầu dùng chung B1/B2 được đưa vào đó. Bảng dưới giữ thứ tự cho đợt sửa Browser sau này, không phải điều kiện để bắt đầu hoặc bàn giao Graph. Chặng C/D/E vẫn chưa thực hiện; thiết kế mốc chốt gửi/recovery cho Graph được nghiệm thu riêng, không đồng nghĩa đã sửa ba lỗi Browser.

| Chặng | Công việc | Điều kiện chuyển chặng |
|---|---|---|
| A | Đóng băng phạm vi, bổ sung test tái hiện bốn lỗi | Các test thất bại đúng nguyên nhân, môi trường test cách ly |
| B | Kiểm tra khóa cấu hình | Nhánh thiếu/sai khóa dừng sớm; khóa hợp lệ qua restart |
| B1 | Startup pause, chặn maintenance Facebook chưa được phép, loại miễn proof của Pilot | DB giả từng running vẫn pause; Python/SQL/UI đều yêu cầu kiểm tra từng Page |
| B2 | Graph cho 19 HYBRID và ánh xạ alias | Không tự bật, không fallback Browser, giữ rules/history/quota trong fixture giả |
| C | Dấu thao tác, chặn bộ nhớ, recovery, giữ lỗi gốc | Ma trận lỗi DB và crash/restart đạt |
| D | Tách callback và bổ sung mốc chốt gửi | Ma trận pause/epoch/khóa/DOM cũ đạt |
| E | Bổ sung nhận diện hạn chế | Fixture JS dương/âm và các mốc trước/sau Enter đạt |
| F | Scheduler/API/UI, hồi quy đầy đủ, tài liệu | Tất cả điều kiện nghiệm thu bên dưới đạt |

Có thể làm cấu hình và thiết kế test DOM song song. Các thay đổi cùng chạm `antidetect_client.py` phải có một đầu mối tích hợp; không để nhiều nhánh sửa vùng exception/lifecycle của sender đồng thời mà không đối chiếu. Mỗi chặng có diff tập trung và test đi kèm; không commit các file legacy/untracked có sẵn vào phạm vi sửa này.

9. **Lệnh xác nhận và điều kiện bàn giao**

Phạm vi phần này là đợt hoàn tất sửa bốn lỗi Browser. Đợt Graph dùng ma trận và điều kiện bàn giao trong kế hoạch Graph, vẫn chạy các test hiện có còn áp dụng để phát hiện hồi quy, nhưng không tuyên bố đã nghiệm thu Browser.

Chạy nhóm phù hợp sau từng chặng, không chạy lại toàn bộ suite sau mỗi thay đổi nhỏ:

```powershell
python -m pytest -q test_runtime_isolation.py
python -m pytest -q test_browser_safety.py test_browser_client.py
python -m pytest -q test_browser_scheduler.py test_profile_dispatch.py test_browser_api.py test_dispatch.py
python -m pytest -q test_migrations_runtime.py test_browser_database.py test_multi_page.py
```

Sau khi tích hợp hoàn tất:

```powershell
python -m pytest -q -o faulthandler_timeout=30
git -c safe.directory="C:/Projects/Tools/facebook comment/facebook_page_commenter" diff --check
```

Giữ audit hook chặn file vận hành, DB tạm, khóa giả và chặn HTTP/WebSocket thật. Nếu sandbox Windows tiếp tục chặn socketpair nội bộ của asyncio, chạy cùng bộ test trong môi trường cho phép loopback nội bộ; không gỡ các lớp chặn HTTP/CDP hay file nhạy cảm để làm test pass.

Điều kiện bàn giao mã nguồn:

- Tất cả regression test của bốn lỗi và toàn bộ test còn áp dụng đều đạt; cập nhật những kỳ vọng cũ bị thay đổi có chủ đích bằng giải thích rõ ràng.
- Không skip các test nhận diện DOM cần Node trong lượt nghiệm thu cuối.
- Không mất tín hiệu checkpoint/restricted trong mọi lỗi DB được tiêm vào.
- Crash/restart không làm mất chặn; kết quả không rõ không được tự gửi lại.
- Không deadlock giữa Page/profile/Sheet/mutex điều khiển; pause không bị giữ sau HTTP hoặc SQLite timeout.
- Hạn mức ngày, khoảng cách, luân phiên quét, mapping, timestamp và dedupe vẫn đúng.
- Giữ nguyên dữ liệu trong diễn tập nâng cấp/recovery trên DB giả.
- `git diff --check` sạch; cập nhật README, tài liệu bàn giao và nhật ký kết quả thực chạy, không dự đoán số lượng test sẽ pass.

Kết quả bàn giao gồm mã nguồn, test hồi quy, bảng hành vi lỗi, hướng dẫn khôi phục profile và báo cáo các kiểm tra đã chạy. Chỉ sau đó mới đánh giá triển khai dịch vụ và nghiệm thu Facebook thật như một bước riêng. Việc sửa source/test không tự cho phép bật AUTO hoặc gửi thử thật.

10. **Triển khai và rollback cần diễn tập trước trên dữ liệu giả**

Diễn tập đường nâng cấp từ trạng thái JSON cũ, startup có task SENDING/dấu thao tác dở, shutdown đang gửi và khôi phục sau lỗi lưu DB. Không hạ phiên bản state bằng cách bỏ marker hoặc xóa counter để làm bản cũ chạy được.

Nếu cần rollback code sau này, phải giữ lịch sử gửi, counter, NEED_CHECK và trạng thái chặn. Bản code chưa hiểu version trạng thái mới phải chặn Browser thay vì mặc định READY. Diễn tập điều này trên DB tạm trước khi có kế hoạch triển khai thực tế.

Một điểm vận hành cần tách khỏi bốn lỗi: mặc định 100 thao tác/profile/ngày bao gồm cả lượt quét. Với quét mỗi 240 giây và Browser luôn khả dụng, riêng quét định kỳ có thể dùng hết ngân sách sau khoảng 6 giờ 40 phút, chưa tính kiểm tra/gửi. Đợt sửa này giữ hạn mức hiện tại; trước khi chạy cả ngày cần đánh giá nhịp quét và phân bổ ngân sách, không mặc nhiên nâng giới hạn để vượt qua điều kiện chặn.
