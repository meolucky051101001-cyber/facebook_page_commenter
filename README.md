# Facebook Page Multi-Account Commenter Hub

Hệ thống quản lý Fanpage đa tài khoản độc lập, định tuyến kịch bản bình luận riêng cho từng Page và tự động lên lịch comment sau một khoảng thời gian ($X$ phút) kể từ khi bài viết mới được đăng.

---

## 🎯 Tính năng nổi bật

1. **Quản lý Đa tài khoản cô lập (Anti-Chain Ban):**
   - Chỉ dùng 1 App Meta duy nhất (thêm các nick làm App Tester).
   - Mỗi Fanpage được lưu trữ và sử dụng `Page Access Token` độc lập.
   - Khi 1 nick bị checkpoint hoặc mất quyền, chỉ Page của nick đó dừng, các Page ở nick khác vẫn hoạt động 100% bình thường.

2. **Kịch bản Bình luận riêng biệt theo Page:**
   - Cài đặt thời gian chờ tùy chỉnh ($X$ phút) cho từng Page.
   - Hỗ trợ cú pháp xoay vòng nội dung **Spintax** dạng `{Chào|Hello} {bạn|anh/chị}` để mỗi bài viết nhận được một bình luận tự nhiên, độc nhất.
   - Công tắc Bật/Tắt tự động độc lập cho từng Page.

3. **Cơ chế Hàng đợi Tác vụ (Task Queue) an toàn:**
   - Ràng buộc duy nhất `UNIQUE(page_id, post_id)` chống comment trùng lặp trên cùng 1 bài.
   - Bộ nhận diện sự cố: Tự động phân loại mã lỗi Meta: `SUCCESS`, `FAILED`, `TOKEN_EXPIRED`, và `NEED_CHECK` (trường hợp mất mạng hoặc phản hồi không rõ ràng, yêu cầu kiểm tra thực tế để không comment đúp).

4. **Giao diện Dashboard trực quan (FastAPI Web App):**
   - Quản lý Nick & Page.
   - Cấu hình kịch bản & thời gian trễ.
   - Soạn và hẹn giờ bình luận thủ công.
   - Theo dõi trạng thái, nhật ký và nút hủy lịch đang chờ.

---

## 🚀 Hướng dẫn khởi chạy

### 1. Cài đặt thư viện phụ thuộc (nếu chưa có)
```bash
pip install fastapi uvicorn httpx python-dateutil
```

### 2. Khởi chạy ứng dụng
```bash
cd C:\Users\admin\.gemini\antigravity\scratch\facebook_page_commenter
python run.py
```

Ứng dụng sẽ chạy tại địa chỉ: **[http://127.0.0.1:8000](http://127.0.0.1:8000)**

---

## 🛠 Hướng dẫn kết nối Nick & Lấy Token
1. Truy cập [developers.facebook.com](https://developers.facebook.com/) tạo 1 App nội bộ.
2. Thêm các nick Facebook vào mục **App Roles $\rightarrow$ Roles $\rightarrow$ Testers**.
3. Dùng từng nick truy cập **Graph API Explorer** để lấy User Access Token với các quyền:
   - `pages_show_list`
   - `pages_read_engagement`
   - `pages_manage_engagement`
4. Dán token vào Dashboard của tool, hệ thống sẽ tự động bóc tách và lưu Page Token riêng biệt vào cơ sở dữ liệu.
