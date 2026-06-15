"""OBS WebSocket v5 controller (obsws-python, port 4455).

Vì port 4455 = OBS WebSocket protocol v5 (OBS 28+), ta dùng `obsws-python` chứ KHÔNG
phải `obs-websocket-py` (lib đó nhắm protocol v4/port 4444 — request sẽ fail trên v5).

Thiết kế phòng thủ: mọi thao tác bọc try/except, không bao giờ ném lỗi ra ngoài làm
sập controller 24/7. Mất kết nối -> tự reconnect ở lần gọi sau (hoặc gọi reconnect()).
"""
import json
import logging
import os
import threading
import uuid

import obsws_python as obs

log = logging.getLogger("live")


class _LockingClient:
    """Bọc ReqClient để mọi lời gọi tự serialize qua 1 lock — tránh corrupt khi nhiều thread
    (run_forever + status poller + control) dùng chung 1 kết nối obsws-python."""

    def __init__(self, client, lock):
        object.__setattr__(self, "_c", client)
        object.__setattr__(self, "_lock", lock)

    def __getattr__(self, name):
        attr = getattr(object.__getattribute__(self, "_c"), name)
        if callable(attr):
            lock = object.__getattribute__(self, "_lock")
            def wrapped(*a, **k):
                with lock:
                    return attr(*a, **k)
            return wrapped
        return attr

# Source kind cho text khác nhau giữa OS: Linux = text_ft2_source_v2, Windows = text_gdiplus_v3.
_TEXT_KINDS = ("text_ft2_source_v2", "text_gdiplus_v3", "text_gdiplus_v2")

# Thứ tự z trong scene: phần tử [0] = DƯỚI CÙNG (nền), phần tử cuối = TRÊN CÙNG.
# (OBS: sceneItemIndex càng cao càng nằm trên.) Chỉ còn 5 source.
# "TVC" = Media Source (video), "TVC Image" = Image Source (ảnh tĩnh) — cùng vị trí, chỉ 1 cái hiện.
# (OBS Media Source không hiển thị được ảnh tĩnh, nên cần source ảnh riêng cho TVC dạng ảnh.)
SCENE_LAYER_ORDER = ["Background", "Bottom Bar", "TVC", "TVC Image", "Avatar Video", "Avatar Video 2",
                     "Product Image", "Banner", "Product Table"]

# Layout mặc định cho canvas dọc 1080x1920 — mỗi source là 1 hộp (bounds) tại (x,y) kích thước (w,h).
# SCALE_OUTER = phủ kín hộp (nền/banner); SCALE_INNER = vừa khít hộp giữ tỉ lệ (video/ảnh).
SCENE_DEFAULT_LAYOUT = {
    "Background":    {"x": 0,   "y": 0,    "w": 1080, "h": 1920, "bounds": "OBS_BOUNDS_SCALE_OUTER"},
    "Bottom Bar":    {"x": 0,   "y": 1620, "w": 1080, "h": 300,  "bounds": "OBS_BOUNDS_SCALE_OUTER"},
    "Product Table": {"x": 0,   "y": 1400, "w": 1080, "h": 520,  "bounds": "OBS_BOUNDS_SCALE_INNER"},
    "TVC":           {"x": 20,  "y": 1400, "w": 380,  "h": 250,  "bounds": "OBS_BOUNDS_SCALE_INNER"},
    "TVC Image":     {"x": 20,  "y": 1400, "w": 380,  "h": 250,  "bounds": "OBS_BOUNDS_SCALE_INNER"},
    "Avatar Video":  {"x": 60,  "y": 400,  "w": 650,  "h": 900,  "bounds": "OBS_BOUNDS_SCALE_INNER"},
    "Avatar Video 2":{"x": 60,  "y": 400,  "w": 650,  "h": 900,  "bounds": "OBS_BOUNDS_SCALE_INNER"},
    "Product Image": {"x": 750, "y": 800,  "w": 300,  "h": 300,  "bounds": "OBS_BOUNDS_SCALE_INNER"},
    "Banner":        {"x": 0,   "y": 0,    "w": 1080, "h": 200,  "bounds": "OBS_BOUNDS_SCALE_OUTER"},
}


