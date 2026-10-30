# Phản hồi của akali cho bản nháp "Giao job Chuyển động nhân vật (Fun-5B)"

> Trả lời tài liệu `akali-api-mk.md` (bản nháp từ phía máy render). Đã đối chiếu với code akali
> đang chạy ngày 31/10/2026.
> Hợp đồng API đầy đủ phía akali: `RENDER_SERVER_INTEGRATION.md`. Tài liệu này chỉ nêu những chỗ
> cần sửa trong bản nháp và câu trả lời cho mục 7.
>
> **Cập nhật 31/10/2026:** đã thêm **§6 — Phản hồi lần 2**, trả lời bản v2 của `akali-api-mk.md`.
> Bản v2 đã nhận đủ các điểm từ §0 đến §5 dưới đây.

## 0. Tóm tắt

akali **đồng ý hướng chung**: `params.engine = "fun5b"`, mỗi job là một folder Drive gồm ảnh và video
mẫu, `output_url` là link folder, và giữ nguyên các luồng heartbeat, huỷ, đối soát hiện có.

Trước khi code, cần sửa năm điểm trong bản nháp:

1. **Lọc engine khi claim:** akali đã có sẵn cơ chế lọc theo engine (`blocked_engines` / `deferred_engines`),
   nhưng do admin đặt trên dashboard. Trường `engines` mà máy render khai báo sẽ được thêm vào và
   **dùng song song** với cơ chế hiện có (§2).
2. **Lease** hiện là **1800 giây**, không phải 600 (§1).
3. **Folder kết quả chỉ được chứa video thành phẩm**, vì akali trừ point theo tổng độ dài video
   trong folder đó (§3.1).
4. **Nhóm máy:** job chỉ về máy cùng nhóm với người tạo, nên nhóm đó phải có ít nhất một máy chạy được
   fun5b (§3.3).
5. Tên tài liệu tham chiếu: hợp đồng API của akali là `RENDER_SERVER_INTEGRATION.md`, không phải
   `APi_RENDER_QUEUE.md` hay `doc-api-md`.

---

## 1. Các chỗ trong bản nháp lệch với akali hiện tại

| Mục bản nháp | Bản nháp ghi | Thực tế akali | Sửa thành |
|---|---|---|---|
| Đầu file | Đọc kèm `APi_RENDER_QUEUE.md`, `doc-api-md` | Hợp đồng do akali giữ là `RENDER_SERVER_INTEGRATION.md` | Đổi tham chiếu |
| §4 | "Hiện `/claim` giao **bất kỳ** job nào cho **bất kỳ** máy nào" | `/claim` đã lọc theo **nhóm máy**, theo `blocked_engines` (engine máy không bao giờ nhận) và `deferred_engines` (engine máy chỉ nhận khi hết job khác). Hai danh sách này do admin đặt cho từng máy ở tab Phân nhóm. | Xem §2 |
| §4 | Job fail quay về hàng chờ "có thể rơi vào đúng máy đó lần nữa" | Sau `/fail`, máy **khác** đang rảnh nhận ngay. Máy vừa làm hỏng thì phải đợi hết backoff. Dù vậy, nếu trong nhóm chỉ có máy đó thì nó vẫn nhận lại sau backoff. | Vẫn cần lọc engine, nhưng lý do là trường hợp nhóm chỉ có máy yếu |
| §5.3 | Lease 600s | Production đang chạy `LEASE_SECONDS=1800` | Sửa số. Heartbeat 60s/lần vẫn giữ nguyên |
| §5 bước 6 | "output_url sớm: không cần" | Đúng. `output_url` trong heartbeat là **tuỳ chọn** (xem `doc/early-output-url.md`) | Giữ nguyên |
| §6 | Ước tính thời gian lúc tạo job | akali chưa có bảng thời gian theo engine cho fun5b | Bên akali tự làm, máy render không cần sửa gì |

---

## 2. Lọc engine khi claim — hợp đồng chốt

Máy render gửi danh sách engine **nó thực sự chạy được ngay lúc này**:

```
POST /api/worker/claim
{"worker_id": "render-01", "engines": ["musetalk", "latentsync", "fun5b"]}
```

akali chỉ giao một job khi thoả **tất cả** điều kiện sau:

| Điều kiện | Nguồn | Ghi chú |
|---|---|---|
| Engine của job nằm trong `engines` | Máy render, gửi ở mỗi lần claim | **Mới.** Đây là *khả năng* của máy: đủ VRAM, đã cài xong model |
| Engine của job không nằm trong `blocked_engines` | Admin, tab Phân nhóm | Đã có. Đây là *chính sách*: admin có thể chặn một engine dù máy chạy được |
| Job không nhóm, hoặc cùng nhóm với máy | Admin | Đã có (mục 8.6 của hợp đồng) |
| Máy đang bật | Admin | Đã có. Máy bị tắt luôn nhận `204` |

Thứ tự ưu tiên giữ nguyên: job thuộc `deferred_engines` của máy xếp sau mọi job khác, sau đó theo
`priority` giảm dần rồi `id` tăng dần.

Quy ước tương thích:

