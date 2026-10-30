# akali × máy render — Job "Chuyển động nhân vật" (Fun-5B)

> **Bản bàn giao cho phía akali, 31/10/2026.** Hai bên đã chốt toàn bộ nội dung qua 2 vòng phản hồi
> ([phan-hoi-akali.md](phan-hoi-akali.md)). **Phía máy render đã code xong.** Tài liệu này liệt kê những gì phía akali
> cần làm và những gì máy render đảm bảo.
> Hợp đồng API chung vẫn là `RENDER_SERVER_INTEGRATION.md` (do akali giữ). Ở đây chỉ mô tả phần thêm cho engine `fun5b`.

---

## 0. Tóm tắt

**Fun-5B:** người dùng đưa **1 ảnh nhân vật** và **1 video chuyển động mẫu**. Máy render tạo video nhân vật trong ảnh
làm theo động tác của video mẫu, giữ nền và ánh sáng của ảnh. Video ra **720×1280 (9:16), 30fps, không có tiếng**.
Không dùng Excel, không dùng TTS, không có giọng đọc.

**1 job akali = 1 cặp ảnh + video = 1 video thành phẩm.**

### Việc phía akali cần làm

| # | Việc | Mục |
|---|---|---|
| 1 | `/claim` đọc trường `engines` của máy và chỉ giao job có engine nằm trong đó, song song với `blocked_engines` / `deferred_engines` hiện có. Lưu `engines` của lần claim gần nhất để hiện trên dashboard | §1 |
| 2 | Thêm engine `fun5b` vào dashboard: form nạp job, tab Phân nhóm, trang công suất | §5 |
| 3 | Form nạp job fun5b: ảnh nhân vật, video mẫu, ảnh nền (tuỳ chọn), chế độ, seed, mép bàn và các tuỳ chọn khác | §2 |
| 4 | Tạo folder đầu vào trên Drive với tên file cố định, rồi tạo job với `params.engine = "fun5b"` | §3, §4 |
| 5 | Đơn giá point cho fun5b (akali chốt nội bộ) | — |
| 6 | Cảnh báo hoặc chặn khi nhóm của người dùng không có máy nào chạy được fun5b | §5 |

### Thứ tự triển khai

1. **akali làm việc #1** (`/claim` đọc `engines`). Máy render bản mới **đã** gửi `engines`; akali bản hiện tại bỏ qua
   trường này nên không có gì hỏng trong lúc chờ.
2. akali làm việc #2–#6, rồi **mới mở form nạp job fun5b**. Làm theo thứ tự này để không có job fun5b nào nằm chờ khi
   chưa máy nào nhận.

> **Giai đoạn chuyển tiếp** (máy đã gửi `engines`, akali chưa đọc): việc chọn engine cho từng máy vẫn chỉ dựa vào
> `blocked_engines` / `deferred_engines`. **Không gỡ** `blocked_engines` của các máy yếu (ví dụ chặn `latentsync` trên
> 3080 Ti) với suy nghĩ "máy đã tự khai báo rồi".

---

## 1. `/claim` — trường `engines`

Máy render gửi ở **mọi** lần claim:

```
POST /api/worker/claim
{"worker_id": "render-01", "engines": ["musetalk", "latentsync", "fun5b"]}
```

akali chỉ giao một job khi thoả **tất cả** điều kiện sau:

| Điều kiện | Nguồn |
|---|---|
| `params.engine` của job nằm trong `engines` | máy render (khả năng: đủ VRAM, đã cài xong model) |
| Engine của job không nằm trong `blocked_engines` | admin (chính sách) |
| Job không thuộc nhóm nào, hoặc cùng nhóm với máy | admin |
| Máy đang bật | admin |

Thứ tự ưu tiên giữ nguyên: job thuộc `deferred_engines` xếp sau, rồi theo `priority` giảm dần, rồi `id` tăng dần.

| Trường hợp | akali xử lý |
|---|---|
| Không có `engines` (bridge bản cũ) | coi như `["musetalk", "latentsync"]`, **không bao giờ** giao fun5b |
| `engines: []` | trả `204` |
| Job không có `params.engine` | coi là `musetalk` |
| Engine lạ trong `engines` | bỏ qua, không lỗi |

