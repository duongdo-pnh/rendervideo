# Kế hoạch tích hợp Fun-5B (từ repo temo) vào rendervideo

> Trạng thái (30/10/2026): **bước 0–3 đã làm xong trên máy render, chưa commit, chưa build `.deb`**. Bước 4 (Akali) và 5 (installer) chưa làm.
>
> **Khác so với bản nháp:**
> - `GET /api/jobs/engine-choices` **không** thêm fun5b. Form lip-sync giữ nguyên; fun5b chỉ có trên trang riêng.
> - Xem trước mép bàn được vẽ ngay trên trình duyệt (khung 640:1152, `object-cover`), nên **không có** endpoint `table-preview`.
> - Thêm 2 endpoint: `POST /api/engines/fun5b/plates/lookup` (ảnh này đã có nền sạch chưa) và `GET /api/jobs/{id}/ref-image` (ảnh thu nhỏ trong hàng đợi).
> - UI **không** hiện trạng thái server và **không** có nút "Tắt server": server tự bật theo job và tự tắt khi engine khác cần GPU. Endpoint `/api/engines/fun5b/server` (xem trạng thái) và `/server/stop` (tắt) vẫn còn để dùng khi cần.
> - Form **không** có ô upload ảnh nền sạch: nền luôn tự động (dùng nền đã lưu, chưa có thì FireRed vẽ). API vẫn nhận `background` để dùng cho Akali sau này.
> - Tắt server fun5b trước khi chạy MuseTalk/LatentSync bằng `fun5b_adapter.ensure_stopped()`: tắt rồi nhả khoá ngay, để không ép cặp job MuseTalk chạy lần lượt.
> - Lỗi đầu vào (không tìm thấy người, thiếu model) thoát với exit code 3, và `queue_worker` đánh dấu failed luôn, **không retry**.
> Phạm vi: chỉ renderer `fun5b/` của temo (Wan2.2-Fun-5B-Control + FastWan LoRA). `wan_animate/` (14B) không chạy được trên 3090 nên không mang sang.

## 0. Các quyết định đã chốt

| # | Quyết định |
|---|---|
| 1 | **Audio:** giữ nguyên, video ra **câm** (Fun-5B vốn bỏ tiếng). |
| 2 | **Hai đường tạo job, cùng vào một hàng đợi:** (a) **UI của repo này**: một **tab mới** "Chuyển động nhân vật". Người dùng upload ảnh nhân vật + video mẫu, job vào hàng đợi của máy này giống cách tạo job hiện tại, render xong đẩy Drive. (b) **Akali**: Akali giao job fun5b (folder Drive chứa ảnh + video), máy render tải về, render, đẩy Drive rồi báo `complete`. |
| 3 | **FireRed** vẫn dùng để vẽ nền sạch. **Nền đã vẽ được lưu lại** và dùng lại cho các lần sau với cùng ảnh. |
| 4 | **Không xoá model.** Chỉ **tắt hoặc bật server** như đang làm với MuseTalk/LatentSync. |
| 5 | **Mang code fun5b từ temo sang repo này** (commit vào rendervideo). Repo này không phụ thuộc repo temo nữa. |
| 6 | Bố trí code, venv và model **theo đúng quy ước sẵn có** của repo. |

---

## 1. Tóm tắt Fun-5B (số đo thật trên RTX 3090 24 GB)

- **Vào:** 1 ảnh nhân vật + 1 video chuyển động. **Ra:** nhân vật trong ảnh làm theo động tác của video, giữ nền và ánh sáng của ảnh.
- **Output:** H.264 **720×1280 (9:16) @ 30fps**, không tiếng.
- **Tốc độ** (preset mặc định, 14 bước): clip 30s ≈ **26–28 phút**. Load model ~2 phút.
- **VRAM:** ~12.3 GB khi nghỉ, đỉnh ~20 GB. FireRed cần ~19 GB nên phải gỡ model video ra GPU trong lúc vẽ nền (~6.5 phút) rồi load lại (~2 phút).
- **RAM:** ~14 GB khi nghỉ, đỉnh ~22 GB (máy có 31 GB).
- **Huỷ:** kiểm tra sau mỗi bước denoise (~5s). Không có hiệu lực trong lúc tính pose và decode (có thể trễ tới ~1 phút).
- **Model:** 54 GB, đã có trên máy (trong `~/Desktop/Code/temo/ckpts`).

