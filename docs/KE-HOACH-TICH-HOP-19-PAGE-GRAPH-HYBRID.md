# Kế hoạch ưu tiên Meta Graph API cho 19 Page HYBRID

> Cập nhật 17/11/2026: người dùng đã chọn triển khai kết hợp Graph và Browser theo từng Page. [Luồng mới](KE-HOACH-VAN-HANH-GRAPH-VA-BROWSER.md) thay thế quyết định chỉ chạy Graph bên dưới. Phần dưới giữ làm lịch sử kế hoạch 16/11 và mapping do người dùng cung cấp; các mục ghi “chưa sửa” không phải trạng thái source hiện tại. DB thật vẫn chưa được đối chiếu hoặc đổi mode.

Cập nhật 16/11/2026, Asia/Bangkok, theo thông tin người dùng. Đây là kế hoạch và quy trình làm việc, chưa xác nhận đã đồng bộ DB workspace hoặc đã triển khai.

**Quyết định mới nhất: chọn Meta Graph API làm đường xử lý cho đợt triển khai 19 Page HYBRID.** 47 Page ANTIDETECT nằm ngoài phạm vi chạy và tiếp tục thuộc diện phải pause; Browser không phải phương án dự phòng khi Graph lỗi. Yêu cầu lần này chỉ cập nhật kế hoạch, chưa đổi cấu hình, đồng bộ DB, kiểm tra Facebook hoặc bật gửi.

Graph API là giao diện tích hợp chính thức, giúp tránh phụ thuộc thao tác DOM và phiên đăng nhập Browser. Điều này không bảo đảm tránh bị hạn chế hoặc khóa: quyền truy cập, chính sách nội dung, hành vi gửi và giới hạn cấp Page/tài khoản/ứng dụng vẫn áp dụng. Không đặt một mức gửi được quảng bá là “an toàn tuyệt đối”, không đổi tài khoản/transport để vượt hạn chế.

1. **Nguồn dữ liệu và lựa chọn DB**

Người dùng đã tự tích hợp, mã hóa Fernet token tài khoản Lê Thị Quyên tại:
`C:\Users\admin\.gemini\antigravity\scratch\facebook_page_commenter\fb_commenter.db`.
Người dùng xác nhận các quyền `pages_show_list`, `pages_read_engagement`, `pages_manage_engagement`, `pages_manage_posts` và danh mục **66 Page = 19 HYBRID + 47 ANTIDETECT**. Nhóm HYBRID gồm 9 Page của Bóng Ma Đề Cương và 10 Page của Lê Thị Quyên. Đây là thông tin người dùng báo, chưa phải kết quả kiểm tra Graph hoặc đối chiếu DB của Codex.

Người dùng đã chọn **giữ DB workspace hiện tại; đồng bộ riêng sau**. DB đích của kế hoạch vẫn là:
`C:\Projects\Tools\facebook comment\facebook_page_commenter\fb_commenter.db`.

Không đổi `database.DB_PATH`, không copy/attach/import hoặc mở hai DB trong lượt cập nhật này. Không suy luận cấu hình đích có thể giải mã dữ liệu từ nguồn. Tương thích khóa và chuyển dữ liệu mật thuộc bước đồng bộ riêng do người dùng quản lý; Codex không đọc khóa để đối chiếu. Không báo 19 Page đã sẵn sàng ở workspace trước khi có đối chiếu metadata sau đồng bộ.

2. **Bảo mật và quyền chạy**