class OBSController:
    def __init__(self, host="localhost", port=4455, password="obs123456"):
        self.host = host
        self.port = port
        self.password = password
        self.client = None
        self._lock = threading.RLock()  # serialize mọi OBS request giữa các thread
        self.event_client = None        # kênh nhận sự kiện (media phát xong) — tùy chọn
        self._on_media_end_cb = None
        self.connect()

    # ---------------------------------------------------------------- connection

    def connect(self):
        """Mở kết nối tới OBS. Trả True/False, không bao giờ raise."""
        try:
            self.client = _LockingClient(
                obs.ReqClient(host=self.host, port=self.port, password=self.password, timeout=5),
                self._lock,
            )
            v = self.client.get_version()
            log.info(f"OBS connected (OBS {v.obs_version}, WS {v.obs_web_socket_version})")
            if self._on_media_end_cb:   # (re)mở kênh sự kiện sau khi kết nối lại
                self._start_event_client()
            return True
        except Exception as e:
            self.client = None
            log.warning(f"OBS connect thất bại (OBS chưa mở?): {e}")
            return False

    def _safe_disconnect(self):
        self._stop_event_client()
        try:
            if self.client:
                self.client.disconnect()
        except Exception:
            pass
        self.client = None

    # ---------------------------------------------------------------- sự kiện (media phát xong)

    def enable_events(self, on_media_end):
        """Đăng ký callback nhận sự kiện media phát xong: on_media_end(input_name)."""
        self._on_media_end_cb = on_media_end
        self._start_event_client()

    def events_active(self):
        return self.event_client is not None

    def _start_event_client(self):
        if not self._on_media_end_cb:
            return
        self._stop_event_client()
        try:
            self.event_client = obs.EventClient(
                host=self.host, port=self.port, password=self.password, timeout=5)
            self.event_client.callback.register(self.on_media_input_playback_ended)
            log.info("OBS event client bật (chuyển video theo media-end)")
        except Exception as e:
            self.event_client = None
            log.warning(f"OBS event client lỗi (sẽ chuyển video theo thời lượng): {e}")

    def _stop_event_client(self):
        try:
            if self.event_client:
                self.event_client.disconnect()
        except Exception:
            pass
        self.event_client = None

    def on_media_input_playback_ended(self, data):
        """obsws-python ánh xạ tên hàm 'on_<event>' -> sự kiện MediaInputPlaybackEnded."""
        try:
            if self._on_media_end_cb:
                self._on_media_end_cb(getattr(data, "input_name", None))
        except Exception:
            pass

    def reconnect(self):
        log.info("OBS reconnect...")
        self._safe_disconnect()
        return self.connect()

    def is_connected(self):
        """True nếu đang kết nối (ping bằng get_version). Tự hạ cờ nếu rớt."""
        if not self.client:
            return False
        try:
            self.client.get_version()
            return True
        except Exception:
            self.client = None
            return False

    def _guard(self):
        """Đảm bảo có kết nối trước khi thao tác; thử connect 1 lần nếu chưa."""
        if self.client and self.is_connected():
            return True
        return self.connect()

    # ---------------------------------------------------------------- setters

    def set_video(self, source, path, restart=True):
        """Đặt file cho media source (ffmpeg_source). restart=True: phát lại ngay.
        restart=False: chỉ nạp file (dùng khi source đang ở scene ẩn — để scene-activate tự restart)."""
        if not self._guard():
            return False
        try:
            path = os.path.abspath(path)
            self.client.set_input_settings(
                source, {"local_file": path, "is_local_file": True}, True
            )
            if restart:
                try:  # phát lại từ đầu để video mới chạy ngay
                    self.client.trigger_media_input_action(
                        source, "OBS_WEBSOCKET_MEDIA_INPUT_ACTION_RESTART"
                    )
                except Exception:
                    pass
            return True
        except Exception as e:
            log.error(f"set_video('{source}') lỗi: {e}")
            self.client = None
            return False

    def set_text(self, source, text):
        if not self._guard():
            return False
        try:
            self.client.set_input_settings(source, {"text": str(text)}, True)
            return True
        except Exception as e:
            log.error(f"set_text('{source}') lỗi: {e}")
            self.client = None
            return False

    def set_image(self, source, path):
        if not self._guard():
            return False
        try:
            path = os.path.abspath(path) if path else ""   # path rỗng -> xóa ảnh (hiện trống)
            self.client.set_input_settings(source, {"file": path}, True)
            return True
        except Exception as e:
            log.error(f"set_image('{source}') lỗi: {e}")
            self.client = None
            return False

    def switch_scene(self, name):
        if not self._guard():
            return False
        try:
            self.client.set_current_program_scene(name)
            return True
        except Exception as e:
            log.error(f"switch_scene('{name}') lỗi: {e}")
            self.client = None
            return False

    # ---------------------------------------------------------------- stream

    def start_stream(self):
        if not self._guard():
            return False
        try:
            self.client.start_stream()
            log.info("Stream STARTED")
            return True
        except Exception as e:
            log.error(f"start_stream lỗi: {e}")
            return False

    def stop_stream(self):
        if not self._guard():
            return False
        try:
            self.client.stop_stream()
            log.info("Stream STOPPED")
            return True
        except Exception as e:
            log.error(f"stop_stream lỗi: {e}")
            return False

    def is_streaming(self):
        if not self._guard():
            return False
        try:
            return bool(self.client.get_stream_status().output_active)
        except Exception:
            return False

    # ---------------------------------------------------------------- introspection / setup

    def list_scenes(self):
        if not self._guard():
            return []
        try:
            return [s["sceneName"] for s in self.client.get_scene_list().scenes]
        except Exception as e:
            log.error(f"list_scenes lỗi: {e}")
            return []

    def list_sources(self):
        """Trả [(name, kind), ...] của tất cả input/source."""
        if not self._guard():
            return []
        try:
            return [(i["inputName"], i["inputKind"]) for i in self.client.get_input_list().inputs]
        except Exception as e:
            log.error(f"list_sources lỗi: {e}")
            return []

    def ensure_live_sources(self, scene="AI Host", video="Avatar Video",
                            image_src="Product Image", bg_src="Background",
                            banner_src="Banner", tvc_src="TVC"):
        """Tạo scene + 5 source nếu chưa có; xóa source text thừa; add chroma key; sắp z-order.
        KHÔNG đè vị trí source đã tồn tại (chỉ canh layout cho source mới)."""
        if not self._guard():
            return False
        try:
            scenes = self.list_scenes()
            if scene not in scenes:
                self.client.create_scene(scene)
                log.info(f"Tạo scene '{scene}'")
            existing = {n for n, _ in self.list_sources()}
            # Chỉ 5 source. Thứ tự z do reorder_sources lo.
            to_make = [
                (bg_src, "image_source", {"file": ""}),
                ("Bottom Bar", "color_source_v3", {"color": 0xCC303030, "width": 1080, "height": 300}),
                ("Product Table", "image_source", {"file": ""}),
                (tvc_src, "ffmpeg_source", {"is_local_file": True, "local_file": "", "looping": True}),
                ("TVC Image", "image_source", {"file": ""}),
                (video, "ffmpeg_source", {"is_local_file": True, "local_file": "", "looping": False}),
                ("Avatar Video 2", "ffmpeg_source", {"is_local_file": True, "local_file": "", "looping": False}),
                (image_src, "image_source", {"file": ""}),
                (banner_src, "image_source", {"file": ""}),
            ]
            created = []
            for sname, kind, settings in to_make:
                if sname in existing:
                    continue
                self.client.create_input(scene, sname, kind, settings, True)
                created.append(sname)
                log.info(f"Tạo source '{sname}' ({kind}) trong scene '{scene}'")
            # Xóa source text cũ không còn dùng (Product Name / Product Price).
            for leftover in ("Product Name", "Product Price"):
                if leftover in existing:
                    try:
                        self.client.remove_input(leftover)
                        log.info(f"Xóa source thừa '{leftover}'")
                    except Exception as e:
                        log.error(f"remove_input('{leftover}') lỗi: {e}")
            self.reorder_sources(scene)        # sắp z-order: Background xuống đáy (không đổi vị trí)
            self.ensure_chroma_key(video)      # tách nền xanh cho cả 2 source avatar
            self.ensure_chroma_key("Avatar Video 2")
            # Giữ FRAME CUỐI khi clip kết thúc (không xóa thành đen) + không đóng file khi ẩn
            # -> double-buffer chuyển clip liền mạch, không nháy đen.
            for av in (video, "Avatar Video 2"):
                try:
                    self.client.set_input_settings(av, {
                        "clear_on_media_end": False,    # giữ frame cuối (không xóa thành đen)
                        "close_when_inactive": False,   # không đóng file khi ẩn -> nạp nhanh hơn
                        "restart_on_activate": True,    # restart khi hiện (source ẩn KHÔNG phát liên tục được)
                    }, True)
                except Exception as e:
                    log.error(f"set avatar media settings '{av}' lỗi: {e}")
                self.ensure_filter(av, "Fade", "color_filter_v2", {"opacity": 1.0})  # cho crossfade
            if created:                        # CHỈ canh layout cho source MỚI -> không đè vị trí đã chỉnh tay
                self.apply_default_layout(scene)
            # Đảm bảo Avatar Video 2 nằm trong scene chính (gối đầu 1-scene), ẩn ban đầu, cùng vị trí Avatar Video.
            try:
                names = {i["sourceName"]: i["sceneItemId"]
                         for i in self.client.get_scene_item_list(scene).scene_items}
                if "Avatar Video 2" not in names:
                    self.client.create_scene_item(scene, "Avatar Video 2", False)
                    names = {i["sourceName"]: i["sceneItemId"]
                             for i in self.client.get_scene_item_list(scene).scene_items}
                if "Avatar Video" in names and "Avatar Video 2" in names:
                    t = self.client.get_scene_item_transform(scene, names["Avatar Video"]).scene_item_transform
                    self.client.set_scene_item_transform(scene, names["Avatar Video 2"], {
                        "positionX": t["positionX"], "positionY": t["positionY"],
                        "alignment": t.get("alignment", 5), "boundsAlignment": t.get("boundsAlignment", 0),
                        "boundsType": t["boundsType"], "boundsWidth": t["boundsWidth"],
                        "boundsHeight": t["boundsHeight"]})
            except Exception as e:
                log.error(f"ensure Avatar Video 2 trong scene lỗi: {e}")
            return True
        except Exception as e:
            log.error(f"ensure_live_sources lỗi: {e}")
            return False

    def _ensure_scene_b(self, scene_a, scene_b="AI Host 2"):
        """Tạo scene B = bản sao scene A (dùng chung source, KHỚP vị trí) nhưng thay Avatar Video
        bằng Avatar Video 2. Dùng để chuyển A<->B bằng Fade (che cú decode -> không nháy/đen)."""
        try:
            if scene_b not in self.list_scenes():
                self.client.create_scene(scene_b)
                log.info(f"Tạo scene '{scene_b}'")
            a_items = self.client.get_scene_item_list(scene_a).scene_items
            a_tf = {it["sourceName"]: self.client.get_scene_item_transform(scene_a, it["sceneItemId"]).scene_item_transform
                    for it in a_items}
            b_names = {i["sourceName"] for i in self.client.get_scene_item_list(scene_b).scene_items}
            # thêm các source chung (trừ avatar) + Avatar Video 2 vào B
            for it in a_items:
                n = it["sourceName"]
                if n in ("Avatar Video", "Avatar Video 2") or n in b_names:
                    continue
                self.client.create_scene_item(scene_b, n, True)
            if "Avatar Video 2" not in b_names:
                self.client.create_scene_item(scene_b, "Avatar Video 2", True)
            # bỏ Avatar Video 2 khỏi A (mỗi scene 1 avatar)
            for it in a_items:
                if it["sourceName"] == "Avatar Video 2":
                    self.client.remove_scene_item(scene_a, it["sceneItemId"])
            # copy transform A -> B cho khớp (Avatar Video 2 dùng transform của Avatar Video)
            for it in self.client.get_scene_item_list(scene_b).scene_items:
                n = it["sourceName"]
                ref = "Avatar Video" if n == "Avatar Video 2" else n
                t = a_tf.get(ref)
                if not t:
                    continue
                try:
                    self.client.set_scene_item_transform(scene_b, it["sceneItemId"], {
                        "positionX": t["positionX"], "positionY": t["positionY"],
                        "alignment": t.get("alignment", 5), "boundsAlignment": t.get("boundsAlignment", 0),
                        "boundsType": t["boundsType"], "boundsWidth": t["boundsWidth"],
                        "boundsHeight": t["boundsHeight"]})
                except Exception:
                    pass
            self.reorder_sources(scene_b)
            return True
        except Exception as e:
            log.error(f"_ensure_scene_b lỗi: {e}")
            return False

    def _setup_fade_transition(self, duration=150):
        """Đặt hiệu ứng chuyển cảnh = Fade (Mờ dần) + thời lượng (ms)."""
        try:
            trs = [t["transitionName"] for t in self.client.get_scene_transition_list().transitions]
            fade = next((t for t in trs if "fade" in t.lower() or "mờ" in t.lower()), None)
            if fade:
                self.client.set_current_scene_transition(fade)
                self.client.set_current_scene_transition_duration(int(duration))
                log.info(f"Transition Fade '{fade}' {duration}ms")
            return True
        except Exception as e:
            log.error(f"_setup_fade_transition lỗi: {e}")
            return False

    def _pick_text_kind(self):
        """Chọn text source kind mà OBS này hỗ trợ (Linux vs Windows)."""
        try:
            kinds = set(self.client.get_input_kind_list().input_kinds)
            for k in _TEXT_KINDS:
                if k in kinds:
                    return k
        except Exception:
            pass
        return _TEXT_KINDS[0]

    # ---------------------------------------------------------------- dashboard (W1)

    def get_stats(self):
        """Số liệu OBS (fps, cpu%, RAM MB, frames). Trả dict rỗng nếu lỗi."""
        if not self._guard():
            return {}
        try:
            s = self.client.get_stats()
            return {
                "fps": round(getattr(s, "active_fps", 0) or 0, 1),
                "cpu": round(getattr(s, "cpu_usage", 0) or 0, 1),
                "memory_mb": round(getattr(s, "memory_usage", 0) or 0, 1),
                "render_total": getattr(s, "render_total_frames", 0),
                "render_skipped": getattr(s, "render_skipped_frames", 0),
                "output_total": getattr(s, "output_total_frames", 0),
                "output_skipped": getattr(s, "output_skipped_frames", 0),
            }
        except Exception as e:
            log.error(f"get_stats lỗi: {e}")
            return {}

    def get_stream_status_detail(self):
        """Trạng thái stream: active, duration (s), bytes, dropped frames, congestion."""
        if not self._guard():
            return {}
        try:
            st = self.client.get_stream_status()
            return {
                "active": bool(st.output_active),
                "duration_ms": getattr(st, "output_duration", 0) or 0,
                "bytes": getattr(st, "output_bytes", 0) or 0,
                "skipped": getattr(st, "output_skipped_frames", 0) or 0,
                "total": getattr(st, "output_total_frames", 0) or 0,
                "congestion": round(getattr(st, "output_congestion", 0) or 0, 3),
            }
        except Exception:
            return {}

    def get_record_status(self):
        if not self._guard():
            return {}
        try:
            r = self.client.get_record_status()
            return {"active": bool(r.output_active), "duration_ms": getattr(r, "output_duration", 0) or 0}
        except Exception:
            return {}

    def get_program_screenshot(self, width=480):
        """Ảnh scene chương trình hiện tại dạng data URL (cho khung PREVIEW). None nếu lỗi."""
        if not self._guard():
            return None
        try:
            scene = self.client.get_current_program_scene().current_program_scene_name
            height = int(width * 9 / 16)
            try:  # tính theo tỉ lệ canvas thật để khỏi méo
                v = self.client.get_video_settings()
                bw, bh = getattr(v, "base_width", 0), getattr(v, "base_height", 0)
                if bw and bh:
                    height = max(8, int(width * bh / bw))
            except Exception:
                pass
            r = self.client.get_source_screenshot(scene, "jpg", width, height, 60)
            return r.image_data  # 'data:image/jpg;base64,...'
        except Exception:
            return None

    def start_recording(self):
        if not self._guard():
            return False
        try:
            self.client.start_record(); log.info("Recording STARTED"); return True
        except Exception as e:
            log.error(f"start_recording lỗi: {e}"); return False

    def stop_recording(self):
        if not self._guard():
            return False
        try:
            self.client.stop_record(); log.info("Recording STOPPED"); return True
        except Exception as e:
            log.error(f"stop_recording lỗi: {e}"); return False

    def set_volume(self, source, mul):
        """Đặt âm lượng theo hệ số tuyến tính 0..1."""
        if not self._guard():
            return False
        try:
            self.client.set_input_volume(source, vol_mul=float(mul)); return True
        except Exception as e:
            log.error(f"set_volume('{source}') lỗi: {e}"); return False

    def toggle_mute(self, source):
        if not self._guard():
            return False
        try:
            self.client.toggle_input_mute(source); return True
        except Exception as e:
            log.error(f"toggle_mute('{source}') lỗi: {e}"); return False

    def ensure_chroma_key(self, source, color="green"):
        """Thêm filter Chroma Key (tách nền xanh) cho source nếu chưa có. Idempotent."""
        if not self._guard():
            return False
        try:
            fl = self.client.get_source_filter_list(source).filters
            if any(f["filterName"] == "Chroma Key" for f in fl):
                return True
            self.client.create_source_filter(
                source, "Chroma Key", "chroma_key_filter_v2",
                {"key_color_type": color, "similarity": 400, "smoothness": 80,
                 "key_spill_reduction": 100, "opacity": 1.0})
            log.info(f"Thêm Chroma Key cho '{source}'")
            return True
        except Exception as e:
            log.error(f"ensure_chroma_key('{source}') lỗi: {e}")
            return False

    def apply_default_layout(self, scene, layout=None):
        """Đặt vị trí/kích thước mặc định: mỗi source = 1 hộp bounds tại (x,y) kích thước (w,h)."""
        layout = layout or SCENE_DEFAULT_LAYOUT
        if not self._guard():
            return False
        try:
            items = {i["sourceName"]: i["sceneItemId"]
                     for i in self.client.get_scene_item_list(scene).scene_items}
        except Exception as e:
            log.error(f"apply_default_layout list lỗi: {e}")
            return False
        ok = True
        for src, p in layout.items():
            iid = items.get(src)
            if iid is None:
                continue
            tf = {"positionX": float(p["x"]), "positionY": float(p["y"]), "alignment": 5,
                  "boundsType": p["bounds"], "boundsAlignment": 0,
                  "boundsWidth": float(p["w"]), "boundsHeight": float(p["h"])}
            try:
                self.client.set_scene_item_transform(scene, iid, tf)
            except Exception as e:
                log.error(f"apply_default_layout {src} lỗi: {e}")
                ok = False
        log.info(f"Đặt layout mặc định scene '{scene}'")
        return ok

    def place_above(self, scene, src, other):
        """Đưa src lên trên other trong z-order (cho double-buffer: giữ clip cũ che clip mới đang nạp)."""
        if not self._guard():
            return False
        try:
            items = self.client.get_scene_item_list(scene).scene_items
            idx = {i["sourceName"]: i["sceneItemIndex"] for i in items}
            ids = {i["sourceName"]: i["sceneItemId"] for i in items}
            if src not in ids or other not in ids or idx[src] > idx[other]:
                return True
            self.client.set_scene_item_index(scene, ids[src], idx[other])
            return True
        except Exception as e:
            log.error(f"place_above lỗi: {e}")
            return False

    def ensure_filter(self, source, name, kind, settings):
        """Thêm filter cho source nếu chưa có (idempotent)."""
        if not self._guard():
            return False
        try:
            fl = self.client.get_source_filter_list(source).filters
            if any(f["filterName"] == name for f in fl):
                return True
            self.client.create_source_filter(source, name, kind, settings)
            return True
        except Exception as e:
            log.error(f"ensure_filter('{source}','{name}') lỗi: {e}")
            return False

    def set_source_opacity(self, source, opacity):
        """Đặt độ mờ (0..1) qua filter 'Fade' (color_filter_v2) — cho crossfade chuyển clip."""
        if not self._guard():
            return False
        try:
            self.client.set_source_filter_settings(source, "Fade", {"opacity": float(opacity)}, True)
            return True
        except Exception:
            return False

    def swap_visible(self, scene, show_src, hide_src):
        """Ẩn/hiện 2 source ATOMIC trong CÙNG 1 frame (RequestBatch executionType=SerialFrame)
        -> chuyển clip không bị chớp/lệch frame."""
        if not self._guard():
            return False
        try:
            with self._lock:
                items = {i["sourceName"]: i["sceneItemId"]
                         for i in self.client.get_scene_item_list(scene).scene_items}
                if show_src not in items or hide_src not in items:
                    return False
                ws = self.client._c.base_client.ws   # websocket nền của ReqClient
                msg = {"op": 8, "d": {"requestId": uuid.uuid4().hex, "haltOnFailure": False,
                       "executionType": 1,   # SerialFrame: thực thi trong cùng 1 frame
                       "requests": [
                           {"requestType": "SetSceneItemEnabled",
                            "requestData": {"sceneName": scene, "sceneItemId": items[show_src],
                                            "sceneItemEnabled": True}},
                           {"requestType": "SetSceneItemEnabled",
                            "requestData": {"sceneName": scene, "sceneItemId": items[hide_src],
                                            "sceneItemEnabled": False}},
                       ]}}
                ws.send(json.dumps(msg))
                ws.recv()
            return True
        except Exception as e:
            log.error(f"swap_visible lỗi: {e}")
            return False

    def get_media_state(self, source):
        """Trạng thái media của 1 source (vd OBS_MEDIA_STATE_PLAYING). None nếu lỗi."""
        if not self._guard():
            return None
        try:
            return self.client.get_media_input_status(source).media_state
        except Exception:
            return None

    def media_status(self, source):
        """(state, cursor_ms) của media source — cho cơ chế gối đầu (biết khi gần hết clip)."""
        if not self._guard():
            return (None, None)
        try:
            st = self.client.get_media_input_status(source)
            return (st.media_state, st.media_cursor)
        except Exception:
            return (None, None)

    def reorder_sources(self, scene, order=None):
        """Sắp xếp lại z-order các source trong scene. order[0] = dưới cùng (nền).
        Bỏ qua source không có trong scene. Trả True/False."""
        order = order or SCENE_LAYER_ORDER
        if not self._guard():
            return False
        try:
            items = self.client.get_scene_item_list(scene).scene_items
            id_map = {i["sourceName"]: i["sceneItemId"] for i in items}
            for index, name in enumerate(order):
                if name in id_map:
                    self.client.set_scene_item_index(scene, id_map[name], index)
            log.info(f"Sắp z-order scene '{scene}': {[n for n in order if n in id_map]}")
            return True
        except Exception as e:
            log.error(f"reorder_sources lỗi: {e}")
            return False

    def get_scene_item_transform(self, scene, source):
        """Lấy transform (vị trí/scale) hiện tại của 1 source trong scene. None nếu lỗi."""
        if not self._guard():
            return None
        try:
            items = self.client.get_scene_item_list(scene).scene_items
            item = next((i for i in items if i["sourceName"] == source), None)
            if not item:
                return None
            r = self.client.get_scene_item_transform(scene, item["sceneItemId"])
            return r.scene_item_transform
        except Exception as e:
            log.error(f"get_scene_item_transform lỗi: {e}")
            return None

    def set_scene_item_transform(self, scene, source, transform):
        """Đặt transform (dict: positionX/Y, scaleX/Y...) cho 1 source trong scene."""
        if not self._guard():
            return False
        try:
            items = self.client.get_scene_item_list(scene).scene_items
            item = next((i for i in items if i["sourceName"] == source), None)
            if not item:
                return False
            self.client.set_scene_item_transform(scene, item["sceneItemId"], transform)
            return True
        except Exception as e:
            log.error(f"set_scene_item_transform lỗi: {e}")
            return False

    def set_source_visible(self, scene, source, visible):
        """Ẩn/hiện 1 source trong scene (SetSceneItemEnabled qua scene_item_id)."""
        if not self._guard():
            return False
        try:
            items = self.client.get_scene_item_list(scene).scene_items
            item = next((i for i in items if i["sourceName"] == source), None)
            if not item:
                return False
            self.client.set_scene_item_enabled(scene, item["sceneItemId"], bool(visible))
            return True
        except Exception as e:
            log.error(f"set_source_visible lỗi: {e}"); return False

    def set_stream_service(self, server, key, use_auth=False):
        """Đặt đích RTMP tùy ý (rtmp_custom) — server + stream key. Cho TikTok/Shopee/YouTube/FB."""
        if not self._guard():
            return False
        try:
            self.client.set_stream_service_settings(
                "rtmp_custom", {"server": server or "", "key": key or "", "use_auth": bool(use_auth)})
            log.info(f"Set RTMP service: {server}")
            return True
        except Exception as e:
            log.error(f"set_stream_service lỗi: {e}")
            return False

    def get_profiles(self):
        if not self._guard():
            return []
        try:
            return list(self.client.get_profile_list().profiles)
        except Exception:
            return []

    def set_profile(self, name):
        if not self._guard() or not name:
            return False
        try:
            self.client.set_current_profile(name)
            return True
        except Exception as e:
            log.error(f"set_profile lỗi: {e}")
            return False

    def media_action(self, source, action="restart"):
        """action: 'restart' | 'pause' | 'play' | 'stop' | 'next' | 'previous'."""
        if not self._guard():
            return False
        amap = {
            "restart": "OBS_WEBSOCKET_MEDIA_INPUT_ACTION_RESTART",
            "pause": "OBS_WEBSOCKET_MEDIA_INPUT_ACTION_PAUSE",
            "play": "OBS_WEBSOCKET_MEDIA_INPUT_ACTION_PLAY",
            "stop": "OBS_WEBSOCKET_MEDIA_INPUT_ACTION_STOP",
        }
        try:
            self.client.trigger_media_input_action(source, amap.get(action, amap["restart"]))
            return True
        except Exception as e:
            log.error(f"media_action lỗi: {e}"); return False


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    c = OBSController()
    print("is_connected:", c.is_connected())
    print("scenes:", c.list_scenes())
    print("sources:", c.list_sources())
    print("streaming:", c.is_streaming())