Preset (API dùng key ASCII, UI hiện tên tiếng Việt):

| key | Tên hiển thị | Steps | Ghi chú |
|---|---|---|---|
| `fast` | Nhanh | 8 | nhanh nhất |
| `balanced` | Cân bằng | 12 | |
| `natural` *(mặc định)* | Tự nhiên | 14 | ~28 phút / 30s |
| `natural_light` | Ánh sáng tự nhiên (chậm) | 25 | chậm hơn ~2.3 lần |

---

## 2. Bố trí trong repo (theo quy ước hiện có)

Quy ước hiện tại của repo:
- **Code của app** được commit ở root (`musetalk_adapter.py`, `musetalk_render_server.py`, `latentsync/`, ...).
- **Upstream ngoài** là checkout pin commit trong `engines/` (gitignored), có venv riêng (`engines/MuseTalk/.venv`).
- **Model** nằm ở `checkpoints/` hoặc `engines/<X>/models`, đều gitignored và không bị xoá khi update app.

Áp dụng cho Fun-5B:

```
rendervideo/
├── fun5b/                       ← MỚI, commit. Code mang từ temo/fun5b
│   ├── __init__.py
│   ├── config.py                ← thay cho wan_animate/config.py: đường dẫn lấy theo ROOT của app
│   ├── run.py                   ← pipeline + render()   (từ temo, sửa đường dẫn)
│   ├── pose.py  matting.py  facelock.py  plate.py
│   ├── vae_encode.py  vae_decode.py
│   └── edit_worker.py           ← FireRed, chạy trong .venv-edit
├── fun5b_render_server.py       ← MỚI, commit. Server socket thường trú (thay cho Gradio ui.py)
├── fun5b_adapter.py             ← MỚI, commit. Venv chính gọi server (giống musetalk_adapter.py)
├── install/fun5b-source.json    ← MỚI. Pin commit VideoX-Fun 4b7b640
├── install/setup_fun5b.sh       ← MỚI. Cài engine trên máy mới (tuỳ chọn)
├── install/download_fun5b_models.sh ← MỚI. Từ temo/scripts/fun-models.sh
│
├── engines/VideoX-Fun/          ← gitignored. Checkout upstream pin commit
├── engines/Fun5B/.venv          ← gitignored. Python 3.11, torch 2.8.0+cu128 (= .venv-fun cũ)
├── engines/Fun5B/.venv-edit     ← gitignored. diffusers 0.42 cho FireRed
├── checkpoints/fun5b/           ← gitignored. 54 GB model (= temo/ckpts)
└── fun5b_cache/                 ← gitignored. Cache pose + nền sạch (không bị dọn tự động)
    ├── poses/
    └── plates/
```

**Những chỗ phải sửa khi mang code sang** (code temo chạy được trên máy này rồi, chỉ đổi phần "đường dẫn và cách gọi"):

| Ở temo | Sang rendervideo |
|---|---|
| `from wan_animate import config` (ROOT, OUTPUT_DIR, FPS, DEFAULT_SEED, VIDEO_COLOR_PARAMS) | `fun5b/config.py` riêng. Bỏ hẳn phụ thuộc vào `wan_animate`. |
| `third_party/VideoX-Fun` và `sys.path.insert` | `engines/VideoX-Fun` (đường dẫn lấy từ config) |
| `ckpts/...` | `checkpoints/fun5b/...` |
| File tạm cố định `outputs/.fun5b_ref.png`, `.fun5b_control.mp4` | Đưa vào `work/<job_id>/` của queue_worker, mỗi job một thư mục |
| `outputs/.fun5b_pose_cache`, `.fun5b_plate_cache` | `fun5b_cache/poses`, `fun5b_cache/plates` |
| `ui.py` (Gradio: `generate`, `background_for`) | Logic này chuyển vào `fun5b_render_server.py`. **Không** mang Gradio sang. |
| Tên preset tiếng Việt làm key | Map key ASCII sang `PRESETS` |
| `.venv-edit/bin/python` trong `plate.py` | `engines/Fun5B/.venv-edit/bin/python` |
| Biến môi trường do `fun-ui.sh` đặt | Do adapter đặt khi spawn server: `PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True`, `MALLOC_MMAP_THRESHOLD_=1048576`, `TORCHINDUCTOR_COMPILE_THREADS=1`, `TORCHINDUCTOR_CACHE_DIR`/`TRITON_CACHE_DIR` trỏ vào `fun5b_cache/compile` |