- Không đọc `.env`, cookie thật, giá trị `ENCRYPTION_KEY`; không lấy/giải mã/in `page_access_token_enc`, `long_lived_user_token_enc`.
- Khi làm việc với danh mục, kế hoạch, Pilot hoặc UI vận hành, chỉ dùng `page_id`, `page_name`, `fb_profile_id`, `connection_mode`, `token_status`.
- Nếu cần đối chiếu DB ở bước sau, dùng projection đúng năm cột; không `SELECT *`, dump/export DB, lấy cả bản ghi, hoặc kiểm tra hash/chiều dài ciphertext. Lượt này chỉ sử dụng thông tin người dùng cung cấp.
- Không thực thi helper lấy token hoặc gọi API kiểm tra/scan/send trên DB thật từ công cụ chẩn đoán. `database.get_page_with_decrypted_token(page_id)` chỉ phục vụ bên trong runtime ứng dụng khi người dùng đã cho phép thực hiện đúng thao tác; không cần dán lại token.
- Không sửa `.env` hoặc tự chọn `PILOT_PAGE_ID`. Không coi việc có token, HYBRID, đồng bộ Sheet hay kiểm tra kết nối là quyền bật AUTO/gửi.
- Trạng thái yêu cầu là **tạm dừng cho đến khi người dùng cho phép**. Không chạy app thật để “xem UI”, không restart dịch vụ hoặc mở Browser thật trong lượt cập nhật này. Quyền gửi từ các phiên cũ không được dùng để tự chạy đợt tích hợp mới.
- Chưa kiểm tra tiến trình thật; không khẳng định dịch vụ đang pause chỉ dựa trên yêu cầu hoặc tài liệu.

3. **Mười mapping mới được cung cấp**

Các Page dưới đây là HYBRID theo báo cáo ở DB nguồn. Người dùng chưa cung cấp giá trị `token_status` cụ thể và chưa đối chiếu DB workspace; không tự gán `OK`/READY.

| page_id | page_name | fb_profile_id | connection_mode | token_status tại workspace |
|---|---|---|---|---|
| 1431559820038424 | Siêu iu mibobo | 61596674181645 | HYBRID theo báo cáo | Chưa đối chiếu |
| 1365190813348254 | Mibobo cute quá | 61595540657222 | HYBRID theo báo cáo | Chưa đối chiếu |
| 1408883392307164 | Mybo quá gầy | 61595910721219 | HYBRID theo báo cáo | Chưa đối chiếu |
| 1468141369706067 | TÔI là Công | 61595530361278 | HYBRID theo báo cáo | Chưa đối chiếu |
| 1400953876431537 | Mi sieu bo la bo | 61595826538327 | HYBRID theo báo cáo | Chưa đối chiếu |
| 1281191508406575 | Góc Trang Trí Xinh | 61591791771842 | HYBRID theo báo cáo | Chưa đối chiếu |
| 1113909888479874 | Tiệm Doll Xinh Xắn | 61590749657084 | HYBRID theo báo cáo | Chưa đối chiếu |
| 1256748860846855 | Chị Đẹp Handmade | 61590958924481 | HYBRID theo báo cáo | Chưa đối chiếu |
| 1238789899306225 | Chị Đẹp Unboxing | 61591154001764 | HYBRID theo báo cáo | Chưa đối chiếu |
| 1107777362413519 | Nhật Kí Sân Trường | 61571001207774 | HYBRID theo báo cáo | Chưa đối chiếu |

Nhóm 9 Page hiện hữu của Bóng Ma Đề Cương chưa có đầy đủ chín mapping trong thông báo mới. Không suy đoán ID hoặc ghép theo tên. Có thể tiếp tục thiết kế/test 19 Page bằng fixture giả; danh mục vận hành đủ 19 cần metadata an toàn của cả hai nhóm sau bước đồng bộ riêng.

4. **Luồng xử lý đã chọn: GRAPH_API**

`connection_mode=HYBRID` mô tả có liên kết Graph và Antidetect. `send_mode` là lựa chọn đường gửi riêng của ứng dụng; HYBRID không có nghĩa gửi bằng cả hai cách, tự fallback hoặc tự cấp readiness.

Mục tiêu triển khai là `send_mode=GRAPH_API` cho các Page nằm trong phạm vi được duyệt sau này; giữ nguyên `connection_mode=HYBRID` và `fb_profile_id`. Chưa thực hiện thay đổi mode trong lượt lập kế hoạch. Các bước sau chỉ bắt đầu trên dữ liệu thật sau khi hoàn tất sửa source/test và có quyền thực hiện tương ứng:

