# Cài môi trường desktop

Chạy `bash install/setup.sh`, hoặc bấm **Cài môi trường** trên màn hình khởi động.
Bộ cài chuẩn bị cả LatentSync và MuseTalk, tải model và kiểm tra render trước khi
mở ứng dụng. Gói desktop phụ thuộc Python hệ thống, FFmpeg và PolicyKit để xin
quyền quản trị trong phiên đồ họa.

## Điều kiện và driver

- GPU phải có cấu hình `_validated: true` trong `gpu_profiles.json`. Hiện chỉ
  `sm_86` được bật; không tự cho qua RTX 4090/5090 chưa xác thực.
- Cần tối thiểu 60 GiB trống ở thư mục cài, 8 GiB ở `/tmp`, 5 GiB ở phân vùng `/`.
  Đây là ngưỡng dự phòng của bộ cài, không phải dung lượng model chính xác.
- Nếu có phần cứng NVIDIA nhưng driver không hoạt động, trên Ubuntu bộ cài dùng
  `ubuntu-drivers install` theo [hướng dẫn Ubuntu](https://ubuntu.com/server/docs/how-to/graphics/install-nvidia-drivers/).
  Sau đó trả mã 20 và yêu cầu người dùng khởi động lại, hoàn tất Secure Boot/MOK
  nếu cần, rồi tiếp tục cài. Bộ cài không tự khởi động lại máy.
- Nếu chạy ngoài gói desktop và thiếu PolicyKit, mở Terminal chạy
  `sudo apt install policykit-1` rồi mở lại ứng dụng.

## Model và mã nguồn

`download_models.py` tải UNet, Whisper, VAE cục bộ, InsightFace và GFPGAN cùng
model phụ của GFPGAN; phía MuseTalk có UNet 1.5, VAE, Whisper, DWPose, face parsing
và S3FD. Lỗi tải sẽ dừng bộ cài. Không gọi script upstream tự nâng thư viện.

Nguồn MuseTalk khóa tại commit trong `musetalk-source.json`, đã đối chiếu từ
[repository upstream](https://github.com/TMElyralab/MuseTalk/tree/0a89dec45a0192b824e3cf4daf96c239440c5ed8).
`prepare_musetalk.py` thêm tham số thư mục cache và giới hạn thời lượng avatar mà
ứng dụng cần. Chạy lại không lặp bản vá. Checkout khác commit hoặc có sửa đổi
không nhận diện sẽ được giữ nguyên và báo lỗi, không reset mã nguồn của người dùng.
Khóa commit không đồng nghĩa đã chứng nhận render trên mọi GPU.

## Kiểm tra hoàn tất

1. Kiểm tra phụ thuộc bằng `pip check` và CUDA ở từng môi trường.
2. Nạp runtime MuseTalk để chuẩn bị model detector phụ.
3. Import API mà không chạy startup/hàng đợi.
4. Render đoạn mẫu một giây từ `assets/demo1_*` bằng LatentSync (có GFPGAN) và
   MuseTalk, trong các tiến trình riêng. Hugging Face chạy ở chế độ offline.
5. Kiểm tra output có cả hình/âm thanh, thời lượng hợp lệ và giải mã được.
6. Chỉ sau khi tất cả đạt mới ghi `.environment-ready.json` và bỏ dấu đang cài.

Desktop kiểm tra receipt và mọi file trong `environment.py` trước khi mở backend.
Màn hình desktop hiển thị tiến trình theo 8 bước, thời gian đã chạy và 300 dòng log gần nhất; log đầy đủ ở `logs/setup.log`. Tiến trình theo bước không thể hiện phần trăm dung lượng tải hoặc thời gian còn lại. Đoạn render thử chỉ xác nhận
luồng kỹ thuật hoạt động, không phải đánh giá chất lượng lip-sync.

Kiểm tra nhẹ không cần tải model:

```bash
python3 -m unittest discover -s tests -p test_install_environment.py -v
bash -n install/setup.sh
cargo check --offline --manifest-path desktop/src-tauri/Cargo.toml
```