**Trên máy này:** di chuyển (`mv`, cùng ổ nên xong ngay, không tải lại) `temo/ckpts`, `temo/.venv-fun`, `temo/.venv-edit` và `temo/third_party/VideoX-Fun` sang các vị trí trên trong `~/.local/share/byscom-render`. Cache cũ (18 pose và 8 nền sạch) cũng chuyển sang `fun5b_cache/`. Venv uv sau khi di chuyển vẫn chạy được nếu gọi thẳng `bin/python`. Nếu lỗi thì tạo lại bằng `setup_fun5b.sh`, model vẫn giữ nguyên. Repo dev trỏ tới các thư mục đó bằng symlink, giống cách đang làm cho checkpoints.
> Sau bước này **systemd unit `fun-ui` (Gradio :7862) của temo sẽ hỏng**, nên tắt và disable nó. Đồng thời nó cũng đang giữ ~12 GB VRAM.

---

## 3. Hai đường tạo job

### 3.0 Đường (a): tab mới trên UI máy này

```
[Tab "Chuyển động nhân vật"] upload ảnh + video mẫu (+ nền sạch tuỳ chọn), chọn preset, ...
     │  POST /api/jobs/animate  (multipart)
     ▼
job_files lưu vào uploads/<hex12>/  → db.add_job(engine="fun5b", drive_folder="<tên>__fun5b" hoặc theo ngày như job thường)
     ▼
queue_worker → render_job.py → fun5b_adapter → fun5b_render_server
     ▼
~/Desktop/Renders/<name>.mp4 → upload Drive (giống job MuseTalk/LatentSync hiện tại)
     ▼
Hiện trong tab Hàng đợi (có thanh tiến độ) và trang Video như mọi job khác
```

Đường này không cần Akali. Cách đặt tên file, Drive và tính năng ưu tiên/huỷ/retry đều dùng chung với job hiện tại.

### 3.0b Đường (b): Akali

```
[UI Akali] người dùng upload: ảnh nhân vật + video mẫu (+ chọn preset, ...)
     │  Akali đẩy lên Drive: 1 folder / job
     ▼
POST /api/worker/claim  ──►  akali_bridge.py (máy render)
     │  params.engine == "fun5b"
     │  tải folder Drive về /var/tmp/akali_batches/<job_id>/
     │  nhận diện file: đúng 1 ảnh + 1 video   (không có Excel, không có TTS)
     ▼
db.add_job(engine="fun5b", drive_folder="<tên video>__job<id>", ...)
     ▼
queue_worker → render_job.py → fun5b_adapter → fun5b_render_server
     │  (FireRed: lấy nền từ cache nếu có, chưa có thì vẽ rồi lưu lại)
     ▼
~/Desktop/Renders/<name>.mp4 → upload Drive vào folder "<tên video>__job<id>"
     ▼
akali_bridge: heartbeat trong suốt quá trình → POST /complete {output_url = link folder}
              lỗi → POST /fail {error, retryable}
```

### 3.1 Hợp đồng với Akali (cần bổ sung vào `doc_api/APi_RENDER_QUEUE.md`)

**Folder Drive của job:**

| File | Bắt buộc | Nhận diện |
|---|---|---|
| Ảnh nhân vật | ✔ (đúng 1) | `.png .jpg .jpeg .webp`, không phải `background.*` |
| Video mẫu | ✔ (đúng 1) | `.mp4 .mov .mkv .webm` (giống `_VIDEO_EXTS` hiện có) |
| Ảnh nền sạch | ✖ | tên file `background.<ext>`. Nếu có thì dùng luôn, bỏ qua FireRed. |

**`params` của job:**

```jsonc
{
  "engine": "fun5b",            // bắt buộc, dùng để rẽ nhánh
  "preset": "natural",          // fast | balanced | natural | natural_light
  "seed": 42,
  "keep_background": true,      // false = để model tự vẽ lại nền
  "auto_background": true,      // FireRed vẽ nền sạch (dùng cache nếu đã có)
  "table_line": null,           // 0..100 (% chiều cao), null = tự động
  "sharp_hands": false          // tay nét khi cử động nhanh (+10–25% thời gian)
}
```

**Kết quả:** `output_url` là link folder Drive chứa video. Mỗi job Akali ra **1 video**, không có Excel kết quả.