```text
Người dùng đồng bộ DB riêng, giữ DB workspace làm đích
  -> đối chiếu metadata tại DB workspace
  -> xác nhận mapping canonical/alias duy nhất
  -> bảo toàn rule, chuỗi, lịch sử, quota và pause
  -> người dùng cho phép kiểm tra Graph cho Page cụ thể
  -> runtime tự lấy token nội bộ, chỉ trả kết quả đã lọc
  -> xác minh danh tính/quyền hữu hiệu theo từng Page
  -> chọn rõ GRAPH_API cho Page được duyệt
  -> người dùng cho phép chạy Page và tác vụ cụ thể
  -> kiểm tra policy lần cuối ngay trước POST
  -> gửi qua Graph, lưu kết quả hoặc NEED_CHECK
  -> chỉ mở rộng sang Page khác khi được duyệt thêm
```

Graph dùng canonical `page_id` mới; `fb_profile_id` dạng `615...` được giữ cho Browser, ánh xạ Sheet và đối chiếu permalink/chống trùng. Không thay ID Graph bằng alias hoặc ghép bằng tên. Sheet dùng alias phải được giải về canonical đã xác nhận, không nhân đôi Page/rule/task. Giữ nguyên toàn bộ rule/chuỗi khi liên kết; kiểm thử bảo toàn bằng DB/Sheet giả, không đọc rule thật trong lượt này.

Bước đồng bộ riêng sau này cần xử lý trường hợp workspace đã có row dùng alias làm khóa Page: không chỉ đổi khóa trong một bảng rồi để rule/task/history trỏ sang khóa cũ. Diễn tập bằng dữ liệu giả phải kiểm tra các liên kết page_rules, tasks, publications, incoming_comments và khóa chống trùng. Nếu mapping mơ hồ, dừng việc gộp thay vì ghép bằng tên. Không tạo script chuyển token hoặc đụng ciphertext trong lượt cập nhật kế hoạch này.

| Nhóm | Phạm vi của kế hoạch mới |
|---|---|
| 19 HYBRID | Chỉ triển khai đường GRAPH_API trong đợt này; xác minh từng Page, chỉ chạy sau khi được phép |
| 47 ANTIDETECT | Giữ danh mục, rules và yêu cầu pause; loại khỏi phạm vi chạy, không tự mở Browser hoặc coi đã có Graph token |
| Page ANTIDETECT muốn bổ sung sau | Người dùng tích hợp Graph riêng, đối chiếu metadata, xác minh từng Page và duyệt phạm vi mới |
| Graph lỗi trên HYBRID | Pause/cooldown đúng phân loại, không fallback Browser |

Lỗi token/quyền một Page không được làm mất mapping/rule của Page khác. Rate limit cấp tài khoản/ứng dụng phải áp dụng cooldown đúng phạm vi; khi chưa xác định được phạm vi thì dừng bảo thủ phần có thể bị ảnh hưởng, không chuyển sang Page khác để thử vượt chặn. Hạn mức bình luận từng Page tiếp tục dùng chung giữa các đường ghi của Page; không reset quota khi đổi mapping, liên kết hoặc khởi động lại.

5. **Hạng mục bắt buộc trước khi chạy Graph**

Đây là các yêu cầu triển khai và kiểm chứng, chưa phải tính năng đã hoàn tất. `meta_client.py` đã có phân loại lỗi, xác minh Page/bài và xử lý một số trường hợp kết quả chưa rõ. Cần kiểm tra các nhánh hiện hữu và bổ sung phần còn thiếu, tránh viết lại toàn bộ client hoặc coi kết quả kiểm thử cũ là nghiệm thu mới.

**5.1. Cấu hình và môi trường test** — `config.py`, `conftest.py`, `test_runtime_isolation.py`.