| Trường hợp | akali xử lý |
|---|---|
| Không gửi `engines` (bridge bản cũ) | Coi như `["musetalk", "latentsync"]`, **không bao giờ** giao fun5b |
| Gửi `engines: []` | Không giao job nào (trả `204`) |
| Job không có `params.engine` | Coi là `musetalk` |
| Engine lạ trong `engines` | Bỏ qua, không lỗi |

Tên engine phải khớp đúng chữ thường: `musetalk`, `latentsync`, `fun5b`. Trên dashboard, akali hiển thị
`musetalk` là "MT-Fast" và `latentsync` là "LS-Pro", nhưng giá trị ghi trong `params.engine` vẫn là tên gốc.

akali sẽ lưu danh sách `engines` của lần claim gần nhất để hiện cột "Engine" của từng máy trên dashboard,
như bản nháp gợi ý.

---

## 3. Ràng buộc từ akali mà bản nháp chưa nhắc

### 3.1 Folder kết quả chỉ chứa video thành phẩm

Khi nhận `/complete`, akali tính point bằng cách **cộng độ dài mọi file video nằm trực tiếp trong folder
`output_url`** (lấy `videoMediaMetadata.durationMillis` từ Drive). Vì vậy:

- `output_url` phải là **link folder** (`.../drive/folders/<id>`). Gửi link file thì akali tính ra 0 giây
  và không trừ point.
- **Không** để ảnh nhân vật, video mẫu hay `background.*` trong folder kết quả. Video mẫu nằm trong đó sẽ
  bị tính tiền như video thành phẩm. Folder `"<tên video mẫu>__job<id>"` ở bước F2 phải là folder **riêng**,
  khác folder đầu vào mà akali tạo.
- Video câm không ảnh hưởng gì, Drive vẫn trả độ dài bình thường.

### 3.2 `/complete` bắt buộc có `output_url`

Thiếu `output_url` thì akali trả `400` và job không được đánh dấu xong. Nếu không có video nào lên được
Drive thì phải gọi `/fail`, không gọi `/complete` (khớp với dòng "upload Drive lỗi" ở §5.1 bản nháp).

### 3.3 Nhóm máy

Job của người dùng thuộc nhóm G chỉ được giao cho máy thuộc nhóm G. Nếu nhóm G không có máy nào khai báo
`fun5b` thì job fun5b của nhóm đó sẽ **chờ mãi**. Phía akali sẽ chặn hoặc cảnh báo ngay lúc tạo job.
Máy render không cần làm gì, nhưng khi kiểm tra trên dashboard thì đừng nhầm đây là lỗi bridge.

### 3.4 Heartbeat và `progress`

Không đổi so với job Excel: `progress` là phần trăm 0–100, bridge tự kẹp giá trị. akali không kiểm tra
con số này tăng đều hay nhảy bậc.

---

## 4. Trả lời mục 7 của bản nháp

| # | Câu hỏi | Trả lời của akali |
|---|---|---|
| 1 | Thêm `engines` vào `/claim`? | **Đồng ý**, theo hợp đồng ở §2. Dùng song song với `blocked_engines` hiện có, không thay thế |
| 2 | 1 job = 1 cặp ảnh + video, hay batch? | **Bắt đầu với 1 cặp.** akali tính point theo tổng độ dài video trong folder, nên sau này làm batch (nhiều video ra cùng một folder) thì phía akali không phải sửa cách tính |
| 3 | Nhận diện theo đuôi file hay tên cố định? | akali tự tạo folder đầu vào và sẽ đặt tên cố định: `nhan_vat.<đuôi>`, `video_mau.<đuôi>`, và `background.<đuôi>` nếu có. **Máy render vẫn nên nhận diện theo đuôi file** như §2 bản nháp, để folder do người dùng tự dựng cũng chạy được |
| 4 | Mép bàn bắt buộc trên UI akali? | **Đề xuất:** mặc định "Tự động" (`table_line: null`), có nút mở modal để đặt vạch như máy render. Không bắt buộc |
| 5 | Upload Drive lỗi: `/fail` hay `/complete`? | **`/fail`, `retryable: true`.** `/complete` mà không có video thì người dùng thấy job "Xong" nhưng folder rỗng và không bị trừ point. **Đề nghị thêm:** bridge tự thử upload lại vài lần trước khi gọi `/fail`, vì `/fail` có retry sẽ làm máy khác render lại từ đầu (30 phút trở lên) |
| 6 | Âm thanh | Để câm như đã chốt. akali không phụ thuộc vào chuyện có tiếng hay không |

---

## 5. Việc phía akali sẽ làm

Để bên máy render biết những phần phụ thuộc vào akali:

1. `/claim` nhận `engines` và lọc theo §2. Lưu `engines` lần gần nhất để hiện trên dashboard.
2. Thêm `fun5b` vào danh sách engine của dashboard (form nạp job, tab Phân nhóm, trang công suất).
3. Form nạp job fun5b: upload ảnh nhân vật, chọn hoặc upload video mẫu, ảnh nền tuỳ chọn, chọn preset,
   đặt mép bàn. akali tạo folder đầu vào trên Drive theo §2 bản nháp và tên file ở §4 câu 3.
