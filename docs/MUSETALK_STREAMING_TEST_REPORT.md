# MuseTalk Streaming — báo cáo chạy A–Z

Ngày chạy: 2026-07-29  
GPU: NVIDIA GeForce RTX 3090 24 GB

## Environment

- Khôi phục `engines/MuseTalk` đúng commit gitlink:
  `0a89dec45a0192b824e3cf4daf96c239440c5ed8`.
- Tạo environment riêng `engines/MuseTalk/.venv`.
- MuseTalk environment:
  - Python 3.10
  - PyTorch 2.0.1 + CUDA 11.8
  - torchvision 0.15.2
  - mmcv 2.0.1 CUDA wheel
  - mmdet 3.1.0
  - mmpose 1.1.0
- RenderVideo environment chính được giữ ở PyTorch 2.5.1 + CUDA 12.1.
- Tải đủ MuseTalk V1.5 UNet, SD-VAE, Whisper tiny, DWPose,
  face parser và S3FD.

## Model-load

- Full MuseTalk runtime load: khoảng 28 giây.
- GPU allocated sau load: khoảng 2.15 GB.
- GPU reserved sau load: khoảng 7.19 GB.

## Unit/regression test

- 7/7 unit tests pass:
  - AutoVoice: 4
  - MuseTalk streaming core: 3
- Python compile pass cho streaming API/core và batch adapter/server.
- MuseTalk environment: `pip check` không có broken requirement.
- RenderVideo environment: package hoạt động; `pip check` chỉ báo wheel
  `decord 0.6.0` không khai báo hỗ trợ platform hiện tại, là trạng thái có sẵn.

## MuseTalk batch regression

Kết quả:

- Output: `test_outputs/musetalk_stream/batch_regression.mp4`
- Duration: 2.000 giây
- Video: H.264, 1280×720, 25 FPS
- Audio: AAC mono, 16 kHz
- Batch persistent server và MP4 mux pass.

## LatentSync regression

Kết quả:

- Output: `test_outputs/musetalk_stream/latentsync_regression.mp4`
- Duration: 2.080 giây
- Video: H.264, 1080×1920, 25 FPS
- Audio: AAC mono, 16 kHz
- Full diffusion/render/mux pass.

Lượt smoke đầu với output preset 480p thất bại vì test carrier sau resize
không được InsightFace nhận diện. Lượt sau dùng carrier đã scan detect đủ
50/50 frame và pass. Đây là lỗi lựa chọn input test, không phải regression.

## RTMP end-to-end

Capture cache-HIT:

- Output: `test_outputs/musetalk_stream/received_cache_hit.flv`
- Duration: 35.021 giây
- Video: H.264, 1280×720, 25 FPS
- Video frame thực nhận: 875 (= 35 × 25)
- Audio: AAC mono, 48 kHz
- First speaking frame cache-HIT: khoảng 589 ms.
- Output FPS: 25.03.
- AV drift metric: 0 ms.
- Dropped frame: 0.
- Frame idle và speaking có hash khác nhau; SSIM khoảng 0.958, xác nhận
  speaking frame đã được inference/blend rồi truyền qua RTMP.
- Stream duy trì idle/silence trước và sau câu mà không mở FFmpeg theo câu.

Capture dài 45 giây:

- Output: `test_outputs/musetalk_stream/received.flv`
- Duration: 45.021 giây
- Video/audio codec và timeline đúng.

## Benchmark gate

### Batch size 10

- 50 speaking frame
- Render FPS: 6.06
- Tổng GPU batch time: 3,687 ms
- Tổng blend time: 1,547 ms
- Time-to-first-frame: 5,909 ms trong process benchmark cold
- Output: 25.03 FPS nhờ idle/buffer

### Batch size 20

- 50 speaking frame
- Render FPS: 7.38
- Tổng GPU batch time: 4,752 ms
- Tổng blend time: 1,906 ms
- Time-to-first-frame: 4,347 ms
- Output: 25.05 FPS nhờ idle/buffer

## Kết luận gate

Streaming transport, idle loop, audio/video encode, API lifecycle, batch
MuseTalk và LatentSync đều chạy end-to-end. Tuy nhiên producer MuseTalk chỉ
đạt 6.06–7.38 FPS trên RTX 3090 với pipeline hiện tại, thấp hơn gate bắt buộc
25 FPS.

Theo requirement Phase 1:

- Không approve production realtime ở cấu hình hiện tại.
- Không chạy soak test 2 giờ vì benchmark gate đã fail.
- RTMP reconnect counter/backoff đã được kích hoạt khi receiver local dừng,
  nhưng reconnect capture dài hạn chưa được approve.
- Cần tối ưu inference/decode/blend hoặc dùng GPU nhanh hơn, sau đó benchmark
  lại đạt ít nhất 25 FPS trước khi chạy soak test 2 giờ.

## Cleanup

- Tất cả API, persistent MuseTalk server và FFmpeg receiver test đã dừng.
- Không còn compute process giữ GPU sau test.
- Test artifact được giữ trong `test_outputs/musetalk_stream/` để kiểm tra.