Sửa lỗi khóa tạm dùng chung: khi dotenv bị tắt mà thiếu/sai khóa, dừng khởi động bằng thông báo tĩnh trước DB/worker; không tự sinh khóa mất sau restart. Test dùng khóa giả, DB tạm và môi trường subprocess được khai báo rõ. Chặn truy cập `.env`, DB/cookie thật và HTTP/CDP thật; không import app với cấu hình vận hành để kiểm tra.

**5.2. Startup pause và quyền theo phạm vi** — `app.py`, `scheduler.py`, các handler thao tác thủ công.

Qua đọc mã nguồn, `app.py` hiện khôi phục `automation_run_state='running'` đã lưu. `PAUSED_BY_DEFAULT` không bảo đảm pause khi DB từng running. Cần thêm chính sách startup luôn pause trong đợt tích hợp này, trước khi worker có thể gọi Facebook, kể cả DB nhập vào có trạng thái running.

Giữ rule/chuỗi/opt-in từng Page nhưng global pause phải chặn hiệu lực chạy. Resume phải có phạm vi rõ ràng; không để nút tiếp tục chung vô tình khởi chạy mọi Page cũ đang bật. Nạp thêm token hoặc check thành công không resume task.

`paused_all` hiện chưa chặn maintenance kiểm tra Graph hoặc đồng bộ Sheet. Vì vậy không khởi động app/TestClient trên DB thật để kiểm tra trạng thái. Cần chặn maintenance/scan Facebook và đồng bộ Sheet tự động khi chưa được phép, ngoài việc chặn comment/reply/publication. Một lệnh kiểm tra kết nối được duyệt chỉ cho phép thao tác đọc đúng Page; không tắt global pause để chạy lệnh đó. API, scheduler và UI phải cùng kiểm tra phạm vi, không chỉ ẩn nút trên UI.

Đối với đợt chạy Graph này, chỉ nhận Page trong danh sách được duyệt và transport GRAPH_API. Nút Resume không khôi phục toàn bộ opt-in cũ hoặc mở 47 ANTIDETECT. Restart thu hồi quyền chạy đang mở và quay về pause, không gửi bù task cũ; các task cũ cần được đánh giá riêng trước khi cho chạy.

**5.3. Readiness và định tuyến nhiều Page** — `comment_dispatch.py`, `database.py`, `app.py`, `meta_client.py`, lớp ánh xạ Sheet.

`PILOT_PAGE_ID` hiện bỏ qua proof kiểm tra kết nối ở `comment_dispatch.graph_ready`, `_public_graph_ready` trong `app.py`, và SQL readiness trong `database.py`. Cần bỏ ngoại lệ cấp quyền này hoặc thu hẹp Pilot về vai trò chọn Page hiển thị/thử nghiệm. Python, SQL và UI phải cùng yêu cầu xác minh từng Page. Không biến một biến Pilot đơn thành danh sách 19 Page được miễn kiểm tra; không sửa token_status=OK để vượt điều kiện.

Proof phải gắn với canonical Page và phiên liên kết hiện hành; đổi mapping, provider hoặc transport làm mất hiệu lực proof cũ. Token_status=OK chưa đủ để gửi. Runtime được phép phải xác minh danh tính Page và khả năng thực hiện đúng thao tác, không suy ra quyền hữu hiệu chỉ từ danh sách quyền người dùng đã báo. Trước kiểm tra thật, đối chiếu tài liệu Meta chính thức cho phiên bản Graph đang cấu hình, quyền/endpoint, chế độ ứng dụng và điều kiện truy cập liên quan; chưa coi việc thêm App Tester là đủ cho mọi môi trường.

**5.4. Mốc chốt gửi và kết quả không chắc chắn** — `comment_dispatch.py`, `scheduler.py`, `database.py`, các đường ghi của `meta_client.py`.