4. Đơn giá point cho fun5b (hiện MT-Fast 1 point/phút, LS-Pro 2 point/phút). akali sẽ chốt nội bộ, không
   ảnh hưởng tới bridge.
5. Cảnh báo khi nhóm của người dùng không có máy nào chạy được fun5b (§3.3).

Thứ tự triển khai đề xuất: akali làm bước 1 trước, bridge gửi `engines` (bản bridge cũ vẫn chạy bình
thường nhờ quy ước mặc định). Sau đó mới mở form nạp job fun5b, để không có job fun5b nào nằm chờ trong
lúc chưa có máy nhận.

---

## 6. Phản hồi lần 2 — trả lời bản v2 của `akali-api-mk.md`

Bản v2 đã nhận đủ các điểm ở §0–§5. Phần này trả lời mục 7 của bản v2 và thêm hai góp ý.

### 6.1 Trả lời mục 7 (bản v2)

| # | Câu hỏi | Trả lời của akali |
|---|---|---|
| 1 | akali bản cũ có bỏ qua trường `engines` trong `/claim` không? | **Có.** `/claim` hiện chỉ đọc `worker_id`; mọi trường khác bị bỏ qua, không trả 400/422. **Bridge phát hành trước akali được.** Xem lưu ý ở §6.2 |
| 2 | Drive hết dung lượng: `retryable` `true` hay `false`? | **Đồng ý `retryable: false`**, `error` ghi rõ "Drive máy render hết dung lượng". Trên akali, job chuyển sang "Lỗi" và hiện nội dung `error`. Dọn Drive xong, người dùng bấm "Chạy lại". Nếu dùng `true` thì job sẽ đi qua lần lượt từng máy, mỗi máy render ~30 phút rồi hỏng y hệt |
| 3 | Các máy có dùng chung một tài khoản Drive không? | Đây là cấu hình của máy render, akali không trả lời thay được. Dù câu trả lời là gì, **vẫn giữ bước F3**: bấm "Chạy lại" trên akali giữ nguyên `akali_job_id`, nên cùng một máy cũng có thể ghi lại vào folder `fun5b__job<id>` đã có file của lần trước |

### 6.2 Lưu ý cho giai đoạn chuyển tiếp

Trong khoảng thời gian bridge mới đã gửi `engines` nhưng akali chưa cập nhật, akali **bỏ qua** `engines`.
Việc chọn engine cho từng máy vẫn chỉ dựa vào `blocked_engines` / `deferred_engines` mà admin đặt ở tab
Phân nhóm. Vì vậy:

- **Không gỡ** `blocked_engines` của các máy yếu (ví dụ chặn `latentsync` trên 3080 Ti) với suy nghĩ "máy đã tự
  khai báo `engines` rồi". Gỡ ra thì máy đó vẫn nhận job LS-Pro.
- Với fun5b thì khoảng này không có rủi ro, vì akali chưa mở form nạp job fun5b nên chưa có job fun5b nào để
  giao nhầm.

Sau khi akali đọc `engines`, hai cơ chế chạy song song như ở §2: `engines` nói máy **chạy được** gì,
`blocked_engines` nói admin **không cho** máy chạy gì.

### 6.3 Góp ý: dọn folder kết quả lần nữa ngay trước `/complete`

F3 chỉ dọn folder `fun5b__job<id>` lúc **bắt đầu** job. Trường hợp sau vẫn để lại 2 video:

1. Máy A đang upload video thì mất lease. akali giao job cho máy B.
2. Máy B chạy F3, folder đã sạch.
3. Upload dở của máy A lên xong, sau đó máy A mới nhận 409 ở nhịp heartbeat kế tiếp.
4. Máy B render xong và upload. Folder có **2 video**, akali trừ point **gấp đôi**.

**Đề nghị:** ngay trước khi gọi `/complete`, bridge liệt kê folder kết quả và chuyển vào thùng rác mọi file
**không phải** file `drive_link` của chính render job này. Có thể dùng lại `trash_folder_contents` của F3, chỉ
cần thêm tham số giữ lại một file id.

### 6.4 Góp ý: akali phải đọc được folder kết quả

akali tính point bằng **service account của akali**, đọc folder `output_url` trên Drive của máy render
(§3.1). Folder `fun5b__job<id>` nằm dưới `GOOGLE_DRIVE_FOLDER_ID` của máy render nên thừa hưởng quyền chia sẻ
của folder gốc. Cần giữ điều kiện như job Excel hiện nay: **folder gốc đó được chia sẻ cho service account
akali** (ít nhất quyền xem). Nếu không, akali tính ra 0 giây và không trừ point; job vẫn "Xong" nên rất khó
phát hiện.

### 6.5 Drive máy render hết dung lượng

Đã ghi nhận cảnh báo ở §5.4 bản v2. Bên akali sẽ kiểm tra lại các job lip-sync từ 22/10 tới nay, vì nếu Drive
hết dung lượng thì các job đó cũng bị ảnh hưởng. Đồng ý phải xử lý xong trước khi mở fun5b.
