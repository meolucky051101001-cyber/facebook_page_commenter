# Bàn giao hiện trạng Commenter Facebook

Cập nhật: 18/11/2026, Asia/Bangkok. Thư mục làm việc:
`C:\Projects\Tools\facebook comment\facebook_page_commenter`.

Ưu tiên đọc `docs/KE-HOACH-VAN-HANH-GRAPH-VA-BROWSER.md` cho luồng mới. Đã triển khai source/test kết hợp: Graph hoặc phiên đăng nhập Browser được chọn riêng cho từng Page, không fallback. Startup luôn Pause; chưa đổi DB vận hành, mode, opt-in hoặc bật AUTO. Không đọc `.env`, DB thật, cookie, token hoặc khóa thật trong lần triển khai này. Kết quả kiểm thử thực chạy nằm ở mục 17/11 trong `docs/NHAT-KY-TRIEN-KHAI-COMMENT-TU-DONG.md`.

Đợt chiều 18/11 hoàn thiện review & triển khai nâng cấp toàn diện tại source workspace:
- **Đồng bộ DB an toàn (có backup trước & sau tại `backups/sync-19hybrid-20261118/`):** DB chính `fb_commenter.db` đạt `quick_check=ok`, `foreign_key_check=[]`, đủ **66 Fanpage = 19 HYBRID (9 Bóng Ma Đề Cương + 10 Lê Thị Quyên) + 47 ANTIDETECT**, xác minh giải mã Fernet thành công cả 19 token trong bộ nhớ mà không in token/khóa, bảo toàn nguyên vẹn 45 tác vụ lịch sử (44 `SUCCESS`, 1 `CANCELLED`) và đặt `automation_run_state='paused'`.
- **Quỹ dự trữ gửi & chu kỳ quét riêng cho Browser:** thêm `BROWSER_SCAN_INTERVAL_SEC=900` (15 phút/chu kỳ quét nền cho Browser), `BROWSER_RESERVED_SEND_OPERATIONS=25` (quét dừng ở 75/100 để giữ 25 lượt cuối ngày cho `send`/`check`) và ưu tiên nhường khe 60 giây khi profile đang có bình luận đến hạn gửi.
- **Nâng cấp bộ trích xuất DOM Comet (`_BROWSER_EVIDENCE_JS`) & cầu nối CDP stealth:** hỗ trợ permalink `pfbid...`, đọc Relay JSON (`script[type="application/json"]`) và React Fiber cho `post_id`, `creation_time` Unix và `actorIds` trên giao diện Facebook mới mà vẫn fail-closed khi thiếu bằng chứng; khóa đồng bộ `_PORT_BRIDGE_CONTEXT` và chặn mở tab mồ côi khi `/tabs/transient` timeout.
- **Đồng bộ giao diện:** thêm tab `6. Profile & Preflight` trên dashboard `:8080`, hiển thị hạn mức quét/quỹ dự trữ gửi trên cả `:8080` và `/commenter/`, tối ưu cache đọc khóa quản trị trong `integration/antidetect/web/app.py`. Chưa chép sang `C:\Projects\Tools\Antidetect Python` thật theo lựa chọn hiện tại.

Đợt sáng 18/11 bổ sung `operational_readiness.py`: API/profile UI chỉ đọc, preflight toàn lịch sử tác vụ, phân biệt chốt READY/WAITING/BLOCKED với proof từng Page. Trạng thái lỗi không giả định counter=0. Nút Pause không bị khóa bởi check/Resume đang chờ. Script `verify_commenter_ui.py` kiểm tra giao diện bằng 66 Page giả và chặn mọi request ngoài fixture; không chạy application/lifespan thật.

Các phần ngày 16/11 và 15/11 bên dưới là lịch sử. Quyết định cũ hoãn Browser và nhận định các lỗi chưa sửa không còn phản ánh source mới; số liệu DB/dịch vụ cũ không phải trạng thái hiện tại. Quyền chạy cũ không cấp quyền vận hành mới. DB workspace vẫn là nguồn đích, đồng bộ từ `.gemini` riêng sau.

