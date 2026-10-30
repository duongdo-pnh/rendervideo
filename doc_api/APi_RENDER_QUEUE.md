# Nối máy render vào hàng chờ akali

> **Job Fun-5B (ảnh nhân vật + video mẫu, `params.engine = "fun5b"`)** đi nhánh riêng, không Excel/TTS:
> xem [AKALI_FUN5B.md](AKALI_FUN5B.md). Từ 31/10/2026 `/claim` gửi kèm `engines` (§4 của file đó).

Hướng dẫn dựng `akali_bridge.py` trên **máy render** để nó tự lấy job từ hàng chờ
của akali, chạy hết một batch Excel, rồi trả link thư mục Drive về.

> **Hợp đồng API do phía akali giữ, nằm ở `doc_api/doc-api-md`** (bản 21/09/2026:
> 6 endpoint, có `/cancel` và `GET /jobs?worker_id=`, lease 1800s, `progress` là
> phần trăm 0-100). File này là góc nhìn **phía máy render**: code thật trong repo
> này làm gì, và những chỗ bản akali mô tả đã cũ so với repo.
>
> Bản akali viết dựa trên snapshot `feat/update-3080ti`, nên mục 2 và 8.1 của nó
> còn nói `process_excel(default_video=...)` số ít và "LatentSync không vào được
> hàng chờ". Cả hai **đã khác** trên repo này — xem mục 2.1 và 7.1 dưới đây. Đừng
> sửa code ngược lại cho khớp bản đó.

> **Đọc mục 1 trước khi làm bất cứ gì.** Tài liệu này viết dựa trên một bản
> `rendervideo` cụ thể. Bản trên máy render **khác bản đó**, nên mọi thứ ở đây
> phải được kiểm chứng lại trên máy thật trước khi tin.

---

## 1. Mốc đối chiếu

Tài liệu viết dựa trên:

| | |
|---|---|
| Repo | `rendervideo` |
| Branch | `feat/reroll-code-build-app` |
| Commit | `49eb064` — *"fix: replace CRF encoding with targeted bitrate, always show queue file grouping"* (05/09/2026) |
| Máy | máy dev, **không có model weights** |

Vì bản trên máy render khác, tài liệu **không dùng số dòng** ở bất cứ đâu — chỉ
dùng tên hàm và lệnh `grep`. Nếu một lệnh kiểm tra ở mục 2 không ra kết quả như
mô tả, dừng lại và đối chiếu lại thay vì sửa bừa.

---

## 2. Kiểm tra bản trên máy render

Chạy tuần tự ở thư mục gốc `rendervideo`. Cột cuối là việc phải làm nếu lệch.

### 2.1 Các hàm bridge sẽ gọi

```bash
grep -n "def process_excel\|def submit_jobs\|def export_results" excel_import.py
grep -n "def status_counts\|def results_for_batch\|def save_batch\|def get_batch_excel" tts_db.py
grep -n "def add_job\|def worker_busy\|^MODELS" database.py
grep -n "def ensure_folder\|def default_folder_id\|def upload_file\|def drive_enabled" google_drive_upload.py
```

Kỳ vọng — chữ ký phải khớp, đặc biệt là tham số:

| Hàm | Chữ ký kỳ vọng |
|---|---|
| `process_excel` | `(excel_path, default_videos=None, default_provider=None, shopee_item_id=None)` |
| `submit_jobs` | `(rows, shopee_item_id=None, progress=None, batch_id=None, excel_path=None, render_config=None, dedup=False)` |
| `results_for_batch` | `(batch_id)` → dict `{excel_row: {tts_status, render_job_id, render_status, output_path, name}}` |
| `ensure_folder` | `(service, name, parent_id)` → folder_id |

**`process_excel` nhận `default_videos` (số nhiều), không phải `default_video`.**
Đây là list các `{"name": <tên file gốc không đuôi>, "path": <đường dẫn video>}`.
Với **đúng 1 video** trong list (đúng hợp đồng ở mục 4), hàm nội bộ
`_resolve_avatar()` round-robin về đúng video đó cho mọi dòng — hành vi giống hệt
`default_video` cũ. Bridge chỉ cần bọc video vào list 1 phần tử:
```python
default_videos = [{"name": video_path.stem, "path": str(video_path)}]
```
**Nếu bản trên máy render vẫn dùng `default_video` (số ít, không phải list)**:
đó là bản cũ hơn, bridge phải truyền thẳng path chứ không bọc list.

