# Kế hoạch tích hợp MuseTalk Realtime Streaming

## 1. Mục tiêu

Bổ sung chế độ livestream cho MuseTalk mà không thay đổi hành vi render MP4 hiện tại:

- `mode=batch`: dùng pipeline hiện tại, tạo ảnh tạm, silent video rồi mux MP4.
- `mode=stream`: frame được blend ngay sau từng GPU batch và chuyển trực tiếp vào pipeline audio/video có queue giới hạn; không tạo PNG hoặc MP4 trung gian.
- MVP dùng RTMP qua một tiến trình FFmpeg tồn tại suốt session.
- Thiết kế output độc lập với inference để có thể bổ sung WHIP, WebRTC và file output.

LatentSync, queue job, database job, Google Drive upload, GPU lock và batch API hiện tại không thuộc nhánh streaming và phải tiếp tục hoạt động như trước.

## 2. Hiện trạng codebase

Các điểm tích hợp hiện có:

- `musetalk_adapter.py` khởi động persistent MuseTalk server và gửi một JSON request qua Unix socket.
- `musetalk_render_server.py` load model một lần, cache `Avatar` trong RAM, gom các batch request tương thích rồi inference chung.
- `_infer_group()` hiện giữ toàn bộ decoded frame của mỗi request trong RAM.
- `_encode_job()` hiện blend toàn bộ frame ra PNG, encode silent MP4 rồi mux audio.
- Request cũ chưa có trường `type`; vì vậy server phải coi request không có `type` là `batch`.

Các khoảng trống cần xử lý:

- Chưa có abstraction output, bounded AV queue, master clock, idle loop hoặc reconnect.
- Chưa có lifecycle/session registry.
- Chưa có HTTP API cho stream.
- Inference và encode batch đang phụ thuộc vào danh sách toàn bộ frame.
- Chưa có benchmark tách inference, blend và encode.

## 3. Quyết định kiến trúc

### 3.1 Ranh giới module

Thêm package `musetalk_streaming/`:

```text
musetalk_streaming/
├── __init__.py
├── models.py          # state, config, packet, metric snapshot
├── output.py          # StreamOutput ABC, FFmpeg RTMP implementation
├── synchronizer.py    # monotonic clock, bounded AV buffers, playout
├── session.py         # one stream lifecycle and sentence priority queue
├── manager.py         # single-GPU session registry
└── api.py             # HTTP API factory/router
```

`musetalk_render_server.py` vẫn là process sở hữu model/GPU/avatar. Nó được mở rộng bằng request có `type`:

- thiếu `type` hoặc `type=batch`: đường cũ, backward compatible.
- `type=stream_enqueue`: tạo audio feature, inference theo GPU batch, blend từng frame và gửi ngay cho session output.
- Các command lifecycle (`stream_start`, `stream_status`, `stream_interrupt`, `stream_stop`) được dispatch riêng, không đi qua batch coalescing.

HTTP API là lớp mỏng gọi adapter/Unix socket; stream key không được ghi log hoặc trả lại từ status.

### 3.2 Luồng dữ liệu

```text
sentence priority queue
        |
        v
MuseTalk batch inference
        |
        v
blend từng decoded batch
        |
        v
bounded speaking-frame buffer + PCM 16 kHz
        |
        v
AV synchronizer (monotonic 25 FPS)
   |                    |
idle/repeat frame       silence/resample
   |                    |
   +---------+----------+
             v
    persistent FFmpeg RTMP
```

Inference tạo dữ liệu nhanh nhất có thể nhưng playout luôn theo clock 25 FPS. Timestamp dựa trên chỉ số frame/sample, không dựa trên thời gian inference.

### 3.3 FFmpeg transport

MVP dùng một FFmpeg process với hai raw inputs:

- video: `rawvideo`, BGR24, fixed width/height, 25 FPS.
- audio: mono signed PCM 16-bit, 16 kHz; FFmpeg resample lên 48 kHz và encode AAC.
- output: FLV/RTMP, H.264 `yuv420p`, GOP 50, `zerolatency`, bitrate cấu hình được.

Hai pipe OS riêng được truyền bằng file descriptor. Chỉ output worker được phép ghi pipe. URL được giữ trong config private và mọi log phải đi qua hàm mask URL.