## Lịch sử cập nhật Graph/HYBRID — 16/11

**Lựa chọn mới nhất: triển khai Graph API cho 19 HYBRID; 47 ANTIDETECT loại khỏi phạm vi chạy và giữ yêu cầu pause.** Không fallback Browser khi Graph lỗi. Đây là yêu cầu cập nhật kế hoạch, chưa cho phép sửa cấu hình vận hành, đồng bộ DB, gọi Facebook hoặc gửi. API chính thức không bảo đảm tránh ban; vẫn kiểm soát quyền, policy và giới hạn cấp Page/tài khoản/ứng dụng.

Ngày 16/11, người dùng báo đã tự nạp và mã hóa token tài khoản Lê Thị Quyên vào
`C:\Users\admin\.gemini\antigravity\scratch\facebook_page_commenter\fb_commenter.db`.
Danh mục được báo là **66 Page = 19 HYBRID + 47 ANTIDETECT**; nhóm HYBRID gồm
9 Page Bóng Ma Đề Cương và 10 Page Lê Thị Quyên. Các quyền được báo: pages_show_list, pages_read_engagement,
pages_manage_engagement, pages_manage_posts. Không coi đây là kết quả kiểm tra
live hoặc dữ liệu đã có trong DB workspace.

Người dùng đã chọn **giữ DB workspace hiện tại; đồng bộ riêng sau**. Không đổi
DB_PATH, copy/import/attach DB nguồn hoặc suy luận cấu hình mã hóa hai nơi giống
nhau. Chưa đồng bộ trong lượt này. Mapping đủ mười Page mới và test kế hoạch ở
`docs/KE-HOACH-TICH-HOP-19-PAGE-GRAPH-HYBRID.md`.

Quy tắc bắt buộc: không đọc `.env`, cookie thật, giá trị ENCRYPTION_KEY; không
lấy/giải mã/in page_access_token_enc hoặc long_lived_user_token_enc. Chỉ dùng
page_id, page_name, fb_profile_id, connection_mode, token_status khi cập nhật
danh mục/Pilot/UI. Không SELECT * hoặc gọi helper lấy token để chẩn đoán. Runtime
ứng dụng tự lấy token nội bộ khi được cho phép đúng thao tác; không hỏi lại token.

**Giữ yêu cầu tạm dừng cho đến khi người dùng cho phép chạy phạm vi cụ thể.**
Quyền gửi trong các ghi nhận cũ bên dưới không phải quyền chạy đợt tích hợp này.
Không tự bật AUTO, đổi send_mode/Pilot, resume task, gọi Graph hoặc mở Browser.
HYBRID là năng lực kết nối, không phải readiness/quyền gửi. Chưa kiểm tra dịch vụ
thật nên không xác nhận trạng thái pause hiện tại của tiến trình.

Cần sửa trước triển khai: startup hiện khôi phục run_state=running đã lưu;
maintenance còn có thể gọi Graph khi paused; PILOT_PAGE_ID còn miễn một phần
proof kiểm tra Page. Kế hoạch mới yêu cầu pause khi startup và xác minh từng
Page qua cùng quy tắc Python/SQL/UI. Bổ sung mốc kiểm tra policy trước POST,
trạng thái gửi bền và recovery NEED_CHECK khi timeout/crash/lỗi lưu kết quả.
Giữ rule/history/quota/dedupe; Resume không mở toàn bộ opt-in cũ.

Trình tự G0–G4: cách ly test → cấu hình khóa/pause/phạm vi → readiness từng Page
và mapping → an toàn gửi/lỗi/recovery → hồi quy và bàn giao source.
G5–G7 là bước riêng sau: người dùng đồng bộ DB → đối chiếu metadata/kiểm tra được
duyệt → Pilot một Page, một tác vụ → mở rộng theo từng đợt được duyệt tới 19.
Quyền check không phải quyền gửi; quyền Pilot không phải quyền bật toàn bộ.