**Nếu `submit_jobs` không có `render_config`**: cấu hình render từ akali không áp
được cho batch. Đây là điều kiện bắt buộc, không đi tiếp được nếu thiếu.

### 2.2 Thư mục Drive của batch

```bash
grep -n "drive_folder" tts_worker.py
```

Kỳ vọng: `drive_folder` = **tên file Excel bỏ đuôi**, và được truyền vào
`db.add_job(...)`. Đây là cơ sở để dựng link trả về akali.

**Nếu bản kia đặt tên thư mục theo cách khác** (theo ngày, theo batch_id...),
sửa lại mục 5 bước 8 cho khớp — bridge phải dựng link đúng thư mục thật.

### 2.3 Engine có bị ghim không

```bash
grep -rn "INSERT INTO jobs" --include="*.py" .
grep -n 'def add_job' -A 10 database.py
```

Trên bản đối chiếu (đã đổi so với bản `feat/update-3080ti` cũ):

- `INSERT INTO jobs` xuất hiện **đúng một lần**, trong `add_job`
- `add_job(name, video_path, audio_path, model_res, ..., engine="musetalk")` —
  `engine="musetalk"` chỉ là **giá trị mặc định của tham số**, không còn dòng
  ghi đè vô điều kiện. Hàm validate `engine in ("musetalk", "latentsync")` và
  **raise nếu khác** — không còn âm thầm ép về musetalk.

Nghĩa là **LatentSync đã vào hàng chờ được**, không còn bẫy như bản cũ. Xem lại
mục 7.1 — bẫy đó đã được gỡ trên bản này.

**Đường đi engine**: `render_config={"engine": "latentsync", ...}` truyền vào
`submit_jobs` → lưu JSON trong cột `render_config` của `tts_jobs` → `tts_worker.py`
`json.loads` ra `cfg` → `db.add_job(..., **cfg)`. Bridge cứ truyền `engine` xuống
qua `render_config`, không cần sửa gì thêm ở tầng này.

Nếu bản trên máy render **vẫn còn** dòng `engine = "musetalk"` ghi đè vô điều
kiện trong `add_job` → đó là bản cũ hơn bản đối chiếu này, LatentSync chưa vào
được hàng chờ, áp dụng lại mục 7.1 bản cũ (đổi `engine = "musetalk"` thành
`engine = engine if engine in ("musetalk", "latentsync") else "musetalk"`).

### 2.4 Model weights

```bash
ls -la checkpoints/ 2>&1
ls -la engines/MuseTalk/models/ 2>&1
```

Cần có:

| Engine | File |
|---|---|
| LatentSync 512 | `checkpoints/latentsync_unet.pt` |
| LatentSync 256 | `checkpoints/v1.5/latentsync_unet.pt` |
| MuseTalk | `engines/MuseTalk/models/musetalkV15/unet.pth`, `sd-vae/`, `whisper/`, `face-parse-bisent/` |

Trên máy dev **cả hai đều thiếu** — đó là lý do phải kiểm tra lại trên máy render.
Thiếu weight thì bridge chạy đúng nhưng render luôn hỏng.

### 2.5 Tham số của render_job

```bash
python3 render_job.py --help
```

Kỳ vọng có: `--guidance --steps --seed --enhance_mouth --enhance_region
--out_res --input_type --engine` (còn có thêm `--color_correct`, mặc định bật,
bridge không cần quan tâm — do `queue_worker` tự truyền theo giá trị mặc định).

---

## 3. Hợp đồng API của akali

Bridge chỉ cần 4 endpoint này, không phải đọc source akali.

- Base URL: `http://<IP akali>:8000`
- Xác thực: `Authorization: Bearer <WORKER_API_KEY>` cho **mọi** request

### 3.1 Xin việc