Sau bước chuẩn bị chậm, kiểm tra lần cuối ngay trước khi bắt đầu POST: global/Page pause, phạm vi được duyệt, transport, proof còn hiệu lực, Page/bài đúng danh tính, task còn hợp lệ, rule/generation, quota, cooldown và khóa chống trùng. Định nghĩa rõ mốc chốt gửi và đồng bộ với lệnh pause/đổi binding; không thêm bước đọc chậm sau kiểm tra cuối làm policy lỗi thời. Pause có hiệu lực trước mốc đó phải chặn POST; request đã bắt đầu không thể được tuyên bố đã hủy chỉ vì vừa pause.

Giữ quyền xử lý task và dấu trạng thái đang gửi bền trước POST. Nếu không lưu được trạng thái cần thiết thì không gửi. Nếu đã bắt đầu POST mà timeout, mất phản hồi, crash hoặc không lưu được kết quả, giữ/reconcile NEED_CHECK và chặn tự gửi lại. Nếu DB đang lỗi, giữ chặn trong tiến trình; lần khởi động sau nhận diện task SENDING chưa hoàn tất thay vì trả thẳng về PENDING. Bảo toàn ID kết quả đã xác minh và quota/dedupe qua recovery, không log response nguyên bản hoặc token. Kiểm tra riêng các đường comment, reply, publication; Pilot bình luận không cấp quyền thử ghi loại khác.

**5.5. Phân loại lỗi và backoff** — `meta_client.py`, `scheduler.py`, `comment_dispatch.py`.

| Tín hiệu | Hành vi cần kiểm chứng |
|---|---|
| Token hết hạn, thiếu quyền, sai danh tính | Dừng Page hoặc nhóm liên quan theo lỗi, vô hiệu readiness tương ứng; yêu cầu kiểm tra lại, không thử gửi bằng Browser |
| Hạn chế hoạt động hoặc lỗi chính sách | Chặn tác vụ/phạm vi bị ảnh hưởng, không coi là lỗi mạng retry được; không tự resume khi chỉ check kết nối thành công |
| Rate limit Page/account/app | Tôn trọng tín hiệu sử dụng và Retry-After nếu có; backoff có giới hạn, không retry đồng loạt hoặc đổi tài khoản để vượt giới hạn |
| Lỗi đọc tạm thời | Retry có giới hạn trong phạm vi thao tác đọc đã được cho phép |
| POST không rõ đã ghi hay chưa | NEED_CHECK; không retry mù; đối chiếu qua thao tác được phép trước khi quyết định bước tiếp theo |
| Lỗi không nhận diện | Dừng bảo thủ với mã lỗi đã lọc; không mặc định retry ghi |

Hạn mức nội bộ là trần vận hành, không phải bảo đảm Meta sẽ cho phép gửi tới mức đó. Log/exception chỉ dùng trường đã lọc, không chứa token trong URL/header/body hoặc đối tượng response; test bằng secret giả để phát hiện rò rỉ mà không truy cập bí mật thật.

6. **Thứ tự triển khai và phần Browser hoãn lại**

Lỗi P2 cấu hình khóa trong [kế hoạch bốn lỗi](KE-HOACH-SUA-4-LOI-AN-TOAN-BROWSER.md) vẫn bắt buộc cho Graph. Ba lỗi P1 riêng Browser (pause trong lần đọc DOM cuối, bỏ sót thông báo hạn chế, lỗi lưu checkpoint) vẫn chưa sửa và được giữ trong backlog. Không bắt buộc hoàn tất chúng để nghiệm thu đường Graph nếu mọi entry point Browser đã bị loại khỏi phạm vi chạy này và test chứng minh không gọi Browser. Nguyên tắc pause trước gửi và bảo toàn trạng thái khi lỗi DB vẫn phải áp dụng, kiểm thử riêng cho Graph theo mục 5.4.