Tên engine viết thường: `musetalk`, `latentsync`, `fun5b`.

**Máy render tự khai báo `fun5b` khi:** GPU ≥ ~20 GB VRAM (RTX 3090/4090/5090) **và** đã tự cài xong engine. Sau khi cập
nhật app, máy tự tải ~57 GB model ở chế độ nền; trong lúc đang cài, máy chưa khai báo `fun5b`. Danh sách được tính lại
ở mỗi lần claim, nên ngay khi cài xong máy sẽ bắt đầu nhận job fun5b. Máy 12 GB (3080 Ti) không bao giờ khai báo `fun5b`.

---

## 2. Form nạp job fun5b

| Trường | Bắt buộc | Kiểu / giá trị | Mặc định | Gửi vào |
|---|---|---|---|---|
| Ảnh nhân vật | ✔ | `.png` `.jpg` `.jpeg` `.webp` | — | file `nhan_vat.<đuôi>` |
| Video mẫu | ✔ | `.mp4` `.mov` `.mkv` `.webm` `.avi` | — | file `video_mau.<đuôi>` |
| Ảnh nền sạch | ✖ | ảnh cùng cảnh, **không có người** | — | file `background.<đuôi>` |
| Chế độ | ✔ | `fast` \| `balanced` \| `natural` \| `natural_light` | `natural` | `params.preset` |
| Seed | ✖ | số nguyên | `42` | `params.seed` |
| Giữ nguyên nền & ánh sáng của ảnh | ✖ | bool | `true` | `params.keep_background` |
| Tự vẽ nền sạch bằng AI | ✖ | bool | `true` | `params.auto_background` |
| Mép bàn | ✖ | số nguyên 1–99 (%), hoặc Tự động | Tự động | `params.table_line` (`null` = tự động) |
| Tay nét khi cử động nhanh | ✖ | bool | `false` | `params.sharp_hands` |

### 2.1 Chế độ (preset)

| Giá trị | Nhãn hiển thị | Ghi chú cho người dùng | Thời gian / 10 giây video mẫu (RTX 3090) |
|---|---|---|---|
| `fast` | Nhanh | Nhanh nhất; ánh sáng kiểu studio | ~5.5 phút |
| `balanced` | Cân bằng | Ánh sáng studio nhẹ hơn | ~8 phút |
| `natural` | Tự nhiên | **Mặc định**, giữ ánh sáng của ảnh gốc | ~9 phút |
| `natural_light` | Ánh sáng tự nhiên (chậm) | Sát ảnh gốc nhất | ~21 phút |

**Ước tính thời gian để hiển thị:** `độ dài video mẫu (giây) / 10 × thời gian theo bảng`, cộng thêm:
- ~2 phút nếu máy chưa nạp model (job fun5b đầu tiên sau một job loại khác);
- ~7 phút nếu ảnh nhân vật **mới** (vẽ nền sạch lần đầu; các lần sau dùng ảnh đó thì dùng lại nền đã lưu trên máy);
- +10–25% nếu bật "Tay nét".

Ví dụ: video mẫu 30 giây, ảnh mới, chế độ Tự nhiên: ≈ 27 + 2 + 7 ≈ **36 phút**.

### 2.2 Mép bàn

Phần ảnh **dưới vạch** (bàn, sản phẩm) được giữ nguyên như ảnh gốc trong suốt video; tay đưa xuống dưới vạch sẽ bị bàn
che. Dùng khi sản phẩm đặt sát người bị vẽ lại. Mặc định **Tự động**, kèm nút mở modal để đặt vạch.

Yêu cầu cho modal (giống UI trên máy render):
- Hiển thị ảnh theo **đúng khung model dùng**: crop giữa về tỉ lệ **640×1152 (9:16)**, kiểu `object-fit: cover`.
- Người dùng bấm hoặc kéo trên ảnh để đặt vạch đỏ; vùng dưới vạch tô đỏ nhạt. Kèm thanh trượt 0–99 để chỉnh chính xác.
- Giá trị gửi đi là **% chiều cao của khung đã crop**, làm tròn số nguyên (1–99). Nút "Tự động" gửi `null`.