Nếu FFmpeg chết, output chuyển `RECONNECTING`, đóng đủ pipe/process cũ, backoff `1, 2, 4, 8, ... 30` giây, mở lại một process và phát frame hiện tại ngay; encoder được cấu hình closed GOP/keyframe sớm. Stop phải signal worker, đóng pipe và terminate/kill có timeout để không còn zombie.

## 4. Đồng bộ audio/video

Thông số cố định của MVP:

- Video: 25 FPS, một frame = 40 ms.
- Audio nguồn: mono PCM 16 kHz.
- Một video frame đi cùng 640 sample nguồn = hai chunk 20 ms × 320 sample.
- Audio output: AAC 48 kHz, 128 kbps.
- Warm-up mặc định: 25–50 speaking frame.
- Video queue tối đa: 250 frame.
- Audio queue tối đa: 160.000 sample nguồn (10 giây).

Một `AVPacket` logic chứa đúng một video frame và tối đa 640 sample PCM tương ứng. Chỉ packet hoàn chỉnh được đưa vào speaking buffer, nhờ đó audio không thể chạy trước video đã render.

Output tick theo `next_deadline = epoch + frame_index / 25`:

- Có speaking packet: phát frame và 640 sample tương ứng.
- Đang buffering và chưa đủ render-ahead: phát idle frame + silence.
- Inference chậm/queue rỗng giữa câu: lặp frame speaking cuối + silence; không phát audio tương lai.
- Không có câu: phát frame tiếp theo của avatar idle loop + silence.
- Chậm clock: không drop audio; video chỉ được drop khi recovery và phải tăng metric.

AV drift được tính từ video timeline và số sample đã gửi sau quy đổi. Mục tiêu vận hành là `abs(av_drift_ms) <= 80`.

## 5. State machine

```text
CREATED -> STARTING -> IDLE <-> BUFFERING -> PLAYING
                       |          |           |
                       +----------+-----------+
                                  |
                           RECONNECTING

mọi trạng thái -> STOPPING -> STOPPED
lỗi không khôi phục được -> FAILED
```

State transition được bảo vệ bằng lock. Status trả snapshot, không trả object mutable hoặc credential.

MVP chỉ cho phép một session active vì một GPU. `POST /api/streams` thứ hai nhận lỗi conflict rõ ràng.

## 6. Sentence queue, priority và interrupt

- Queue dùng priority giảm dần, sau đó sequence tăng dần để giữ FIFO giữa các request cùng priority.
- `request_id` phải duy nhất trong một session.
- Event lifecycle: `queued`, `buffering`, `started`, `completed`, `interrupted`, `failed`.
- `interrupt=false`: không ảnh hưởng câu hiện tại.
- `interrupt=true` hoặc endpoint interrupt:
  - xóa câu chưa phát;
  - đặt cancellation token cho câu hiện tại;
  - inference dừng tại ranh giới GPU batch;
  - synchronizer chỉ bỏ phần packet chưa phát của request đó;
  - stream chuyển về idle hoặc câu ưu tiên kế tiếp mà không đóng FFmpeg.

## 7. API

Các endpoint MVP:

```text
POST   /api/streams
POST   /api/streams/{session_id}/enqueue
POST   /api/streams/{session_id}/interrupt
GET    /api/streams/{session_id}
DELETE /api/streams/{session_id}
```

Validation quan trọng:

- `fps` chỉ nhận 25 trong MVP.
- video/audio path phải tồn tại và là file.
- scheme push URL phải là `rtmp` hoặc `rtmps`.
- resolution cố định trong session; MVP chuẩn hóa avatar sang 1280×720 tại output.
- duplicate session/request trả conflict.
- API không trả `push_url`.

HTTP framework không được ép vào process GPU. `api.py` cung cấp factory và có thể mount vào web service hiện tại; adapter giữ Unix socket protocol làm ranh giới process.

## 8. Backpressure và quản lý bộ nhớ

- Tất cả sentence/video/audio queue đều có `maxsize`.
- GPU producer chờ bằng timeout ngắn khi video buffer đầy và kiểm tra cancellation/stop giữa các lần chờ.
- Không giữ toàn bộ decoded frame của câu: VAE output của một GPU batch được blend, enqueue rồi release trước batch kế.
- Tensor batch được xóa trong `finally`; CUDA OOM gọi `empty_cache()`, đánh dấu request lỗi, nhưng server tiếp tục nhận request.
- Không drop audio ngẫu nhiên.
- Queue và cache không tăng theo uptime.

## 9. Metrics và bảo mật log

Status/metrics tối thiểu:

- state, avatar ID, current request ID, uptime;
- inference FPS, output FPS;
- GPU batch time, blend time;
- video buffer frames, audio buffer ms;
- AV drift ms, dropped frames, reconnect count;
- time-to-first-frame theo request;
- GPU allocated memory.

Stream key luôn mask thành dạng `rtmp://host/app/***`. Không log request body chứa `push_url`, credential hoặc PCM.

## 10. Kế hoạch triển khai

### Phase 1 — benchmark gate

- [ ] Thêm benchmark cache HIT đo audio feature, inference, blend và encode.
- [ ] Chạy với avatar/resolution mục tiêu trên GPU thật.
- [ ] Chỉ tiếp tục RTMP production rollout nếu inference ổn định đạt ít nhất 25 FPS.

Lưu ý: phần mềm có thể được xây dựng và unit-test trên máy không GPU, nhưng kết quả benchmark là deployment gate bắt buộc.

### Phase 2 — streaming core

- [ ] Thêm data model/state machine.
- [ ] Thêm `StreamOutput` và output kiểm thử.
- [ ] Thêm bounded synchronizer và idle/silence.
- [ ] Refactor inference thành iterator theo decoded GPU batch.
- [ ] Blend/enqueue theo batch, không ghi file trung gian.
- [ ] Giữ `_render_group()` batch tương thích tuyệt đối.

### Phase 3 — RTMP

- [ ] Thêm persistent FFmpeg output.
- [ ] Codec/GOP/bitrate/low-latency flags.
- [ ] Reconnect exponential backoff.
- [ ] Mask URL và process cleanup.

### Phase 4 — lifecycle/API

- [ ] Session manager một GPU/một stream.
- [ ] start/enqueue/interrupt/status/stop.
- [ ] Event lifecycle và metrics.
- [ ] Mount API vào service được chọn.

### Phase 5 — kiểm thử và vận hành

- [ ] Unit test state, priority, bounded queue, clock và URL masking.
- [ ] Integration test bằng FFmpeg/file sink không cần RTMP public.
- [ ] Regression test batch request cũ không có `type`.
- [ ] GPU test cache HIT, incremental first frame và OOM recovery.
- [ ] Soak test RTMP 2 giờ, reconnect và memory plateau.
- [ ] Xác minh AV drift trong ±80 ms.

WHIP/WebRTC chỉ bắt đầu sau khi toàn bộ acceptance criteria RTMP đạt.

## 11. Chiến lược tương thích

- Không đổi signature `musetalk_adapter.render(...)`.
- Adapter batch gửi thêm `"type": "batch"` nhưng server vẫn nhận request legacy không có type.
- Không sửa schema database trong MVP; job không có `mode` tiếp tục là batch.
- Không thay đổi `render_job.py` hoặc `queue_worker.py` trừ khi cần truyền rõ `mode=batch`.
- Streaming dùng API/session riêng, không chiếm queue database cũ.
- Batch encoder cũ được giữ trong giai đoạn đầu để tránh thay đổi chất lượng output.

## 12. Definition of done

Chỉ coi hoàn tất production khi:

- Batch MuseTalk và LatentSync regression đều pass.
- Frame speaking đầu được output trước khi inference hết câu.
- Không có PNG/MP4 trung gian ở stream mode.
- 25 FPS ổn định, drift trong ±80 ms.
- Hai câu không chồng audio; interrupt không đóng RTMP.
- Reconnect thành công và stream key được mask.
- Stop giải phóng process, FD, socket, thread và queue.
- Soak test 2 giờ không tăng RAM/VRAM liên tục.
- Metrics đủ để phân biệt bottleneck inference, blend, encode và network.

## 13. Rủi ro cần xác minh sớm

- MuseTalk inference thực tế có thể dưới 25 FPS ở 720p; benchmark là gate, không được che bằng buffer.
- FFmpeg raw dual-pipe có thể backpressure khi network chậm; writer/reconnect cần test bằng fault injection.
- Avatar source có resolution/FPS khác nhau; idle normalization phải nhất quán với output session.
- Audio duration và số whisper chunk có thể lệch ở cuối câu; cần pad/trim PCM đúng `frame_count * 640`.
- Cache avatar hiện xóa cache không hoàn chỉnh; lifecycle stream phải tránh xóa cache đang được session sử dụng.
- Batch grouping hiện giữ toàn bộ decoded frame; refactor iterator không được làm thay đổi thứ tự frame khi nhiều batch request được coalesce.