Ba lỗi P1 Browser (pause cuối DOM, bỏ sót hạn chế, lỗi lưu checkpoint) vẫn chưa
sửa, được giữ trong backlog; không chặn nghiệm thu Graph nếu đã loại Browser
khỏi mọi entry point của đợt chạy và có test chứng minh. Lỗi P2 cấu hình khóa
dùng chung vẫn bắt buộc sửa. Kế hoạch bốn lỗi Browser giữ chi tiết để làm sau.
347 tests là mốc trước các sửa mới, không phải nghiệm thu phần kế hoạch này.
Lượt cập nhật kế hoạch mới chỉ sửa tài liệu; chưa chạy lại test ứng dụng.

## Bổ sung an toàn trong working tree ngày 16/11

Lượt rà soát này bổ sung hạn mức/khoảng cách cho toàn profile Browser, trạng thái
chặn bền khi checkpoint/hạn chế/đăng nhập lại, và kiểm thử bằng dữ liệu giả.
Trạng thái lưu trong `app_settings`, không đổi schema 7. Chưa triển khai/restart
dịch vụ hoặc bật AUTO/gửi Facebook trong lượt này; thông tin vận hành dưới đây
là ghi nhận của phiên trước, chưa được đọc lại từ DB thật. Xem README để biết
điều kiện nghiệm thu trước khi bật Browser.

Xác nhận cuối lượt: `python -m pytest -q -o faulthandler_timeout=30` đạt 347 tests,
1 cảnh báo Starlette/AnyIO deprecation; `git diff --check` sạch. Test chạy bằng
DB tạm, HTTP/CDP giả và chặn mở file vận hành. Sandbox Windows làm socketpair
nội bộ của asyncio bị treo; chạy test ngoài sandbox giải quyết vấn đề này.
Lượt test đầu trước khi siết cách ly vẫn nạp `.env` qua config; đã dừng và sửa
để bỏ qua cả đọc/ghi dotenv, kèm audit hook chặn file nhạy cảm. Không in bí mật.
Scanner luân phiên các Page cùng profile, lưu lượt quét qua restart để khoảng
cách chung không khiến Page đầu tiên chiếm mọi lượt.

Lượt tiếp tục yêu cầu commit/PR ngày 16/11: chạy lại cùng lệnh đạt 347 tests,
1 cảnh báo Starlette/AnyIO (40,54 giây), không có test bị skip; `git diff --check`
sạch. Đóng gói thay đổi trên nhánh `codex/profile-safety-graph-plan` để review
bằng Draft PR. Bốn lỗi đã ghi trong kế hoạch vẫn chưa sửa; kết quả test này
không phải nghiệm thu triển khai Graph hoặc Browser. Giữ các file legacy và
`integration/antidetect/web/app.py.before-move` ngoài commit. Không triển khai,
restart dịch vụ, đồng bộ DB hoặc bật gửi trong lượt này.

## Ghi nhận vận hành của phiên trước — chưa đối chiếu lại

Thông tin dưới đây chỉ là lịch sử. Quy tắc bảo mật, lựa chọn DB và yêu cầu tạm
dừng ở phần cập nhật Graph/HYBRID phía trên được ưu tiên; không lấy các số lượng,
PID hoặc quyền chạy cũ làm trạng thái vận hành hiện tại.

- Dịch vụ `pythonw run_background.pyw` tại `http://127.0.0.1:8080`, schema 7,
  đã cập nhật ngày 16/11 sau khi 268 kiểm thử đạt.
  PID 34996 trong `backups/new-posts-only-20261116/service.pid`. Kiểm tra lại khi tiếp tục.
- Hệ thống đang chạy; 65 Page, 65 rule, 8 tài khoản, tất cả send_mode=GRAPH_API.
  Chỉ Chị Đẹp Thích Vẽ bật AUTO. Doll gửi theo đợt thủ công đã được người dùng yêu cầu.
- 18 tasks: 17 SUCCESS, 1 CANCELLED; không còn PENDING/SENDING/NEED_CHECK sau khi hoàn tất chuỗi Vẽ mới.
- Nguồn: spreadsheet1Qq1lkhVzlDe0FJ5k3HDDkBiGYDqTItO2ycQMDQHATD0,
  tab Comments - Multi-Page, gid1974210351. URL trong DB ưu tiên cấu hình PILOT.