### 2.3 Gợi ý kiểm tra ở form

- Video mẫu nên dài **≤ 60–90 giây**: dài hơn vẫn chạy, nhưng nền có thể trôi dần sau vài chục giây.
- Ảnh nhân vật nên là ảnh đứng, tỉ lệ gần 9:16, thấy rõ người (và bàn hoặc sản phẩm nếu có). Ảnh không tìm thấy người sẽ
  bị máy render từ chối (§4.3).
- Ẩn "Tự vẽ nền sạch" và "Mép bàn" khi tắt "Giữ nguyên nền".

---

## 3. Folder đầu vào trên Drive

akali tạo **1 folder riêng cho mỗi job**, đặt file theo tên cố định:

```
<folder đầu vào của job>/
├── nhan_vat.png        (bắt buộc — .png/.jpg/.jpeg/.webp)
├── video_mau.mp4       (bắt buộc — .mp4/.mov/.mkv/.webm/.avi)
└── background.jpg      (tuỳ chọn — có thì máy bỏ qua bước vẽ nền bằng AI)
```

- `drive_file_id` / `drive_url` trả trong `/claim` là id và link của folder này, giống job Excel.
- Máy render nhận diện theo **đuôi file**, không bắt buộc đúng tên: folder người dùng tự dựng cũng chạy được. File khác
  (ví dụ `.xlsx`) bị bỏ qua.
- Máy render cần **đọc** được folder này (cùng điều kiện chia sẻ như folder của job Excel hiện nay).

---

## 4. Job, params và kết quả

### 4.1 `params`

```jsonc
{
  "engine": "fun5b",          // bắt buộc, viết thường
  "preset": "natural",
  "seed": 42,
  "keep_background": true,
  "auto_background": true,
  "table_line": null,         // hoặc số nguyên 1–99
  "sharp_hands": false
}
```

- Thiếu khoá → máy dùng mặc định ở §2.
- Các khoá của job lip-sync (`input_type`, `model_res`, `out_res`, `guidance`, `steps`, `enhance_*`, `tts_provider`,
  `voice_id`) **không** dùng cho fun5b. Có gửi thì máy bỏ qua.

### 4.2 Trong lúc chạy

- **Heartbeat** 60 giây/lần (lease 1800 giây vẫn đủ, kể cả job dài 40 phút trở lên).
- **`progress`** 0–100, tăng dần theo giai đoạn:

  | Giai đoạn | `progress` |
  |---|---|
  | chờ / nạp model | 0–1 |
  | vẽ nền sạch (ảnh mới, ~7 phút) | 3 |
  | tách động tác từ video mẫu | 5 |
  | render | 5 → 95 (tăng đều theo từng bước) |
  | render xong, đang đẩy video lên Drive | 97 |

- Heartbeat của fun5b **không** gửi `output_url` sớm, vì job chỉ có 1 video.
- **Huỷ trên dashboard akali** (heartbeat nhận `409 canceled`): máy dừng render trong vài giây và không gọi gì thêm.

### 4.3 Kết thúc

| Tình huống | Máy render gọi | `retryable` | Ví dụ `error` |
|---|---|---|---|
| Xong | `/complete` | — | — |
| Folder đầu vào sai (thiếu hoặc thừa ảnh / video) | `/fail` | `false` | `cần đúng 1 ảnh nhân vật (.png/.jpg/.webp, không tính background.*), thấy 2` |
| `params` sai (preset lạ, `table_line` ngoài 1–99, seed không phải số) | `/fail` | `false` | `preset không hợp lệ: 'abc' (dùng fast, balanced, natural, natural_light)` |
| Không tìm thấy người trong ảnh hoặc video, file hỏng | `/fail` | `false` | `[render_job] REJECTED: Không tìm thấy người …` |
| Lỗi máy (GPU, server engine chết…) sau khi đã tự thử lại | `/fail` | `true` | `render rc=1; …` |
| Không đẩy được video lên Drive sau nhiều lần thử | `/fail` | `true` hoặc `false` | ghi rõ lý do |
| Người dùng huỷ trên **máy render** | `/cancel` | — | `người dùng dừng job ở máy render` |