| Chặng | Công việc và đầu ra | Điều kiện chuyển chặng |
|---|---|---|
| G0 | Chốt phạm vi Graph, lập test tái hiện các lỗ hổng dùng chung, fixture 19 HYBRID + 47 ANTIDETECT | Cách ly bí mật/mạng đạt; test tái hiện thất bại đúng lỗi cần sửa |
| G1 | Sửa cấu hình khóa, startup pause, maintenance và quyền chạy theo Page/thao tác | DB giả từng running vẫn pause; chưa duyệt thì không phát sinh request |
| G2 | Bỏ miễn proof Pilot; thống nhất Python/SQL/UI, routing canonical/alias, bảo toàn rule/history/quota | 19 Page được xét riêng; không nhầm provider; không Graph-ready chỉ vì HYBRID/Pilot |
| G3 | Hoàn thiện mốc chốt gửi, trạng thái gửi bền, lỗi/giới hạn và recovery | Pause chặn đúng mốc; crash/lỗi DB/timeout không dẫn tới gửi trùng; không fallback |
| G4 | Hồi quy, diễn tập nâng cấp/rollback DB giả, cập nhật tài liệu | Test bắt buộc đạt; diff sạch; bàn giao source, chưa triển khai |
| G5 — riêng sau | Người dùng đồng bộ DB, đối chiếu năm trường metadata, nghiệm thu kết nối từng Page được duyệt | Danh mục đích được xác nhận; kiểm tra thành công vẫn pause, không gửi |
| G6 — khi được cho phép | Pilot một Page và một tác vụ bình luận cụ thể, rồi đánh giá mở rộng | Có kết quả xác minh, không trùng/nhầm Page; phạm vi khác vẫn pause |
| G7 — duyệt từng đợt | Mở rộng các Page đã xác minh tới tối đa 19 theo phạm vi người dùng chọn | Không có lỗi chưa xử lý; không tự bật toàn bộ hoặc thêm 47 ANTIDETECT |

Không biến bảng này thành lệnh chạy tự động. G5–G7 là các bước vận hành riêng; quyền kiểm tra kết nối không phải quyền gửi, quyền Pilot không phải quyền mở rộng. Không yêu cầu dán lại token ở bất kỳ chặng nào.

7. **Ma trận kiểm thử bổ sung**

Fixture có thể dùng mười mapping metadata được cung cấp; chín mapping còn thiếu dùng ID tổng hợp được ghi rõ là test, không công bố là danh mục thật. Credential trong test chỉ là dữ liệu giả/provider mock.

| Tình huống | Điều kiện đạt |
|---|---|
| DB giả lưu running rồi khởi động | Vẫn pause; không Graph/CDP read/write thật |
| Thêm 10 HYBRID | Không bật AUTO, không resume task, không đổi send_mode ngầm |
| 19 Page được duyệt trong test | Mỗi yêu cầu Graph dùng đúng canonical page_id |
| Sheet dùng alias của mười Page mới | Giải đúng canonical, không tạo trùng Page/rule/task |
| Trùng tên hoặc mapping mơ hồ | Không ghép hoặc gửi tự động |
| HYBRID token_status=OK nhưng thiếu proof | Python/SQL/UI đều chặn, kể cả Pilot |
| Đổi mapping/provider/transport sau check | Proof cũ không còn hiệu lực; không gửi nhầm Page |
| Một Page thiếu quyền/hết hạn | Chặn đúng Page/phạm vi lỗi, giữ rule và mapping |
| Graph lỗi | Không gọi Browser thay thế |
| Rate limit cấp APP/ACCOUNT | Tôn trọng cooldown chung, không retry dồn |
| Hai tài khoản, 19 Page | Không dùng nhầm provider credential giả; không xuất token |
| Pause có task đến hạn | Không comment/reply/publication hoặc maintenance Facebook chưa được phép |
| Pause/đổi rule hoặc binding trong bước chuẩn bị cuối | Nếu có hiệu lực trước mốc chốt thì không POST; test bằng barrier/Event, không đoán bằng sleep |
| DB lỗi trước POST | Không request ghi, không báo SUCCESS |
| Timeout sau POST, crash SENDING hoặc không lưu được kết quả | NEED_CHECK/recovery; không tự trả về hàng đợi retry hoặc gửi trùng |
| Nhận hạn chế/lỗi chính sách | Chặn đúng phạm vi, không đổi thành retry mạng |
| Liên kết/đổi transport trong DB giả | Giữ rule/chuỗi/quota/history/dedupe, không gửi bù |
| 47 ANTIDETECT | Không Graph-ready, không tự mở Browser |
| API/scheduler/maintenance cố gọi Browser trong đợt Graph | Chặn trước Browser I/O, kể cả thao tác thủ công hoặc Page còn opt-in cũ |
| API/UI metadata mới | Chỉ projection năm trường được phép, không giải mã để hiển thị |
| Cho chạy một phạm vi nhỏ rồi Resume/restart | Resume không mở rộng phạm vi; restart quay về pause; không gửi bù task cũ |
| Chỉ cho phép kiểm tra kết nối | Chỉ request đọc đã duyệt; không comment/reply/publication |
| Hai worker cùng nhận task hoặc API trùng scheduler | Chỉ một quyền gửi được chấp nhận, quota/dedupe giữ nguyên |
| Exception/response/URL có secret giả | Log và kết quả UI đã lọc; không làm lộ secret giả |
| Rollback phiên bản không hiểu trạng thái mới | Giữ pause/NEED_CHECK, không xóa lịch sử hoặc reset counter để chạy |