```
POST /api/worker/claim
{"worker_id": "render-01"}
```

`204 No Content` = chưa có việc, ngủ rồi thử lại.
`200` = nhận job:

```json
{
  "id": 78,
  "drive_url": "https://drive.google.com/drive/folders/XXXX",
  "drive_file_id": "XXXX",
  "claim_token": "72ee72d8-144c-4eb5-800c-7ddd404be487",
  "lease_seconds": 600,
  "attempts": 1,
  "max_attempts": 3,
  "params": {
    "engine": "musetalk",
    "input_type": "real",
    "model_res": "256",
    "out_res": "720",
    "guidance": 1.5,
    "steps": 24,
    "seed": 1247,
    "enhance_mouth": 1,
    "enhance_region": "mouth",
    "tts_provider": "autovoice",
    "voice_id": "pv-f940f724-49c9-4b0c-8096-c4f10e27c5db"
  }
}
```

**Hành động claim chính là tín hiệu "máy đang rảnh".** akali không có endpoint
báo rảnh riêng, và cũng không cần — chỉ claim khi thật sự sẵn sàng chạy.

### 3.2 Giữ nhịp

```
POST /api/worker/jobs/<id>/heartbeat
{"claim_token": "...", "progress": 40}
```

`409` = **mất lease**, akali đã thu hồi job và có thể đã giao máy khác. Dừng
ngay, không được gọi `/complete` nữa.

`lease_seconds` mặc định 600. Heartbeat 60s/lần là dư biên.

### 3.3 Báo xong

```
POST /api/worker/jobs/<id>/complete
{"claim_token": "...", "output_url": "https://drive.google.com/drive/folders/YYYY"}
```

`output_url` là **link thư mục** chứa toàn bộ video của batch.

### 3.4 Báo hỏng

```
POST /api/worker/jobs/<id>/fail
{"claim_token": "...", "error": "...", "retryable": true}
```

`retryable: false` → job về `failed` ngay. `true` → về `pending` kèm backoff,
tối đa `max_attempts` lần.

---

## 4. Quy ước dữ liệu đầu vào

Folder Drive akali gửi xuống chứa:

- **đúng 1 file video** — avatar dùng chung cho cả batch
- **đúng 1 file `.xlsx`** — danh sách dòng cần render

Cột `video_path` trong Excel **để trống** (video đã truyền qua `default_videos`,
xem mục 2.1).

Cột `tts_provider` và `tts_voice`: **giá trị thật trong Excel bị bỏ qua** — voice
do akali quyết định, một giọng cho cả batch, bridge đè lại ở bước 8. **Nhưng
không được để Ô TRỐNG.** `process_excel()` validate hai cột này **trước khi**
bridge kịp đè: để trống thì dòng đó bị đẩy vào `errors` ngay tại bước đọc Excel
(`"tts_provider rỗng"` / `"tts_voice rỗng"`), không bao giờ tới được bước 8 để
override. Bridge (hoặc template Excel akali dùng) phải điền sẵn **giá trị bất kỳ
khác rỗng** ở hai cột này (ví dụ `autovoice` / `placeholder`) — giá trị thật
không quan trọng vì sẽ bị ghi đè, chỉ cần không trống. Xem mục 5 bước 5–8.

---

## 5. Luồng bridge

```
 1. rảnh?            → db.worker_busy() false, không có batch running
 2. POST /claim      → 204 thì ngủ; có job thì đi tiếp
 3. idempotency      → akali_job_id đã có trong akali_batches? → theo dõi tiếp, KHÔNG tạo mới
 4. tải folder Drive → workdir/<akali_job_id>/
 5. nhận diện file   → đúng 1 .xlsx + 1 video; sai → /fail retryable=false
 6. đổi tên Excel    → <stem>__job<id>.xlsx
 7. process_excel(xlsx, default_videos=[{"name": ..., "path": <video>}])
 8. đè voice         → mọi dòng
 9. submit_jobs(rows, batch_id, excel_path, render_config)
10. heartbeat 60s    → progress = dòng xong / tổng dòng
11. phát hiện xong   → mọi dòng done/failed VÀ đã có drive_link/drive_error
12. export_results   → upload Excel kết quả lên cùng thư mục
13. POST /complete   → link thư mục Drive
```