**Lỗi trả `retryable=false`:** folder sai số lượng file, không tìm thấy người trong ảnh/video, file hỏng.
**Lỗi `retryable=true`:** lỗi tải Drive, lỗi CUDA/OOM, server chết.

> Với đường (b), phía Akali phải tự có chỗ upload ảnh và video, rồi giao job theo hợp đồng trên. Repo này chỉ lo phần nhận job.

### 3.2 Sửa `akali_bridge.py`
- `_handle_job`: nếu `params.engine == "fun5b"` thì đi nhánh mới `_prepare_and_submit_fun5b`, nhánh Excel cũ giữ nguyên.
- `_identify_fun5b_files(folder)` trả về `(image, video, background|None)` và raise `BridgeFailure(retryable=False)` nếu folder sai.
- Ghi `akali_batches` như cũ (`batch_id` = `"fun5b:<job_id>"`) để cơ chế resume và đối soát sẵn có vẫn chạy được.
- `_wait_for_batch` và `_row_finished` dùng lại logic sẵn có: chờ đến khi `drive_link` hoặc `drive_error` có giá trị.
- `_finish_batch`: với fun5b thì **bỏ** `export_results` + upload Excel, gọi `complete(output_url=link folder)` luôn.
- Huỷ job từ Akali: `request_cancel_by_drive_folder` đã có sẵn và áp dụng được.

---

## 4. Lưu và dùng lại nền sạch (FireRed)

- **Key cache:** `sha256(ảnh đã fit 640×1152 + spec FireRed)`, giống temo. Cùng một ảnh nhân vật thì **lần sau không phải vẽ lại** (tiết kiệm ~7 phút + 2 phút load lại model).
- **Lưu tại** `fun5b_cache/plates/<hash>.png`, kèm `<hash>.jpg` (thumbnail ảnh gốc) và `<hash>.json` (`created_at`, `last_used`, `source_name`).
- Thư mục này **không** nằm trong vòng dọn 3 ngày của `queue_worker`, chỉ bị xoá khi người dùng xoá trên UI.
- Pose cache (`fun5b_cache/poses`) giữ quy tắc của temo: chỉ giữ 200 file mới nhất.
- **Trang quản lý** (đặt cạnh "Avatar cache" hiện có): danh sách nền đã lưu (ảnh gốc, nền sạch, ngày dùng gần nhất), nút xoá.

---

## 5. Server Fun-5B: bật và tắt như MuseTalk

| Việc | Cách làm |
|---|---|
| Bật | **Lazy:** job fun5b đầu tiên mới spawn `engines/Fun5B/.venv/bin/python fun5b_render_server.py` (flock `.fun5b_start.lock`, socket `.fun5b.sock`, log `logs/fun5b_server.log`). Load model ~2 phút. |
| Giữ | Thường trú giữa các job (không load lại). **Không** có hẹn giờ tự gỡ. |
| Tắt | (a) Khi job **MuseTalk/LatentSync** cần GPU: dừng server fun5b (giống `musetalk_adapter.paused()` ở bản 1.3.5) rồi lazy-start lại ở job fun5b kế tiếp. (b) Khi job **fun5b** chạy: dừng server MuseTalk. (c) Nút "Tắt server" trên UI. (d) Lỗi CUDA thì kill server và job được retry. |
| Model | Không bao giờ xoá khỏi đĩa. |
| FireRed | Trong server: gỡ model video, chạy `edit_worker` (subprocess `.venv-edit`), load lại model video. Giống hệt temo. |

Lý do phải tắt luân phiên: Fun-5B (12–20 GB) cộng MuseTalk (5.5–7.3 GB) vượt 24 GB VRAM và gần chạm 31 GB RAM. Cả hai đều chạy trong `gpu_lock()` (file `.gpu.lock`) để không bao giờ cùng render.

---

## 6. Thay đổi Backend

### 6.1 `database.py`
Thêm cột qua `_CONFIG_COLUMNS` (tự migrate bằng ALTER TABLE):

| Cột | Kiểu | Mặc định | |
|---|---|---|---|
| `ref_image_path` | TEXT | NULL | ảnh nhân vật |
| `background_path` | TEXT | NULL | nền sạch có sẵn (tuỳ chọn) |
| `fun5b_preset` | TEXT | NULL | key preset |
| `keep_background` | INT | 1 | |
| `auto_background` | INT | 1 | |
| `table_line` | REAL | NULL | 0..1 |
| `sharp_hands` | INT | 0 | |
| `progress` | REAL | NULL | 0..1, dùng chung cho mọi engine (hiện chỉ fun5b ghi) |