Khi bắt đầu triển khai source, chạy nhóm liên quan sau mỗi chặng; ưu tiên các test hiện có `test_runtime_isolation.py`, `test_dispatch.py`, `test_multi_page.py`, `test_migrations_runtime.py` và regression mới cho Graph. Trước bàn giao chạy toàn suite trong môi trường cách ly và `git diff --check`. Không bỏ test Browser hiện có chỉ vì hoãn phát triển Browser; không dùng kết quả chúng để tuyên bố ba lỗi P1 đã được sửa. Nếu cần quyền loopback cho socketpair nội bộ trên Windows, vẫn giữ các lớp chặn HTTP/CDP thật và file vận hành.

8. **Điều kiện bàn giao, Pilot và dừng đợt chạy**

Test mới và hồi quy phải đạt trước khi triển khai. Mốc 347 tests là kết quả trước các sửa đề xuất, không chứng minh kế hoạch này đã được thực hiện. Bàn giao diff source/test, bảng hành vi lỗi, trạng thái các mục G0–G7, kết quả test thực chạy và hướng dẫn dừng/khôi phục. Lượt cập nhật 16/11 này chỉ sửa tài liệu và kiểm tra tính nhất quán Markdown; chưa thực hiện G0–G7 hoặc chạy lại suite ứng dụng.

Pilot sau này cần chốt một canonical Page, bài được phép, nội dung bình luận, thời điểm và giới hạn số lần gửi cụ thể. Chỉ chạy nhiệm vụ đã duyệt; không tự bật chuỗi/AUTO ngoài nhiệm vụ đó. Bài thuộc Page phải được xác minh. Tiêu chí đạt là kết quả đúng Page/bài/nội dung, lưu được ID và không trùng; chưa rõ kết quả thì dừng ở NEED_CHECK. Mở rộng theo từng đợt được duyệt, không có thời gian chờ hay số lượng Page nào tự động bảo đảm được phép mở rộng.

Dừng đợt chạy khi có sai identity, mất quyền/token, hạn chế hoạt động, giới hạn chưa xử lý, DB lỗi hoặc kết quả ghi chưa rõ. Giữ task/history/quota/dedupe để đối chiếu; không xóa dữ liệu để thử lại, không rollback sang Browser. Khôi phục chỉ sau xử lý nguyên nhân, xác minh phù hợp và cho phép tiếp tục đúng phạm vi.

Số 66/19/47 luôn được ghi là do người dùng cung cấp ở nguồn cho đến khi có đối chiếu metadata workspace. Quyền kiểm tra kết nối không mặc nhiên là quyền gửi; ứng dụng tự lấy token tại runtime được phép, Codex không cần người dùng dán token.
