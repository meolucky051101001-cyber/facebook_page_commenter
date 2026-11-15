# Bàn giao hiện trạng Commenter Facebook

Cập nhật: 15/11/2026, Asia/Bangkok. Thư mục làm việc và chạy dịch vụ:
`C:\Projects\Tools\facebook comment\facebook_page_commenter`.

Đọc kế hoạch trong `docs/KE-HOACH-SUA-LOI-COMMENT-TU-DONG.md` và kết quả từng chặng trong `docs/NHAT-KY-TRIEN-KHAI-COMMENT-TU-DONG.md`. Tài liệu `docs/legacy` chỉ để tham khảo lịch sử.

## Hiện trạng mới nhất sau triển khai Browser

Thông tin dưới đây thay thế các ghi nhận vận hành trước đó.

- Dịch vụ `pythonw run_background.pyw` tại `http://127.0.0.1:8080`, schema 7,
  đã khôi phục lúc 23:51 ngày 15/11 sau khi phát hiện tiến trình cũ tắt.
  PID 12000 trong `backups/browser-followup-20261115/service.pid`. Kiểm tra lại khi tiếp tục.
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

## Browser mới và việc còn lại

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
Browser Use vẫn từ chối origin http://127.0.0.1:8000 vì quyền đã lưu/chính sách,
kể cả sau khi user trả lời “Cho phép kiểm tra Antidetect” rồi “Đã mở quyền”.
Không dùng raw CDP, terminal hay cửa sổ khác để vượt chặn. Chỉ thử lại khi quyền
thực tế của công cụ được gỡ; chưa nghiệm thu actor/Reel/timestamp hoặc gửi Browser.
Chưa bật browser cho Page nào. Không hỏi lại user tên tool: user chưa có tool,
đang nhờ xây auto comment trong project này.

Giới hạn: numeric post URLs, chưa pfbid/shortlinks; cần DOM cung cấp numeric actor ID,
Page/post identity và timestamp tuyệt đối cho scan. TênPage hoặc accessState=ok chưa chứng minh đủ.

Danh mục user spreadsheet1VUwW1xxgM4c8hj6CnGjdJyyHnOHFTKIOYMzLXpduWj8 gid880102023:
65 dòng, 7 profile, 25 dòng thiếu ID. Không tự ghép tên. Nhật Kí Sân Trường/Profile4 trùng,
Chị Đẹp Handmade chưa khớp, Tạp Hóa My Bò/Profile7 chưa có local.
Báo cáo trong backups/profile-catalog-review-20261115.

## Kiểm thử và bảo toàn

264 tests passed, 1 Starlette/AnyIO deprecation. Browser tests mô phỏng,
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