- `add_job`: chấp nhận `engine="fun5b"`. Với fun5b: không bắt buộc `model_res` thuộc MODELS, `audio_path`/`config_path`/`checkpoint_path` để `""`, `seed` mặc định 42.
- Thêm `set_progress(job_id, value)`.

### 6.2 `queue_worker.py` / `render_job.py`
- Nếu `engine == "fun5b"` thì **bỏ qua** trim, `normalize_video` (25fps) và `normalize_audio`. Fun-5B tự xử lý 30fps và crop.
- `render_job.py --engine fun5b --ref-image ... --preset ... --keep-background ... --auto-background ... --table-line ... --sharp-hands ... [--background ...]` gọi `fun5b_adapter.render()`.
- Tiến độ: adapter nhận `{"event":"progress"}` từ server, ghi `db.set_progress` và in ra job log. Việc in log định kỳ cũng giữ cho stall watchdog 20 phút không kích hoạt.
- Huỷ: adapter thấy `cancel_requested` thì gửi `{"op":"cancel"}` để **dừng hẳn** render trong server.
- Lỗi không tìm thấy người hoặc file hỏng thì `mark_failed` ngay, **không retry**.

### 6.3 Protocol socket (1 dòng JSON mỗi message)
```jsonc
→ {"op":"render","job_id":4100,"image":"...","video":"...","out":"work/4100/out.mp4",
   "preset":"natural","seed":42,"keep_background":true,"auto_background":true,
   "background":null,"table_line":null,"sharp_hands":false}
→ {"op":"cancel","job_id":4100}
→ {"op":"status"}     ← {"loaded":true,"busy":false,"vram_mb":12356}
← {"event":"progress","stage":"plate|pose|render","done":363,"total":901}
← {"event":"done","frames":901}
← {"event":"error","message":"...","retryable":false}
```

### 6.4 API endpoint

**Endpoint mới**

| Method | Path | Request | Response | Mục đích |
|---|---|---|---|---|
| GET | `/api/engines/fun5b` | | `{available, reason, presets:[{key,label,steps,note}], defaults, plate_available}` | Engine có dùng được không (thiếu model hoặc venv thì trả lý do), danh sách preset |
| GET | `/api/engines/fun5b/server` | | `{running, loaded, busy, vram_mb}` | Trạng thái server |
| POST | `/api/engines/fun5b/server/stop` | | `{stopped}` | Tắt server (409 nếu đang render) |
| GET | `/api/engines/fun5b/plates` | | `[{id, source_name, created_at, last_used, size_kb}]` | Danh sách nền sạch đã lưu |
| GET | `/api/engines/fun5b/plates/{id}/image` | `?kind=plate\|source` | `image/png` hoặc `image/jpeg` | Xem nền sạch hoặc ảnh gốc |
| DELETE | `/api/engines/fun5b/plates/{id}` | | `{deleted}` | Xoá một nền đã lưu |
| POST | `/api/jobs/animate` | multipart: `image`, `video`, `background?`, `product`, `kind`, `intent`, `other_key`, `preset`, `seed`, `keep_background`, `auto_background`, `table_line`, `sharp_hands` | `JobSubmitResponse` 201 | **Đường (a)**: tạo job fun5b từ tab mới trên UI máy này. Validate: ảnh/video đọc được, engine available, preset hợp lệ. |

**Endpoint cũ cần sửa**

| Method | Path | Thay đổi |
|---|---|---|
| GET | `/api/jobs/engine-choices` | Thêm `fun5b` khi `fun5b_available()` |
| GET | `/api/jobs`, WS `/ws/jobs` | `JobOut` thêm `ref_image_path, fun5b_preset, keep_background, auto_background, table_line, sharp_hands, progress` |
| POST | `/api/jobs/{id}/cancel`, `/cancel-all`, `/api/akali/batches/{id}/cancel` | Không đổi API, chỉ đổi cơ chế bên dưới (§6.2) |
| DELETE | `/api/jobs/{id}`, `/api/jobs/clear/{status}` | Dọn thêm `ref_image_path` và `background_path` (**không** đụng tới `fun5b_cache/plates`) |
| GET | `/api/akali/status` | Không đổi |

---

