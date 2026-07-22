# Google Drive auto-upload — hướng dẫn setup

Render xong, `queue_worker.py` tự đẩy video lên **một thư mục Drive mặc định** (chạy nền, không chặn job kế tiếp). Link Drive lưu vào cột `drive_link` trong `jobs.db`; lỗi upload lưu vào `drive_error` (job vẫn `done`, file gốc vẫn nằm ở `~/Desktop/Renders`).

Mọi secret nằm trong `.env` (đã gitignore) — **không** commit file JSON, không hard-code key.

## Bước 1 — Đưa file JSON vào

Đặt file JSON tải từ Google vào thư mục `secrets/` (đã gitignore):

```bash
mv ~/Downloads/<file-cua-ban>.json secrets/google_drive.json
```

## Bước 2 — Bóc secret từ JSON vào .env

```bash
python google_drive_upload.py --extract secrets/google_drive.json
```

Lệnh này tự nhận dạng loại JSON và điền các biến tương ứng vào `.env` (chmod 600). Xong có thể **xóa file JSON**.

## Bước 3 — Tùy loại JSON

### Cách A: OAuth client (Gmail cá nhân — khuyên dùng)

JSON có key `"installed"` hoặc `"web"`. Cần lấy refresh token **một lần** (mở trình duyệt, đăng nhập, bấm đồng ý):

```bash
python google_drive_upload.py --auth
```

Refresh token tự lưu vào `.env`. Video upload lên sẽ **thuộc tài khoản Google của bạn**, tính vào dung lượng của bạn.

> Nếu app OAuth đang ở chế độ *Testing* trên Google Cloud Console, nhớ thêm email của bạn vào **Test users**, và refresh token sẽ hết hạn sau 7 ngày — bấm **Publish app** để token sống lâu dài.

### Cách B: Service Account

JSON có `"type": "service_account"`. Không cần `--auth`, nhưng phải **share thư mục Drive đích cho email của service account** (quyền *Editor*) — lệnh `--extract` in sẵn email đó.

> ⚠️ Service account **không có quota lưu trữ riêng** — upload vào My Drive cá nhân thường bị lỗi `storageQuotaExceeded`. Chỉ dùng cách này với **Shared Drive** (Google Workspace). Gmail cá nhân → dùng Cách A.

## Bước 4 — Chọn thư mục đích + bật upload

Mở `.env`, điền:

```env
GOOGLE_DRIVE_FOLDER_ID=<ID>      # từ URL: drive.google.com/drive/folders/<ID> (dán cả URL cũng được)
GOOGLE_DRIVE_UPLOAD_ENABLED=1
```

## Bước 5 — Kiểm tra & test

```bash
python google_drive_upload.py --check          # credentials OK? vào được thư mục?
python google_drive_upload.py video_test.mp4   # test upload thật, in ra link
```

Xong. Restart `queue_worker.py` là các job render xong sẽ tự lên Drive.

## Job import từ Excel → thư mục con theo tên file Excel

Job tạo qua **import Excel** sẽ upload vào thư mục con mang **tên file Excel** (bỏ đuôi `.xlsx`) bên trong thư mục Drive chính — tự tạo nếu chưa có, các batch sau cùng file Excel thì dùng lại đúng thư mục đó:

```
Thư mục Drive chính/
├── Đơn hàng tháng 7/        ← import từ "Đơn hàng tháng 7.xlsx"
│   ├── video_1.mp4
│   └── video_2.mp4
└── video_render_tay.mp4     ← job không qua Excel: lên thẳng thư mục chính
```

Tên thư mục lưu ở cột `drive_folder` trong `jobs.db` (chốt tại thời điểm tạo job). Upload tay vào thư mục con: `python google_drive_upload.py video.mp4 --subfolder "Đơn hàng tháng 7"`.

## Vận hành

- **Tắt/bật nhanh:** đổi `GOOGLE_DRIVE_UPLOAD_ENABLED` (0/1) rồi restart worker.
- **Upload lỗi:** xem cột `drive_error` trong DB hoặc log worker (`Drive upload FAILED`). Upload lại tay:
  ```bash
  python google_drive_upload.py ~/Desktop/Renders/<ten_video>.mp4
  ```
- **Đổi thư mục đích:** sửa `GOOGLE_DRIVE_FOLDER_ID`, restart worker. Upload 1 lần sang thư mục khác: `--folder <ID>`.
- Worker tắt giữa chừng khi đang upload → upload dở bị bỏ (file gốc không mất) — upload lại tay như trên.

## Các biến .env

| Biến | Ý nghĩa |
|---|---|
| `GOOGLE_DRIVE_UPLOAD_ENABLED` | `1` = worker tự upload sau khi job done |
| `GOOGLE_DRIVE_FOLDER_ID` | ID thư mục Drive đích (mặc định cho mọi upload) |
| `GOOGLE_DRIVE_CLIENT_ID` / `_CLIENT_SECRET` | OAuth client (Cách A) — từ `--extract` |
| `GOOGLE_DRIVE_REFRESH_TOKEN` | OAuth — từ `--auth`, dùng vĩnh viễn |
| `GOOGLE_DRIVE_SA_CLIENT_EMAIL` / `_SA_PRIVATE_KEY` | Service account (Cách B) — từ `--extract` |
| `GOOGLE_DRIVE_CREDENTIALS_JSON` | Fallback: đường dẫn file JSON nếu chưa bóc vào env |
