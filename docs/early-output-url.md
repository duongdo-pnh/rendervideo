# Báo link thư mục Drive sớm qua heartbeat

Việc này cần làm ở **bridge trên máy render** (`akali_bridge.py`). Source của bridge
không nằm trong repo này, nên phải sửa trực tiếp trên máy render. Phía akali đã làm
xong (commit `ea39454`), không cần sửa gì thêm.

Hợp đồng API đầy đủ xem [RENDER_SERVER_INTEGRATION.md](../RENDER_SERVER_INTEGRATION.md)
mục 3.2 (heartbeat), 3.3 (complete) và mục 6 (luồng bridge).

## 1. Vấn đề

Hiện tại bridge chỉ gửi link thư mục kết quả ở bước 13 (`/complete`), tức là sau khi
**cả batch** đã render xong. Một batch có thể có hàng chục video, nên dù video đầu
tiên đã lên Drive từ lâu, người dùng vẫn chỉ thấy dấu "—" ở cột Drive và phải chờ
đến khi batch xong hết.

## 2. Mong muốn

Ngay khi **video đầu tiên** của batch đã upload xong lên Drive, dashboard hiện nút
**"Mở"**. Bấm vào sẽ mở thư mục Drive và xem được những video đã có, trong khi job
vẫn ở trạng thái **"Đang chạy"**.

## 3. Phía akali đã hỗ trợ sẵn

`POST /api/worker/jobs/<id>/heartbeat` nhận thêm field **`output_url` (tuỳ chọn)**:

```
POST /api/worker/jobs/<id>/heartbeat
Authorization: Bearer <WORKER_API_KEY>
{"claim_token": "...", "progress": 40, "output_url": "https://drive.google.com/drive/folders/YYYY"}
```

Hành vi phía akali ([backend/routes/worker.py](../backend/routes/worker.py), hàm `heartbeat`):

| Tình huống | akali làm gì |
|---|---|
| Có gửi `output_url` | Ghi vào job. **Không đổi `status`**, job vẫn "Đang chạy" |
| Không gửi, hoặc gửi `null` / `""` | Giữ nguyên giá trị đã có (`COALESCE`), hoạt động như cũ |
| Gửi lặp lại ở mọi nhịp heartbeat | Không sao, ghi đè bằng cùng một giá trị |
| 409 (`canceled` / `lease_lost` / `not_found`) | Không ghi gì. Xử lý 409 như cũ |

Dashboard đã tự hiện link khi có `output_url`, không cần job ở trạng thái `done`:
nút "Mở" ở danh sách job ([web/components/JobTable.tsx](../web/components/JobTable.tsx))
và dòng "Kết quả" ở trang chi tiết job ([web/pages/jobs/[id].tsx](../web/pages/jobs/[id].tsx)).

`/complete` vẫn **bắt buộc** gửi `output_url` như trước. Việc gửi sớm qua heartbeat
không thay thế bước này.

## 4. Việc cần sửa ở bridge

Sửa trong vòng heartbeat (bước 10 ở mục 6 của tài liệu tích hợp):

1. Mỗi nhịp heartbeat, nếu batch **chưa có** link thư mục thì kiểm tra xem đã có dòng
   nào có `drive_link` chưa (tức là có video đã thật sự lên Drive).
2. Có rồi thì dựng link thư mục **đúng như bước 13**:
   `ensure_folder(service, excel_stem, default_folder_id())`. Lưu link vào trạng thái
   của batch để không phải gọi Drive API ở mọi nhịp.
3. Từ đó trở đi, mọi heartbeat đều kèm `output_url`.
4. Bước 13 (`/complete`) có thể dùng lại link đã lưu.

Phác thảo (tên biến và hàm chỉnh cho khớp với bridge thật):

```python
def heartbeat_tick(batch):
    payload = {"claim_token": batch.claim_token, "progress": batch.progress_pct()}

    if batch.output_url is None and batch.any_row_has_drive_link():
        try:
            service = gdrive.get_service()
            fid = gdrive.ensure_folder(service, batch.excel_stem, gdrive.default_folder_id())
            batch.output_url = f"https://drive.google.com/drive/folders/{fid}"
            batch.save()
        except Exception:
            logger.exception("Job %s: chua dung duoc link thu muc, thu lai o nhip sau", batch.akali_job_id)

    if batch.output_url:
        payload["output_url"] = batch.output_url

    r = requests.post(f"{AKALI}/api/worker/jobs/{batch.akali_job_id}/heartbeat",
                      json=payload, headers=AUTH, timeout=30)
    # 409 -> xu ly nhu cu (muc 3.2 / 4.1), khong doi gi
```

### Lưu ý

- **Đợi có `drive_link`, không đợi `render_status='done'`.** Upload Drive chạy ở thread
  nền sau `mark_done` (xem "Bước 11" trong tài liệu tích hợp). Gửi link khi video chưa
  lên Drive thì người dùng mở ra sẽ thấy thư mục rỗng hoặc không tồn tại.
- **Thư mục phải đúng là thư mục của batch.** `ensure_folder()` với cùng `<stem>` luôn
  trả về cùng một `folder_id`, nên gọi sớm hay gọi ở bước 13 đều ra cùng một link.
  Nếu bản trên máy render đặt tên thư mục theo cách khác (xem mục 2.2 của tài liệu
  tích hợp), phải dựng link theo đúng cách đó.
- **Lỗi khi gọi Drive API không được làm hỏng heartbeat.** Nếu `ensure_folder` lỗi thì
  vẫn gửi heartbeat bình thường, chỉ không kèm `output_url`, rồi thử lại ở nhịp sau.
  Heartbeat bị trễ có thể làm hết lease và job bị giao cho máy khác.
- **Lưu `output_url` vào trạng thái bền của batch**, ví dụ thêm cột vào bảng
  `akali_batches` (mục 7 của tài liệu tích hợp):
  `ALTER TABLE akali_batches ADD COLUMN output_url TEXT;`. Như vậy nếu bridge khởi
  động lại giữa chừng thì không phải dựng lại link. Nếu không lưu cũng không sao,
  akali vẫn giữ link đã nhận, bridge chỉ phải gọi `ensure_folder` lại một lần.
- Không gửi đường dẫn file đơn lẻ (`webViewLink` từ `upload_file()`). `output_url` phải
  là **link thư mục**.

## 5. Nghiệm thu

1. Nạp một job có từ 3 dòng trở lên.
2. Khi dòng đầu đã có `drive_link` (xem log bridge hoặc `tts_jobs`), chờ tối đa một
   nhịp heartbeat (khoảng 60s).
3. Trên dashboard, job vẫn "Đang chạy" nhưng cột Drive đã có nút **"Mở"**.
4. Bấm "Mở" thì thấy thư mục Drive của batch, có video đầu tiên trong đó.
5. Khi batch xong, job chuyển sang "Xong", link vẫn là **cùng thư mục đó**, đủ video
   và file Excel kết quả.
6. Huỷ job giữa chừng trên dashboard: bridge vẫn nhận `409 reason=canceled` và dừng
   như cũ (mục 4.1). Gửi `output_url` không làm thay đổi gì ở luồng huỷ.

Kiểm tra trực tiếp trong DB của akali:

```sql
SELECT id, status, progress, output_url FROM jobs WHERE id = <job_id>;
-- mong doi: status = 'processing', progress < 100, output_url da co gia tri
```

Làm xong thì đánh dấu dòng checklist tương ứng ở mục 11 của
[RENDER_SERVER_INTEGRATION.md](../RENDER_SERVER_INTEGRATION.md).