## 7. Thay đổi Frontend (React 19 + Vite + Redux)

| File | Thay đổi |
|---|---|
| `types/api.ts` | `Engine` thêm `'fun5b'`. Cập nhật `JobOut`. Thêm `Fun5bInfo`, `Fun5bServerStatus`, `Fun5bPlate`. |
| `api/engines.ts` *(mới)* | `getFun5bInfo`, `getFun5bServer`, `stopFun5bServer`, `listPlates`, `deletePlate`, `plateImageUrl` |
| `api/jobs.ts` | `createAnimateJob` |
| `hooks/useEngineChoices.ts` | Thêm fallback fun5b |
| `pages/animate/AnimatePage.tsx` *(mới)* | **Trang/tab mới "Chuyển động nhân vật"** trên sidebar (route `/animate`, thêm vào NAV trong `AppLayout.tsx`). Có 2 tab con `?tab=create\|plates`. Chỉ hiện khi `GET /api/engines/fun5b` trả `available`; nếu không thì hiện lý do (thiếu model hay venv). |
| `pages/animate/CreateAnimateTab.tsx` *(mới)* | Form: dropzone **ảnh nhân vật có preview**, **video mẫu có preview**, các trường đặt tên giống `CreateJobTab` (product, kind, intent, other_key), select Preset (kèm ghi chú tốc độ), Seed, checkbox "Giữ nền gốc", "Tự vẽ nền sạch (FireRed, dùng lại nền đã lưu)", slider "Mép bàn % chiều cao" (0 = tự động), upload "Ảnh nền sạch" (tuỳ chọn), checkbox "Tay nét khi cử động nhanh (+10–25%)". Ước tính thời gian theo độ dài video (~28 phút / 30s với preset `natural`). Bấm "Thêm vào hàng đợi" sẽ gọi `createAnimateJob`, rồi chuyển sang tab Hàng đợi giống `CreateJobTab`. Có badge "Đã có nền sạch" khi ảnh đã có trong cache. |
| `pages/animate/PlatesTab.tsx` *(mới)* | Lưới nền đã lưu: ảnh gốc ↔ nền sạch, ngày dùng gần nhất, nút xoá |
| `pages/jobs/CreateJobTab.tsx` | **Không đổi** (form lip-sync giữ nguyên). `useEngineChoices` của form này **lọc bỏ** `fun5b`. |
| `pages/jobs/QueueStatusTab.tsx` | **Thanh tiến độ** (`progress`), nhãn "Fun-5B", thumbnail ảnh nhân vật, ước tính thời gian còn lại |
| `pages/settings/` (tab "Fun-5B") | Trạng thái server, VRAM, nút "Tắt server" |

---

## 8. Cài đặt / đóng gói — tự cài sau khi cập nhật

Bản cập nhật `.deb` chỉ mang **code** (`fun5b/`, `install/`), không mang venv và model (`bundle-source.sh` loại trừ `engines/`, `checkpoints/`). Máy nhận bản cập nhật tự cài phần còn thiếu:

- **Khi nào:** mỗi lần backend khởi động, `backend/workers.start_all()` gọi `fun5b_setup.auto_start()`. Nếu thiếu engine và máy chạy được thì nó khởi chạy `install/setup_fun5b.sh` ở chế độ nền, tách khỏi app (đóng cửa sổ thì vẫn cài tiếp). Lần mở app sau, nếu dở dang thì chạy tiếp từ chỗ dừng.
- **Bỏ qua:** máy có GPU dưới ~20 GB (vd 3080 Ti 12 GB) hoặc không có GPU thì không tải gì, chỉ ghi lý do để UI hiển thị.
- **Lỗi có thể thử lại:** thiếu dung lượng (cần ~75 GB cho máy mới), thiếu `git/curl/gcc`, mất mạng. UI có nút **Cài lại**, gọi `POST /api/engines/fun5b/install`. Phần đã tải vẫn được giữ.
- **7 bước:**
  1. Kiểm tra máy.
  2. Lấy `uv` và Python 3.11.15.
  3. Clone VideoX-Fun theo pin trong `install/fun5b-source.json`.
  4. Tạo `engines/Fun5B/.venv` từ `install/fun5b-requirements.lock`.
  5. Tạo `.venv-edit` từ `install/fun5b-edit-requirements.lock`.
  6. Tải model bằng `install/download_fun5b_models.sh`: ~57 GB, pin commit Hugging Face, tự thử lại khi lỗi.
  7. Kiểm tra CUDA.