### Bước 6 — vì sao phải đổi tên Excel

Thư mục con trên Drive đặt theo tên file Excel. Hai batch dùng file trùng tên
(ví dụ khách nào cũng gửi `danh_sach.xlsx`) sẽ **đổ chung một thư mục**, và link
trả về cho batch sau sẽ chứa cả video của batch trước.

Chèn `akali_job_id` vào tên file là cách rẻ nhất, không phải sửa code rendervideo.

### Bước 8 — đè voice

```python
for r in rows:
    r["tts_provider"] = params["tts_provider"]
    r["tts_voice"]    = params["voice_id"]
```

`submit_jobs` đọc hai khoá này từ dict nên **không cần sửa `excel_import.py`**.

### Bước 9 — ánh xạ cấu hình

| `params` từ akali | khoá `render_config` |
|---|---|
| `model_res` | `model_res` |
| `out_res` | `out_res` |
| `guidance` | `guidance` |
| `steps` | `steps` |
| `seed` | `seed` |
| `enhance_mouth` | `enhance_mouth` |
| `enhance_region` | `enhance_region` |
| `input_type` | `input_type` |
| `engine` | `engine` — `"musetalk"` hoặc `"latentsync"`, đã dùng được thẳng (mục 2.3/7.1) |
| `tts_provider`, `voice_id` | **không** qua đây — đè vào `rows` ở bước 8 |

### Bước 11 — vì sao phải đợi `drive_link`

`_start_drive_upload()` chạy ở **thread nền sau** `mark_done`. Thấy
`render_status='done'` mà báo ngay thì video còn chưa lên Drive, link trả về sẽ
thiếu file. Phải đợi mỗi job có `drive_link` **hoặc** `drive_error`.

### Bước 13 — dựng link thư mục

```python
service = gdrive.get_service()
fid = gdrive.ensure_folder(service, excel_stem, gdrive.default_folder_id())
url = f"https://drive.google.com/drive/folders/{fid}"
```

`upload_file()` chỉ trả `webViewLink` của **từng file**, không trả folder id —
nên phải gọi `ensure_folder` riêng.

---

## 6. Bảng trạng thái của bridge

Tạo trong chính file SQLite mà `database.py` / `tts_db.py` đang dùng:

```sql
CREATE TABLE IF NOT EXISTS akali_batches (
    akali_job_id  INTEGER PRIMARY KEY,
    claim_token   TEXT NOT NULL,
    batch_id      TEXT NOT NULL,
    excel_stem    TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'running',  -- running | reported | abandoned
    created_at    TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
```

Bảng này là thứ khiến bridge **restart được mà không mất batch**. Thiếu nó, bridge
chết giữa chừng thì reaper của akali thu hồi job và cả batch render lại từ đầu.

`akali_job_id` là khoá chính → khi akali retry cùng job, bridge nhận ra và nối
tiếp thay vì dựng batch mới.

---

## 7. Bẫy đã biết

### 7.1 LatentSync — đã vào được hàng chờ (bẫy cũ đã gỡ)

**Cập nhật so với bản `feat/update-3080ti` cũ**: bản đối chiếu hiện tại
(`feat/reroll-code-build-app` @ `49eb064`) **không còn** ghi đè `engine =
"musetalk"` vô điều kiện trong `add_job`. `engine="musetalk"` giờ chỉ là default
của tham số, hàm validate `engine in ("musetalk", "latentsync")` và raise nếu
khác — LatentSync đi thẳng qua `render_config` là vào được hàng chờ (xem mục 2.3
cho đường đi cụ thể qua `submit_jobs` → `tts_jobs.render_config` → `tts_worker`).

`queue_worker` đã có nhánh `else` gọi `trim_video_to_audio`, truyền `--engine`
xuống `render_job.py`, và `config_path`/`checkpoint_path` (từ `MODELS` trong
`database.py`) vốn đã trỏ checkpoint LatentSync theo `model_res`.