`error` dài tối đa 2000 ký tự; nên hiển thị nguyên văn cho người dùng.

### 4.4 `output_url` — folder kết quả

- `output_url` là link **folder** `https://drive.google.com/drive/folders/<id>`, tên folder **`fun5b__job<id job akali>`**.
- Folder này tách biệt với folder đầu vào. Bên trong có **đúng 1 file `.mp4`** (video thành phẩm), không có ảnh, video
  mẫu hay file nào khác. Vì vậy akali tính point theo tổng độ dài video trong folder như job Excel mà không phải sửa
  gì.
- Bấm **"Chạy lại"** trên akali (giữ nguyên id job) vẫn an toàn: máy render dọn folder `fun5b__job<id>` lúc bắt đầu và
  ngay trước `/complete`, nên folder luôn chỉ còn 1 video.
- Điều kiện để akali đọc được độ dài video: folder Drive gốc của máy render được chia sẻ cho service account của akali
  (giống job Excel hiện nay).

---

## 5. Dashboard

- Engine `fun5b` hiển thị là **"Fun-5B"** hoặc **"Chuyển động nhân vật"**. Giá trị trong `params.engine` luôn là
  `fun5b`.
- Cột **Engine** của từng máy lấy từ `engines` của lần claim gần nhất. Máy vừa cập nhật app có thể mất một lúc (tự tải
  ~57 GB model) rồi mới hiện `fun5b`.
- Tab Phân nhóm: thêm `fun5b` vào danh sách chọn cho `blocked_engines` / `deferred_engines`.
- **Cảnh báo nhóm:** khi người dùng thuộc nhóm G tạo job fun5b mà không máy nào trong G từng khai báo `fun5b`, cảnh báo
  hoặc chặn ngay, vì job sẽ chờ mãi.
- Trạng thái job dùng lại như cũ: Đang chờ → Đang chạy (thanh `progress`) → Xong (nút "Mở" theo `output_url`) / Lỗi
  (hiện `error`) / Đã huỷ.

---

## 6. Kịch bản nghiệm thu chung

Chạy trên môi trường thật, với ít nhất 1 máy RTX 3090 (có `fun5b`) và 1 máy 3080 Ti (không có `fun5b`) cùng nhóm:

| # | Kịch bản | Kết quả mong đợi |
|---|---|---|
| 1 | Máy 3080 Ti claim khi chỉ có job fun5b trong hàng chờ | `204`, job không bị giao |
| 2 | Máy 3090 claim | Nhận job fun5b; dashboard hiện cột Engine của máy có `fun5b` |
| 3 | Job ảnh mới + video 5–10 giây, chế độ Tự nhiên | `progress` lên dần 1 → 3 → 5 → … → 97; `/complete`; folder `fun5b__job<id>` có đúng 1 mp4; point trừ đúng theo độ dài |
| 4 | Chạy lại đúng job đó | Folder vẫn chỉ 1 video; point không bị trừ gấp đôi |
| 5 | Cùng ảnh, video khác | Nhanh hơn kịch bản 3 (~7 phút) vì dùng lại nền đã lưu |
| 6 | Folder có 2 ảnh nhân vật | `/fail` với `retryable: false`, lỗi hiện rõ |
| 7 | Ảnh không có người (ví dụ ảnh phong cảnh) | `/fail` với `retryable: false`, "Không tìm thấy người" |
| 8 | Huỷ trên dashboard khi đang render | Máy dừng trong vài giây; job "Đã huỷ" |
| 9 | Mép bàn đặt 80% | Phần dưới 80% khung trong video giữ y như ảnh gốc |
| 10 | Tắt app máy render giữa chừng rồi mở lại | Bridge nối tiếp job (lease chưa hết), không render lại từ đầu nếu đã xong |
