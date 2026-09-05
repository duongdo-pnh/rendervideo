# MuseTalk Streaming MVP — cách chạy

Tài liệu kiến trúc và acceptance checklist nằm tại
`docs/MUSETALK_REALTIME_STREAMING.md`.

## Khởi động service

Service phải chạy trong environment MuseTalk có đủ model và dependency:

```bash
cd /home/byscom/Latentsync/rendervideo
engines/MuseTalk/.venv/bin/python musetalk_stream_api.py --host 127.0.0.1 --port 8091
```

Process này load model đúng một lần và chỉ cho phép một stream active trên GPU.

## Tạo stream

```bash
curl -X POST http://127.0.0.1:8091/api/streams \
  -H 'Content-Type: application/json' \
  -d '{
    "session_id": "live-001",
    "avatar_id": "avatar-01",
    "avatar_video": "/absolute/path/avatar.mp4",
    "push_url": "rtmp://server/app/stream-key",
    "fps": 25,
    "resolution": "720",
    "warmup_frames": 25,
    "batch_size": 20
  }'
```

## Enqueue audio

```bash
curl -X POST http://127.0.0.1:8091/api/streams/live-001/enqueue \
  -H 'Content-Type: application/json' \
  -d '{
    "request_id": "sentence-123",
    "audio_path": "/absolute/path/sentence.wav",
    "priority": 10,
    "interrupt": false
  }'
```

## Status, interrupt và stop

```bash
curl http://127.0.0.1:8091/api/streams/live-001
curl -X POST http://127.0.0.1:8091/api/streams/live-001/interrupt
curl -X DELETE http://127.0.0.1:8091/api/streams/live-001
```

Status không chứa RTMP URL/stream key. Log event cũng không ghi request body,
credential hoặc PCM.

## Kiểm tra local

```bash
python3 -m py_compile \
  musetalk_streaming/*.py \
  musetalk_stream_api.py \
  tests/test_musetalk_streaming.py

python3 -m unittest tests.test_musetalk_streaming -v
```

Unit test cần `numpy` và `opencv-python`. GPU acceptance/benchmark cần source,
virtualenv và model MuseTalk trong `engines/MuseTalk`; các artifact này không có
trong working tree hiện tại nên không thể xác nhận FPS, AV drift thực tế hoặc soak
test RTMP chỉ từ checkout này.

## Gửi file audio trực tiếp bằng Postman

Backend phải có session đang chạy trước (mặc định `facebook-live`). Trong Postman:

1. Chọn `POST http://127.0.0.1:8091/api/streams/facebook-live/enqueue-file`.
2. Mở **Body → form-data**.
3. Thêm `audio`, đổi kiểu từ Text sang **File**, rồi chọn file audio.
4. Thêm `request_id` dạng Text, ví dụ `postman-001`.
5. Có thể thêm `priority=0` và `interrupt=false`.
6. Bấm **Send**. HTTP `202` nghĩa là audio đã vào hàng đợi.

Hỗ trợ WAV, MP3, M4A, AAC, FLAC, OGG và OPUS; tối đa 100 MB. File upload được xóa tự động sau khi render, interrupt hoặc dừng session.

```bash
curl -X POST http://127.0.0.1:8091/api/streams/facebook-live/enqueue-file \
  -F "audio=@/duong/dan/audio.wav" \
  -F "request_id=postman-001" \
  -F "priority=0" \
  -F "interrupt=false"
```

Theo dõi tiến trình:

```bash
curl http://127.0.0.1:8091/api/streams/facebook-live
```