**Trước khi bridge gửi `engine=latentsync` xuống thật**: vẫn phải kiểm tra
`checkpoints/latentsync_unet.pt` (hoặc `checkpoints/v1.5/...` tuỳ `model_res`)
có trên máy render không (mục 2.4), và chạy tay một lần `render_job.py --engine
latentsync` với video + audio thật — thiếu weight thì job vào hàng chờ bình
thường nhưng **render hỏng ở bước gọi model**, không phải hỏng ở tầng hàng chờ.

**Nếu bản trên máy render VẪN còn** dòng `engine = "musetalk"` ghi đè vô điều
kiện trong `add_job` → đó là bản cũ hơn bản đối chiếu này (kiểu
`feat/update-3080ti`), bẫy này còn nguyên: LatentSync không vào hàng chờ được,
bridge nhận `engine=latentsync` phải `/fail` với lý do rõ ràng thay vì âm thầm
chạy MuseTalk. Muốn mở trên bản đó, đổi dòng ghi đè thành:

```python
engine = engine if engine in ("musetalk", "latentsync") else "musetalk"
```

### 7.2 Guidance và Steps bị pipeline ép

`render_job.render()` kẹp lại giá trị trước khi chạy:

| `input_type` | Guidance | Steps |
|---|---|---|
| `real` | ≤ 1.5 | ≥ 24 |
| `ai` | ≤ 1.3 | ≥ 28 |

Gửi `guidance=2.5` xuống thì thực tế chạy 1.5, **không báo lỗi**. Dashboard akali
đang cho kéo 1.0–3.0 và 8–50, rộng hơn thực tế nhiều.

### 7.3 `model_res` chỉ có nghĩa với LatentSync

`MODELS` map `256`/`512` sang **checkpoint LatentSync**. `musetalk_render()`
không nhận config/checkpoint, nên hai cột đó chạy không khi engine là MuseTalk.

### 7.4 Retry hai tầng — không xung đột

- rendervideo retry **từng video** (`MAX_RETRIES`, `tts_db.schedule_retry`)
- akali retry **cả batch** (`attempts` / `max_attempts`)

Khác hạt nên không đá nhau. Điều duy nhất phải lo là akali retry batch thì đừng
render lại từ đầu — đã xử bằng khoá idempotency ở mục 6.

### 7.5 Đừng claim sớm

akali bấm giờ lease **ngay lúc `/claim`**. Máy render một GPU, chạy một job một
lúc. Claim rồi xếp hàng chờ là lease cháy trong lúc chờ. Luôn kiểm tra
`db.worker_busy()` trước.

### 7.6 Batch bị bỏ quên nếu bridge restart giữa chừng (đã vá)