- Người dùng đã chọn chuỗi 3 câu của Chị Đẹp Thích Vẽ sau 30/35/40 phút.
  Đã sửa Sheet D5:E5=30/1, D12:E12=35/2, D13:E13=40/3, lưu Drive,
  tool đã sync, source_error=null. Áp dụng bài mới, không gửi bù lịch sử.

## Những bình luận thật đã hoàn thành

- Chị Đẹp Thích Vẽ canonical993921657144854, alias61575475678031:
  tasks5-9 gồm 5 bình luận đã xác minh. Bằng chứng trong
  backups/two-comments-thich-ve-2026-11-15/verified-result.json.
- Chuỗi Vẽ mới đã chạy tự động: tasks 16–18 trên post
  `993921657144854_122155270202849189`, Reel `1454772333471446`.
  Đã đối chiếu cả ba câu qua Graph; thời gian gửi 20:31, 20:36 và 20:41 UTC+7.
  Bằng chứng trong `backups/browser-transport-20261115/ve-auto-sequence-verified.json`.
  Bộ đếm Vẽ hiện 8 bình luận hôm nay, Doll 6. Backup mới nhất `post-send-final.sqlite3`.
- Chị Đẹp Thích Doll canonical1134157636440779, alias61564553242967:
  user xác nhận chỉ 2 bài hôm nay 15/11, mỗi bài 3 câu Sheet dòng2-4.
  Tasks10-12 post1134157636440779_122118128666485108, Reel1842815036864659 (13:00).
  Tasks13-15 post1134157636440779_122118140852485108, Reel1756655442063696 (18:00).
  GET xác minh 6/6 comment đúng nội dung và author. Không gửi bài hôm qua.
  Bằng chứng backups/doll-comments-20261115/verified-result.json;
  backup sau gửi post-send-verified.sqlite3 (quick_check=ok).

## Browser của giai đoạn trước — backlog, chưa cho phép chạy

Các ghi nhận dưới đây là lịch sử source/kiểm tra Browser. Hướng ưu tiên hiện tại là Graph theo phần đầu; không tiếp tục Browser hoặc dùng nó làm fallback dựa trên các ghi nhận này.

Đọc docs/KE-HOACH-BINH-LUAN-TRINH-DUYET.md. Đã triển khai schema 7, chọn send_mode,
trạng thái xác minh browser riêng token, khóa theo profile, scan/verify/send theo
mode và UI kiểm tra/khôi phục. Graph không fallback sang browser. Inbox/xuất bản Graph-only.

Đổi mode/profile xóa xác minh và pause task chờ; SENDING chặn đổi mode. Giữ quota,
cooldown,lịch sử,opt-in. Graph giữ nội dung đã claim; browser kiểm tra lại Sheet,
actor,post,focus,nội dung trước Enter. Không rõ sau Enter => NEED_CHECK, không auto retry.

Antidetect cài C:\Projects\Tools\Antidetect Python; không sửa mã/metadata ngoài repo.
Tiến trình cũ đã tắt; lần kiểm tra gần nhất server mới ở cổng 8000, PID 23580.
Profile 2: c25eebac-3cec-43ec-8d73-51f914b5437b. Người dùng đã đăng nhập lại.
Probe trước khi gặp cổng quyền chỉ trả access_state=ok nhưng không có actor ID,
bài số hoặc timestamp; chưa đủ xác minh phiên đăng nhập và khả năng gửi.
Ngày 16/11 đã truy cập được giao diện Antidetect qua Browser Use. Profile 2 đọc
trực tiếp 10 Page. Probe Page/Reel vẫn thiếu numeric actor ID và timestamp,
chưa nghiệm thu gửi Browser. Người dùng lo checkpoint và chọn chỉ xử lý bài mới
cần bình luận, hạn chế lượt truy cập thừa. Lượt cập nhật sau lựa chọn này dùng
kiểm thử mô phỏng và kiểm tra dịch vụ cục bộ.
Chưa bật browser cho Page nào. Không hỏi lại user tên tool: user chưa có tool,
đang nhờ xây auto comment trong project này.