- **Không bao giờ để lại bản cài hỏng:** venv và VideoX-Fun được dựng dưới tên `*.partial`, đủ rồi mới đổi tên. App coi engine là sẵn sàng chỉ khi `fun5b_adapter.missing_files()` rỗng.
- **UI:** trang "Chuyển động nhân vật" hiện thẻ tiến độ cài (bước, % theo dung lượng model đã tải) thay cho form, rồi tự mở form khi xong.
- **Không** thêm vào `install/environment.py` REQUIRED, nên các máy cũ không bị đòi cài lại toàn bộ.
- Ship: bump version và build trong Docker ubuntu:22.04 (chỉ làm khi được yêu cầu).
- Đã thử trên "máy mới giả lập" (thư mục trống, model trỏ về bản thật): tự chạy qua `auto_start`, xong 7/7 bước, venv dựng lại render được.

## 9. Thứ tự làm

| Bước | Nội dung | Trạng thái |
|---|---|---|
| 0 | Đồng bộ code app đã cài 1.3.5 | ✅ (trừ `google_drive_upload.py`, vì bản cài còn chứa secret OAuth) |
| 1 | Mang `fun5b/` sang, di chuyển model, venv và cache | ✅ CLI render ra 720×1280 |
| 2 | Server + adapter + DB + worker + API | ✅ Chạy thử đầu-cuối: FireRed, render, huỷ (~5s), tắt server |
| 3 | Frontend | ✅ Đã build, chụp màn hình kiểm tra |
| 4 | Akali | ⏳ |
| 5 | Tự cài trên máy khác sau khi cập nhật | ✅ (`fun5b_setup.py`, `install/setup_fun5b.sh`) |

Chi tiết từng bước:

| Bước | Nội dung |
|---|---|
| 0 | Đồng bộ code app đã cài 1.3.5 (chưa commit) về repo: `musetalk_adapter.paused()`, `gpu_info.is_low_vram_gpu()`, ... Tắt và disable `fun-ui`. |
| 1 | Mang `fun5b/` sang, viết `config.py`, sửa đường dẫn. Di chuyển model, venv và cache trên máy này. Chạy thử CLI `python -m fun5b.run` ra video. |
| 2 | `fun5b_render_server.py` + `fun5b_adapter.py` + DB + `queue_worker`/`render_job` + luân phiên tắt MuseTalk + `POST /api/jobs/animate` + `/api/engines/fun5b*` |
| 3 | Frontend: trang "Chuyển động nhân vật" (form + nền đã lưu), thanh tiến độ trong Hàng đợi, tab Settings. **Đến bước này đường (a) đã dùng được đầy đủ.** |
| 4 | `akali_bridge` nhánh fun5b + cập nhật `doc_api/APi_RENDER_QUEUE.md`. Test với job Akali thật (khi bên Akali đã gửi được job fun5b). |
| 5 | `setup_fun5b.sh` và tải model cho các máy khác |

---

## 10. Rủi ro còn lại

1. **Job rất dài** (~28 phút / 30s video) chiếm GPU, nên job lip-sync (MuseTalk/LatentSync) phía sau phải chờ. Mỗi lần đổi qua lại giữa hai loại engine còn mất thêm ~2 phút load server.
2. **RAM 31 GB** khá sát: Fun-5B đỉnh 22 GB, cộng backend và các worker. Đề xuất spawn server bằng `systemd-run --user -p MemoryMax=22G` giống `fun-ui.sh`. Nếu không có systemd user thì chạy bình thường.
3. **Livestream** (MuseTalk realtime) không chạy được cùng lúc với Fun-5B. Khi đang có session livestream thì job fun5b sẽ chờ.
4. Bên Akali phải làm phần UI upload ảnh và video và gửi `params.engine="fun5b"` theo hợp đồng §3.1. Repo này không kiểm soát phần đó.

## 11. Còn cần bạn xác nhận

1. Hợp đồng Akali §3.1 (tên file `background.*`, các key trong `params`) như vậy có ổn không, hay bên Akali đã có format riêng?
2. Tên folder Drive kết quả: job từ Akali dùng `<tên video mẫu>__job<id>`. Job tạo từ tab mới thì đặt theo ngày (`dd-mm-yyyy`) **giống job thường** *(đề xuất)*, hay dùng folder riêng `fun5b/<ngày>`?