Gặp thật (17/09/2026, job #80): bridge claim job, batch render xong hết 10/10,
video lên Drive đủ — nhưng chưa kịp gọi `/complete` thì bridge bị khởi động lại
(app đóng/mở). Restart xong, vòng lặp chính chỉ gọi `/claim` cho job MỚI —
job cũ vẫn `status='running'` trong `akali_batches` nhưng không ai chủ động
quay lại theo dõi, vì bridge chỉ nối lại một batch cũ **khi akali tình cờ cấp
lại đúng job đó qua `/claim`** (mục 6 nguyên bản). Nếu lease job cũ chưa hết
hạn, akali không có lý do cấp lại — job đó bị bỏ quên vĩnh viễn, dù việc render
đã xong xuôi. Tới lúc phát hiện, lease đã hết hạn từ lâu, gọi `/complete` tay
nhận `409 Conflict`.

**Vá**: `main()` giờ gọi `_resume_pending_batches()` ngay sau khi khởi động —
quét `akali_batches` lấy mọi dòng `status='running'`, chủ động dùng
`claim_token` đã lưu để tiếp tục heartbeat/complete, không chờ akali cấp lại.
Nếu lease đã hết hạn thật, heartbeat đầu tiên nhận 409 (`lost_lease`) và bỏ qua
ngay, không lặp vô hạn.

---

## 8. Cấu hình

Biến môi trường cho bridge:

```bash
AKALI_URL=http://10.10.11.103:8000
AKALI_WORKER_KEY=<WORKER_API_KEY của akali>
AKALI_POLL_SECONDS=10
AKALI_HEARTBEAT_SECONDS=60
AKALI_WORKDIR=/var/tmp/akali_batches
```

`worker_id` gửi cho akali (mục 3.1) **không phải biến riêng** — dùng lại `NODE_NAME`
của Dashboard Fleet (`dashboard_reporter.node_name()`), trống thì fallback hostname.
Một máy chỉ đặt tên một chỗ (UI: tab "Dashboard Fleet"; tab "Kết nối akali" chỉ
hiện lại giá trị đang dùng, không sửa được ở đó).

**`akali_bridge.py` tự khởi động cùng backend** — đăng ký trong
`backend/workers.py`'s `_WORKER_SCRIPTS` (từ v1.2.6), giống `queue_worker.py`/
`tts_worker.py`. An toàn spawn vô điều kiện vì `main()` tự thoát ngay nếu thiếu
`AKALI_URL`/`AKALI_WORKER_KEY`. Log ở `logs/akali_bridge.log`. **Bug lịch sử**:
bản v1.2.5 build ra có route `/api/akali/config` để lưu cấu hình nhưng thiếu
đúng dòng đăng ký này — lưu được URL/key nhưng không có tiến trình nào thật sự
chạy để dùng chúng, đã vá ở v1.2.6.

Chuẩn bị kèm theo:

1. **Đổi `WORKER_API_KEY` trên akali.** Mặc định trong `.env.example` là
   `change-me-worker`. Ai chạm được cổng 8000 là claim và đổi trạng thái job được.
2. **Đường mạng.** akali bind `0.0.0.0:8000`. Cùng LAN thì trỏ IP thẳng; khác
   mạng thì cần tunnel hoặc domain.
3. **Credential Drive.** Bridge phải **đọc** được folder akali giao. Service
   account mà `rendervideo` đang dùng để upload chưa chắc được share folder
   nguồn — kiểm tra riêng.
4. **Nâng `LEASE_SECONDS`** trên akali từ 600 lên ~1800, để bridge restart mà job
   không bị thu hồi ngay.

---

## 9. Nghiệm thu theo giai đoạn

Mỗi giai đoạn chạy thật được, không phải chờ xong hết mới thử.

**Giai đoạn 1 — claim và tải**
- [ ] Nạp job trên dashboard akali, bridge nhận được trong vòng một nhịp poll
- [ ] Folder tải về đủ file, nhận diện đúng 1 Excel + 1 video
- [ ] Folder sai cấu trúc → job về `failed`, lý do đọc được trên dashboard

**Giai đoạn 2 — dựng batch và giữ nhịp**
- [ ] `tts_jobs` có đủ số dòng của Excel
- [ ] Mọi dòng dùng đúng `voice_id` akali gửi, kể cả dòng Excel có ghi voice khác
- [ ] Tiến độ trên dashboard nhích dần
- [ ] Tắt bridge giữa chừng rồi bật lại → nối tiếp đúng batch cũ, không dựng batch mới

**Giai đoạn 3 — báo xong**
- [ ] Job về `done`, bấm link ra đúng thư mục chứa đủ video
- [ ] Excel kết quả có cột `video_done`, nằm cùng thư mục
- [ ] Một vài dòng lỗi → job vẫn `done`, dòng hỏng thể hiện trong Excel
- [ ] Không dòng nào ra video → job về `failed`

---

## 10. Việc chưa làm

- `akali_bridge.py` **đã viết xong** (repo root), theo đúng luồng 13 bước ở mục 5:
  khoá single-instance kiểu `queue_worker.py` (`.akali_bridge.lock`), bảng
  `akali_batches` tự tạo (mục 6), và tự điền placeholder không rỗng vào 2 cột
  `tts_provider`/`tts_voice` trước khi gọi `process_excel` (mục 4 — cột này bắt
  buộc không trống dù giá trị thật bị đè ở bước 8). Có test logic thuần ở
  `tests/test_akali_bridge.py` (file identification, row-completion, placeholder
  fill + `process_excel` thật) — **chưa test HTTP thật với akali** vì chưa có
  `AKALI_URL`/`AKALI_WORKER_KEY` để trỏ vào.
- `google_drive_upload.download_folder(folder_id, dest_dir)` **đã thêm**, tái
  dùng `get_service()`/`list_files()` sẵn có (không đệ quy thư mục con — đúng
  hợp đồng mục 4). Đã test tải thật 1 file ~27MB từ Drive, so khớp size với API.
- UI tab **"Kết nối akali"** đã thêm (backend `backend/akali.py`, frontend
  `pages/akali/AkaliPage.tsx`) — chỉ 2 ô AKALI_URL/Worker Key, không có ô
  worker_id (dùng lại tên máy của tab Dashboard Fleet, xem mục 8). Không có nút
  "kiểm tra kết nối" thật, chỉ lưu `.env` — cùng lý do claim() có tác dụng phụ ở
  mục 3.1. Đã test qua trình duyệt thật (điền, lưu, thấy trạng thái đúng).
- **Đã chạy trọn vòng thật** (17/09/2026): job #81 và #82 đi hết claim → tải
  folder → TTS → render → upload từng video → xuất + upload Excel kết quả →
  `/complete`, kết thúc ở `akali_batches.status = 'reported'`. Kiểm chứng trên
  Drive: mỗi thư mục batch có đủ 10 video + 1 file `_ketqua.xlsx`. 4 việc chuẩn
  bị phía akali/vận hành (đổi `WORKER_API_KEY`, share Drive, nâng lease lên
  1800, điền `.env`) đều đã xong.
- Đơn vị `progress` **đã chốt là phần trăm 0-100** (akali xác nhận ở mục 11 của
  `doc-api-md`: backend ghi thẳng vào cột, dashboard vẽ `width: ${progress}%`).
  Backend không kẹp giá trị nên bridge tự kẹp trong `heartbeat()`.

### Đồng bộ huỷ job — đã cài (21/09/2026)

Theo spec ở mục 3.5/3.6 và mục 4 của `doc-api-md`:

| Việc | Cài ở đâu |
|---|---|
| Đọc `reason` trong body 409 | `akali_bridge._conflict_reason()` |
| `reason=canceled` → 6 bước dọn | `akali_bridge._abandon_batch()` |
| Xoá dòng chưa chạy khỏi hàng chờ | `tts_db.delete_unstarted_in_batch()` |
| Dừng render đang chạy + bắt job mồ côi | `database.request_cancel_by_drive_folder()` |
| Huỷ ở máy render → `/cancel` (không `/fail`) | `akali_bridge.cancel()` |
| Đối soát `GET /jobs?worker_id=` | `akali_bridge._reconcile()`, chạy trước claim |

Ba điểm dễ làm sai, ghi lại để khỏi sửa hỏng về sau:

1. **Xoá `tts_jobs` không đủ.** `tts_worker` gọi `db.add_job()` TRƯỚC
   `mark_done()`, nên xoá dòng đúng lúc nó kẹt giữa hai lệnh sẽ để lại render job
   "mồ côi" — không còn dòng nào trỏ tới, nhưng GPU vẫn chạy và video vẫn lên
   Drive. Phải quét thêm bảng `jobs` theo `drive_folder` (= `<stem>__job<id>`,
   duy nhất cho mỗi job akali), và quét **lại lần nữa** sau ~15s để bắt job sinh
   muộn.
2. **Đánh dấu `abandoned` TRƯỚC khi dọn.** Chết giữa chừng thì batch đã bị loại
   khỏi danh sách heartbeat vĩnh viễn; làm ngược lại là kẹt `running` mãi.
3. **Không SIGKILL tiến trình render.** Dùng cờ `cancel_requested` để
   `queue_worker` tự dừng subprocess của nó rồi `mark_cancelled()`.

Còn lại: nghiệm thu giai đoạn 4 (mục 10 của `doc-api-md`) trên hệ thống thật —
bấm Huỷ giữa lúc đang render rồi kiểm `nvidia-smi` + `tts_jobs` + Drive.