Giới hạn: numeric post URLs, chưa pfbid/shortlinks; cần DOM cung cấp numeric actor ID,
Page/post identity và timestamp tuyệt đối cho scan. TênPage hoặc accessState=ok chưa chứng minh đủ.

Danh mục user spreadsheet1VUwW1xxgM4c8hj6CnGjdJyyHnOHFTKIOYMzLXpduWj8 gid880102023:
65 dòng, 7 profile, 25 dòng thiếu ID. Không tự ghép tên. Nhật Kí Sân Trường/Profile4 trùng,
Chị Đẹp Handmade chưa khớp, Tạp Hóa My Bò/Profile7 chưa có local.
Báo cáo trong backups/profile-catalog-review-20261115.

## Kiểm thử và bảo toàn

268 tests passed, 1 Starlette/AnyIO deprecation. Browser tests mô phỏng,
không chứng minh layout thật. Migration 6→7 diễn tập giữ mọi cột cũ/history/rule/quota/settings.
Dịch vụ mới API200,schema 7,65Page,không tự đổi mode.

Đợt tiếp tục sau đăng nhập bổ sung chờ DOM tải xong thay sleep cố định, nhận diện
URL đăng nhập/checkpoint, thử lại lỗi JS context do điều hướng trong giới hạn.
AUTO/publication chống trùng thêm theo permalink đã chuẩn hóa trong cùng Page:
Graph post ID và Reel ID khác nhau nhưng cùng link chỉ giữ một chuỗi.
Không coi mọi Reel/video/post có cùng số là một bài; không nới xác minh trước gửi.
Việc gửi qua Browser với Graph canonical ID khác Reel ID vẫn cần ánh xạ được xác minh.

Backups/browser-followup-20261115 có before-restore.sqlite3, after-restore.sqlite3,
before-restore.json, restored-health.json. quick_check=ok, toàn bộ 18 task giữ nguyên
từng giá trị qua restart; 17 SUCCESS, 1 CANCELLED; Vẽ AUTO, Doll tắt AUTO.

Backups/new-posts-only-20261116 có before-deploy.sqlite3, deployment-state.json,
verified-health.json. Đối chiếu đủ 18 task, cách gửi, mốc bài và quyền AUTO từng
Page qua restart đều giữ nguyên; quick_check=ok, API200, hệ thống running.
Luồng Browser gộp xác minh bài/thời gian và gửi vào cùng một tab, kiểm tra lại
danh tính/nội dung trước Enter. Có hồi quy bật AUTO chỉ nhận bài sau mốc bật,
restart giữ lịch sử và bật lại sau khi tắt tạo mốc mới. Dashboard giải thích
tool vẫn kiểm tra danh sách bài định kỳ. Bản sửa gồm nhận URL /people/.../ID
và giảm lượt mở bài đang ở working tree, đã triển khai cục bộ.

Backups/browser-transport-20261115 có schema 7-source.zip,before-schema 7.sqlite3,
before-deploy.sqlite3,migration-report.json,deployed-health.json,thich-ve-sequence.json,
browser-live-probe.json. Backup cũ ở backups/auto-comment-20261115-834cb6ce.

Test: python -m pytest -q --tb=short -p no:cacheprovider --basetemp .pytest_cache/unique-run

Trước thay phiên bản: pause API, chờ SENDING kết thúc,SQLite backup,dừng đúng process,restart.
Không xóa khóa khi process còn chạy. Không rollbackDB cũ làm mất 11 bình luận thật mới.
Không in/commit .env,token,cookie,proxy secret,DB hoặc rawlogs.

Git dùng git -c safe.directory='C:/Projects/Tools/facebook comment/facebook_page_commenter'.
Browser ban đầu đã push ở 9456d0a; xem Git log để lấy commit đợt tiếp tục.
Giữ nguyên legacy untracked. Không rollback về backup trước đợt gửi mới.
