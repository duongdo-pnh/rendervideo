from __future__ import annotations

import abc
import json
import os
import queue
import subprocess
import threading
import time
import urllib.request
import uuid
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import numpy as np


def mask_push_url(url: str) -> str:
    """Return a log-safe URL with credentials and stream key removed."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    segments = [segment for segment in parts.path.split("/") if segment]
    safe_path = "/" + "/".join(segments[:-1] + ["***"]) if segments else "/***"
    return urlunsplit((parts.scheme, host, safe_path, "", ""))


class StreamOutput(abc.ABC):
    def begin_request(self, request_id: str) -> None:
        """Optional sentence boundary hook for segmented outputs."""

    def end_request(self, request_id: str) -> None:
        """Optional sentence boundary hook for segmented outputs."""

    @abc.abstractmethod
    def start(self, width: int, height: int, fps: int, sample_rate: int) -> None:
        raise NotImplementedError

    @abc.abstractmethod
    def push_video_frame(self, frame: np.ndarray, pts: int | None = None) -> None:
        raise NotImplementedError

    @abc.abstractmethod
    def push_audio_frame(self, pcm: np.ndarray, pts: int | None = None) -> None:
        raise NotImplementedError

    @abc.abstractmethod
    def get_status(self) -> dict:
        raise NotImplementedError

    @abc.abstractmethod
    def stop(self) -> None:
        raise NotImplementedError


class FileStreamOutput(StreamOutput):
    """Raw frame/audio sink plus a JSONL timeline, intended for verification."""

    def __init__(self, directory: str | Path):
        self.directory = Path(directory)
        self._video = None
        self._audio = None
        self._timeline = None
        self._video_frames = 0
        self._audio_samples = 0
        self._lock = threading.Lock()

    def start(self, width: int, height: int, fps: int, sample_rate: int) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        self._video = open(self.directory / "video.bgr", "wb")
        self._audio = open(self.directory / "audio.s16le", "wb")
        self._timeline = open(self.directory / "timeline.jsonl", "w", encoding="utf-8")
        self._timeline.write(json.dumps({
            "type": "start", "width": width, "height": height,
            "fps": fps, "sample_rate": sample_rate,
        }) + "\n")

    def push_video_frame(self, frame: np.ndarray, pts: int | None = None) -> None:
        data = np.ascontiguousarray(frame, dtype=np.uint8)
        with self._lock:
            self._video.write(data.tobytes())
            self._timeline.write(json.dumps({"type": "video", "pts": pts}) + "\n")
            self._video_frames += 1

    def push_audio_frame(self, pcm: np.ndarray, pts: int | None = None) -> None:
        data = np.ascontiguousarray(pcm, dtype="<i2")
        with self._lock:
            self._audio.write(data.tobytes())
            self._timeline.write(json.dumps({
                "type": "audio", "pts": pts, "samples": int(data.size),
            }) + "\n")
            self._audio_samples += int(data.size)

    def get_status(self) -> dict:
        return {
            "running": self._video is not None,
            "video_frames": self._video_frames,
            "audio_samples": self._audio_samples,
        }

    def stop(self) -> None:
        with self._lock:
            for handle in (self._video, self._audio, self._timeline):
                if handle is not None:
                    handle.close()
            self._video = self._audio = self._timeline = None


class ReLiveClipOutput(StreamOutput):
    """Writes each finished MuseTalk sentence as MP4 and posts its path to ReLive."""

    def __init__(self, directory: str | Path, callback_url: str):
        self.directory = Path(directory)
        self.callback_url = callback_url
        self._spec = None
        self._request_id = None
        self._video = self._audio = None
        self._video_path = self._audio_path = None
        self._last_clip = self._last_error = None
        self._delivered = 0
        self._lock = threading.RLock()

    def start(self, width: int, height: int, fps: int, sample_rate: int) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        self._spec = (width, height, fps, sample_rate)

    def begin_request(self, request_id: str) -> None:
        with self._lock:
            if self._request_id == request_id:
                return
            if self._request_id is not None:
                self._finish_locked()
            token = uuid.uuid4().hex
            self._request_id = request_id
            self._video_path = self.directory / f".{token}.bgr"
            self._audio_path = self.directory / f".{token}.s16le"
            self._video = open(self._video_path, "wb")
            self._audio = open(self._audio_path, "wb")

    def push_video_frame(self, frame: np.ndarray, pts: int | None = None) -> None:
        del pts
        with self._lock:
            if self._video is not None:
                self._video.write(np.ascontiguousarray(frame, dtype=np.uint8).tobytes())

    def push_audio_frame(self, pcm: np.ndarray, pts: int | None = None) -> None:
        del pts
        with self._lock:
            if self._audio is not None:
                self._audio.write(np.ascontiguousarray(pcm, dtype="<i2").tobytes())

    def end_request(self, request_id: str) -> None:
        with self._lock:
            if self._request_id == request_id:
                self._finish_locked()

    def _finish_locked(self) -> None:
        request_id = self._request_id
        if request_id is None:
            return
        for handle in (self._video, self._audio):
            if handle is not None:
                handle.close()
        width, height, fps, sample_rate = self._spec
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in request_id)[:80]
        clip = self.directory / f"{int(time.time() * 1000)}-{safe or 'clip'}.mp4"
        command = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-f", "rawvideo", "-pix_fmt", "bgr24", "-s:v", f"{width}x{height}",
            "-r", str(fps), "-i", str(self._video_path),
            "-f", "s16le", "-ar", str(sample_rate), "-ac", "1", "-i", str(self._audio_path),
            "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-shortest", "-movflags", "+faststart", str(clip),
        ]
        try:
            subprocess.run(command, check=True, timeout=300)
            payload = json.dumps({"request_id": request_id, "file_path": str(clip)}).encode("utf-8")
            request = urllib.request.Request(self.callback_url, data=payload,
                headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(request, timeout=15) as response:
                if response.status >= 300:
                    raise RuntimeError(f"ReLive callback HTTP {response.status}")
            self._last_clip, self._last_error = str(clip), None
            self._delivered += 1
        except Exception as exc:
            self._last_error = f"{type(exc).__name__}: {exc}"
            clip.unlink(missing_ok=True)
        finally:
            self._video_path.unlink(missing_ok=True)
            self._audio_path.unlink(missing_ok=True)
            self._request_id = self._video = self._audio = None
            self._video_path = self._audio_path = None

    def get_status(self) -> dict:
        with self._lock:
            return {"running": self._spec is not None, "delivered_clips": self._delivered,
                    "last_clip": self._last_clip, "last_error": self._last_error}

    def stop(self) -> None:
        with self._lock:
            if self._request_id is not None:
                self._finish_locked()
            self._spec = None


class RTMPOutput(StreamOutput):
    """Persistent, reconnecting FFmpeg RTMP transport."""

    _STOP = object()

    def __init__(
        self,
        push_url: str,
        video_bitrate: str = "3500k",
        audio_bitrate: str = "128k",
        output_sample_rate: int = 44_100,
        audio_delay_ms: int = 300,
        queue_size: int = 100,
    ):
        if urlsplit(push_url).scheme not in {"rtmp", "rtmps"}:
            raise ValueError("push_url must use rtmp:// or rtmps://")
        self._push_url = push_url
        self._video_bitrate = video_bitrate
        self._audio_bitrate = audio_bitrate
        self._output_sample_rate = output_sample_rate
        self._audio_delay_ms = max(0, int(audio_delay_ms))
        self._video_queue = queue.Queue(maxsize=queue_size)
        self._audio_queue = queue.Queue(maxsize=queue_size)
        self._stop = threading.Event()
        self._process: subprocess.Popen | None = None
        self._threads: list[threading.Thread] = []
        self._lock = threading.RLock()
        self._reconnects = 0
        self._last_error: str | None = None
        self._spec: tuple[int, int, int, int] | None = None

    @property
    def safe_url(self) -> str:
        return mask_push_url(self._push_url)

    def start(self, width: int, height: int, fps: int, sample_rate: int) -> None:
        with self._lock:
            if self._process is not None:
                return
            self._spec = (width, height, fps, sample_rate)
            self._stop.clear()
            self._spawn()
            self._threads = [
                threading.Thread(target=self._writer, args=("video",), daemon=True),
                threading.Thread(target=self._writer, args=("audio",), daemon=True),
                threading.Thread(target=self._monitor, daemon=True),
            ]
            for thread in self._threads:
                thread.start()

    def _spawn(self) -> None:
        width, height, fps, sample_rate = self._spec
        video_read, video_write = os.pipe()
        audio_read, audio_write = os.pipe()
        command = [
            "ffmpeg", "-hide_banner", "-loglevel", "warning", "-nostdin",
            "-thread_queue_size", "512", "-f", "rawvideo", "-pix_fmt", "bgr24",
            "-s:v", f"{width}x{height}", "-r", str(fps), "-i", f"pipe:{video_read}",
            "-thread_queue_size", "512", "-f", "s16le", "-ar", str(sample_rate),
            "-ac", "1", "-i", f"pipe:{audio_read}",
            "-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency",
            "-pix_fmt", "yuv420p", "-g", str(fps * 2), "-keyint_min", str(fps),
            "-sc_threshold", "0", "-b:v", self._video_bitrate,
            "-maxrate", self._video_bitrate, "-bufsize", self._video_bitrate,
            "-c:a", "aac", "-ar", str(self._output_sample_rate), "-ac", "2",
            "-af", f"adelay={self._audio_delay_ms}:all=1",
            "-b:a", self._audio_bitrate, "-f", "flv", self._push_url,
        ]
        try:
            process = subprocess.Popen(
                command, pass_fds=(video_read, audio_read), close_fds=True,
            )
        finally:
            os.close(video_read)
            os.close(audio_read)
        self._process = process
        self._video_fd = video_write
        self._audio_fd = audio_write

    def _writer(self, kind: str) -> None:
        work_queue = self._video_queue if kind == "video" else self._audio_queue
        while not self._stop.is_set():
            try:
                payload = work_queue.get(timeout=0.25)
            except queue.Empty:
                continue
            if payload is self._STOP:
                return
            while not self._stop.is_set():
                with self._lock:
                    fd = getattr(self, f"_{kind}_fd", None)
                if fd is None:
                    time.sleep(0.05)
                    continue
                try:
                    view = memoryview(payload)
                    while view and not self._stop.is_set():
                        view = view[os.write(fd, view):]
                    break
                except (BrokenPipeError, OSError) as exc:
                    self._last_error = f"{type(exc).__name__}: {exc}"
                    time.sleep(0.05)

    def _monitor(self) -> None:
        backoff = 1
        while not self._stop.wait(0.25):
            with self._lock:
                process = self._process
                dead = process is not None and process.poll() is not None
            if not dead:
                continue
            if self._stop.wait(backoff):
                return
            with self._lock:
                self._close_process()
                try:
                    self._spawn()
                    self._reconnects += 1
                    backoff = 1
                    self._last_error = None
                except Exception as exc:
                    self._last_error = f"{type(exc).__name__}: {exc}"
                    backoff = min(backoff * 2, 30)

    def push_video_frame(self, frame: np.ndarray, pts: int | None = None) -> None:
        del pts
        self._video_queue.put(np.ascontiguousarray(frame, dtype=np.uint8).tobytes(), timeout=1)

    def push_audio_frame(self, pcm: np.ndarray, pts: int | None = None) -> None:
        del pts
        self._audio_queue.put(np.ascontiguousarray(pcm, dtype="<i2").tobytes(), timeout=1)

    def get_status(self) -> dict:
        with self._lock:
            return {
                "running": self._process is not None and self._process.poll() is None,
                "reconnecting": self._process is not None and self._process.poll() is not None,
                "reconnect_count": self._reconnects,
                "last_error": self._last_error,
                "video_queue": self._video_queue.qsize(),
                "audio_queue": self._audio_queue.qsize(),
                "target": self.safe_url,
            }

    def _close_process(self) -> None:
        for name in ("_video_fd", "_audio_fd"):
            fd = getattr(self, name, None)
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
                setattr(self, name, None)
        process, self._process = self._process, None
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)

    def stop(self) -> None:
        self._stop.set()
        for work_queue in (self._video_queue, self._audio_queue):
            try:
                work_queue.put_nowait(self._STOP)
            except queue.Full:
                pass
        with self._lock:
            self._close_process()
        for thread in self._threads:
            if thread is not threading.current_thread():
                thread.join(timeout=2)
        self._threads.clear()
