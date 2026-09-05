# Nối server rendervideo vào dashboard

Dashboard **không tự đi lấy (pull)** dữ liệu từ server nào — mỗi server tự
**đẩy (push)** trạng thái của chính nó lên định kỳ qua các endpoint dưới đây.
Không server nào chủ động push thì server đó sẽ không bao giờ xuất hiện trên
dashboard.

**Mục lục**
- [Đăng ký node — lấy token](#đăng-ký-node--lấy-token)
- [Xác thực](#xác-thực)
- [5 endpoint](#5-endpoint) — [tài nguyên máy](#1-post-apiingestnode--tài-nguyên-máy), [render](#2-post-apiingestnodejobs--hàng-đợi-render), [TTS](#3-post-apiingestnodetts--hàng-đợi-tts), [thống kê](#4-post-apiingestnodestats--thống-kê-riêng-node), [batch Excel](#5-post-apiingestnodebatches---hoàn-thành-batch-excel)
- [Node online/offline hoạt động thế nào](#node-onlineoffline-hoạt-động-thế-nào)
- [Tần suất đẩy khuyến nghị](#tần-suất-đẩy-khuyến-nghị)
- [Xử lý lỗi](#xử-lý-lỗi)
- [Tài liệu tương tác](#tài-liệu-tương-tác)

## Đăng ký node — lấy token

Mỗi server có **1 token riêng** (không còn 1 secret dùng chung cho mọi máy
như trước) — token tự xác định luôn nó là node nào, không cần khai `node_id`
ở đâu cả. node_id do dashboard tự sinh ngẫu nhiên, không phải chuỗi đoán được
kiểu "node01"/"node02" — 1 token lộ ra cũng không suy ra được token của máy
khác, và không máy nào giả mạo/ghi đè dữ liệu của máy khác được.

**Cách 1 — qua giao diện (khuyến nghị, không cần biết dòng lệnh):**

Đăng nhập dashboard → mục **Node keys** ở sidebar → **Tạo node mới** → điền
tên gợi nhớ tuỳ chọn (vd "Render-01") → bấm Tạo. Token hiện ngay trong 1
banner, có nút copy — đóng banner là không xem lại được nữa (backend chỉ lưu
hash). Token bị lộ/thất lạc thì bấm **Tạo lại key** ở đúng dòng node đó —
token cũ mất hiệu lực ngay lập tức, không cần tự xoá gì trong Mongo.

**Cách 2 — qua dòng lệnh** (tiện cho script hoá, kết quả giống hệt cách 1):

```bash
cd dashboard && docker compose exec -it backend python -m app.scripts.create_node
```

Script hỏi 1 tên gợi nhớ (tuỳ chọn), rồi in ra:

```
INGEST_TOKEN=<chuỗi dài>
```

Cả 2 cách: token chỉ hiện **đúng 1 lần** lúc tạo/tạo lại — copy ngay vào cấu
hình của đúng server đó.

## Xác thực

Mọi request dưới đây cần header:

```
X-Ingest-Token: <token của đúng node đó>
```

Thiếu hoặc sai token → `401`. Không còn khái niệm "tắt xác thực" như bản cũ —
không có token hợp lệ thì không push được.

**Base URL**: domain/IP dashboard của bạn (vd `http://10.10.11.103`) — nginx
route thẳng `/api/ingest/*` vào backend, không cần biết port backend thật.

## 5 endpoint

`node_id` **không xuất hiện trong URL** — token gửi lên tự resolve ra đúng
node, mỗi server chỉ cần đúng 1 giá trị `X-Ingest-Token` là đủ, không cần biết
hay tự đặt `node_id`. Mỗi lần push là **ghi đè toàn bộ**, không phải patch
từng phần.

### 1. `POST /api/ingest/node` — tài nguyên máy

CPU/GPU/RAM/disk hiện tại. **Không** gửi `online`/`last_seen_sec_ago` — dashboard
tự suy ra từ thời điểm nhận request này (xem mục Online/Offline bên dưới).

```bash
curl -X POST http://<dashboard>/api/ingest/node \
  -H "X-Ingest-Token: <token>" -H "Content-Type: application/json" -d '{
    "name": "Render-01",
    "gpu_tag": "RTX 4090",
    "cpu_percent": 62,
    "load_avg": [3.1, 2.8, 2.5],
    "ram_used_gb": 41.2,
    "ram_total_gb": 64,
    "disks": {
      "renders": {"used_gb": 320, "total_gb": 512},
      "avatar_cache": {"used_gb": 88, "total_gb": 200},
      "uploads": {"used_gb": 12, "total_gb": 100}
    },
    "gpus": [
      {"index": 0, "name": "RTX 4090", "util_percent": 91,
       "mem_used_mb": 21500, "mem_total_mb": 24576, "temp_c": 71}
    ],
    "current_job": "#1042 ao_thun_trang__ASK_PRICE",
    "last_backup_at": "2026-08-21 12:00"
  }'
```

`name` ở đây là tên **hiển thị** trên dashboard (vd "Render-01") — khác hẳn
`node_id` nội bộ (chuỗi ngẫu nhiên gắn với token), đổi `name` thoải mái không
ảnh hưởng gì tới token/danh tính node. `gpus` là mảng — nhiều GPU trên 1 máy
thì liệt kê đủ từng cái, không gộp chung. `disks`/`current_job`/
`last_backup_at` có thể `null` nếu không có.

### 2. `POST /api/ingest/node/jobs` — hàng đợi render

Toàn bộ job hiện tại của node (không phải lịch sử đầy đủ — chỉ cần đủ để
hiện bảng job gần đây + đếm theo trạng thái).

```bash
curl -X POST http://<dashboard>/api/ingest/node/jobs \
  -H "X-Ingest-Token: <token>" -H "Content-Type: application/json" -d '{
    "counts": {"queued": 4, "rendering": 1, "done": 128, "failed": 3},
    "recent": [
      {"id": 1042, "name": "ao_thun_trang__ASK_PRICE", "status": "rendering",
       "model": "musetalk / 512", "priority": 5, "retries": 0, "error": null,
       "started_at": "2026-08-21 14:02", "finished_at": null},
      {"id": 1038, "name": "tui_xach_da__ASK_QUALITY", "status": "failed",
       "model": "latentsync / 512", "priority": 0, "retries": 2,
       "error": "CUDA out of memory",
       "started_at": "2026-08-21 12:10", "finished_at": "2026-08-21 12:15"}
    ]
  }'
```

`status` chỉ nhận 1 trong 4 giá trị: `queued` / `rendering` / `done` /
`failed`. Job đã upload Google Drive xong thì thêm `drive_link`; lỗi upload
thì thêm `drive_error` — cả 2 field này optional, không có thì bỏ qua.

### 3. `POST /api/ingest/node/tts` — hàng đợi TTS

Tách riêng khỏi hàng đợi render (Excel enqueue vào đây trước, worker gọi TTS
xong mới tạo job render).

```bash
curl -X POST http://<dashboard>/api/ingest/node/tts \
  -H "X-Ingest-Token: <token>" -H "Content-Type: application/json" -d '{
    "counts": {"pending": 3, "submitting": 1, "retry_wait": 2, "done": 640,
               "failed_retryable": 4, "failed_permanent": 1},
    "recent": [
      {"id": 5200, "text": "Giày thể thao size 42 còn hàng không shop?",
       "provider": "vbee", "voice_id": "hn_female_ngochuyen_full_48k-fhg",
       "status": "retry_wait", "attempt_count": 2,
       "last_error": "HTTP 429 Too Many Requests",
       "created_at": "2026-08-21 14:05", "render_job_id": null}
    ],
    "rate_limits": [
      {"provider": "vbee", "throttled": true, "note": "Gặp 429 gần đây — đang giãn nhịp request"}
    ]
  }'
```

`status`: `pending` / `submitting` / `retry_wait` / `done` /
`failed_retryable` / `failed_permanent`.

### 4. `POST /api/ingest/node/stats` — thống kê riêng node

Node tự tổng hợp lịch sử job của **chính nó** (ngày/tuần/tháng/năm + 14 ngày
gần nhất) rồi gửi lên — dashboard chỉ **cộng dồn qua mọi node** ở
`/api/stats`, không tự suy luận gì thêm. Không gửi mục này thì node đó không
đóng góp vào thống kê tổng, và trang thống kê riêng node sẽ báo 404.

```bash
curl -X POST http://<dashboard>/api/ingest/node/stats \
  -H "X-Ingest-Token: <token>" -H "Content-Type: application/json" -d '{
    "today": {"done": 31, "failed": 1},
    "this_week": {"done": 210, "failed": 6},
    "this_month": {"done": 890, "failed": 24},
    "this_year": {"done": 9400, "failed": 310},
    "daily_trend": [
      {"date": "20/08", "done": 28, "failed": 2},
      {"date": "21/08", "done": 31, "failed": 1}
    ]
  }'
```

`daily_trend` nên giữ đúng 14 ngày gần nhất, `date` định dạng `dd/mm`.

### 5. `POST /api/ingest/node/batches` — % hoàn thành batch Excel

Gọi 1 lần cho **mỗi batch** node này có xử lý. `total/done/failed/pending`
chỉ tính trên phần batch đó **giao cho node này** — không phải tổng cả file.
Dashboard tự cộng dồn qua mọi node theo `batch_name`, ra đúng tổng thật dù 1
batch do 1 hay nhiều node cùng xử lý.

```bash
curl -X POST http://<dashboard>/api/ingest/node/batches \
  -H "X-Ingest-Token: <token>" -H "Content-Type: application/json" -d '{
    "batch_name": "Thang8_DotB_TuiXach.xlsx",
    "total": 80, "done": 65, "failed": 3, "pending": 12
  }'
```

Batch có 200 dòng, node A (token A) xử lý 80 dòng đầu và node B (token B) xử
lý 120 dòng còn lại → mỗi node tự gọi endpoint này (bằng token của chính nó)
với `total` là phần CỦA RIÊNG NÓ (80 và 120), dashboard cộng lại ra đúng 200.

## Node online/offline hoạt động thế nào

Dashboard **không** nhận field `online` — tự tính bằng thời điểm nhận request
`POST /api/ingest/node` gần nhất (theo token, tức theo node). Quá
`ONLINE_THRESHOLD_SEC` (mặc định 30s, chỉnh trong `dashboard/backend/.env`)
không thấy push mới → tự chuyển offline, không cần báo riêng.

→ **Push tài nguyên máy dày hơn ngưỡng đó** (khuyến nghị mỗi 10s) để node
luôn hiện đúng trạng thái, không bị nhấp nháy online/offline giả.

## Tần suất đẩy khuyến nghị

| Endpoint | Tần suất |
|---|---|
| `node` (tài nguyên máy) | 10s |
| `node/jobs` | 10–15s, hoặc ngay khi trạng thái job đổi |
| `node/tts` | 10–15s |
| `node/stats` | 1–5 phút (không cần realtime) |
| `node/batches` | mỗi khi tiến độ batch đổi, hoặc mỗi vài phút |

## Xử lý lỗi

Mọi endpoint trả `{"ok": true}` khi thành công. Lỗi thường gặp:

- `401` — thiếu/sai `X-Ingest-Token`, hoặc token chưa từng được tạo (xem mục
  Đăng ký node)
- `422` — sai shape body (thiếu field bắt buộc, sai kiểu dữ liệu) — response
  kèm chi tiết field nào sai
- Mạng lỗi/timeout/dashboard restart giữa chừng — bỏ qua lần đó, push tiếp ở
  chu kỳ sau (đừng để 1 lần lỗi làm chết cả vòng lặp agent)

## Tài liệu tương tác

`http://<dashboard>/docs` — Swagger UI, có sẵn ví dụ mẫu điền trước cho từng
endpoint, bấm "Try it out" để test trực tiếp từ trình duyệt. Nguồn chân lý
chính xác nhất về shape từng field: `dashboard/backend/app/models/schemas.py`.
