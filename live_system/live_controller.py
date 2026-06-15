"""Bộ điều phối live 24/7: lấy video chưa phát từ playlist, đẩy lên OBS, lặp vô hạn.

- sync_to_obs(video, product): set video + text + ảnh + đổi scene "AI Host".
- run_forever(): vòng lặp 24/7 (tự reconnect OBS, loop lại playlist khi hết, không sập vì lỗi).
- start_stream/stop_stream/next_video: cho UI gọi.
- Log ra live_system/logs/live.log (xoay vòng theo ngày, giữ 7 ngày) + giữ 200 dòng gần nhất cho UI.
"""
import logging
import logging.handlers
import random
import threading
import time
from collections import deque
from pathlib import Path  # noqa
from types import SimpleNamespace

import live_database as db
from obs_controller import OBSController

# Tên source/scene trong OBS (khớp ensure_live_sources).
SRC_VIDEO = "Avatar Video"
SRC_VIDEO2 = "Avatar Video 2"   # source avatar thứ 2 (double-buffer chống đen giữa 2 clip)
SRC_IMAGE = "Product Image"
SRC_BG = "Background"
SRC_BANNER = "Banner"
SRC_TABLE = "Product Table"   # ảnh bàn + sản phẩm ở đáy
SRC_TVC = "TVC"
SRC_TVC_IMG = "TVC Image"
SCENE_MAIN = "AI Host"
SCENE_B = "AI Host 2"   # scene phụ để Fade A<->B chống nháy khi chuyển clip
_IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp")

LOG_DIR = Path(__file__).parent / "logs"
LOG_FILE = LOG_DIR / "live.log"

log = logging.getLogger("live")

# Buffer log trong RAM cho Tab 3 (gắn qua setup_logging).
_LOG_BUFFER = deque(maxlen=200)


class _BufferHandler(logging.Handler):
    def emit(self, record):
        try:
            _LOG_BUFFER.append(self.format(record))
        except Exception:
            pass


def setup_logging():
    """Cấu hình logger 'live' 1 lần: file xoay vòng theo ngày (giữ 7 ngày) + console + RAM buffer."""
    if getattr(setup_logging, "_done", False):
        return
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log.setLevel(logging.INFO)
    log.propagate = False
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

    fh = logging.handlers.TimedRotatingFileHandler(
        LOG_FILE, when="midnight", backupCount=7, encoding="utf-8"
    )
    fh.setFormatter(fmt)
    ch = logging.StreamHandler()
    ch.setFormatter(fmt)
    bh = _BufferHandler()
    bh.setFormatter(fmt)
    for h in (fh, ch, bh):
        log.addHandler(h)
    setup_logging._done = True


# Tự đổi asset theo thời gian. kind -> (OBS source, "image"|"video").
# Chỉ bg/banner/tvc (đúng scene_assets); "product image" do trigger SP điều khiển nên KHÔNG xoay ở đây.
_ROTATE_KINDS = {"background": (SRC_BG, "image"),
                 "banner": (SRC_BANNER, "image"),
                 "table": (SRC_TABLE, "image"),
                 "tvc": (SRC_TVC, "video")}
_ROTATE_MIN, _ROTATE_MAX = 10, 3600   # giây


class AssetRotator:
    """Đổi background/banner/tvc theo thời gian (timer độc lập mỗi loại) khi đang stream.
    Cấu hình lưu trong KV 'scene_random': <kind>_rotate_enabled (bool) + <kind>_rotate_interval (giây).
    Thread-safe: OBS đã serialize request bằng RLock nên gọi set_image/set_video từ Timer là an toàn."""

    def __init__(self, controller):
        self.c = controller
        self._lock = threading.Lock()
        self._timers = {}        # kind -> threading.Timer
        self._next_fire = {}     # kind -> epoch (giây) lần đổi kế tiếp
        self._last = {}          # kind -> asset_id vừa chọn (tránh lặp ngay)
        self.running = False

    @staticmethod
    def _clamp(v):
        try:
            v = int(v)
        except (TypeError, ValueError):
            v = 300
        return max(_ROTATE_MIN, min(_ROTATE_MAX, v))

    def _cfg(self):
        return db.get_setting("scene_random", {}) or {}

    def start(self):
        self.stop()
        self.running = True
        cfg = self._cfg()
        for kind in _ROTATE_KINDS:
            if cfg.get(f"{kind}_rotate_enabled"):
                self._schedule(kind, self._clamp(cfg.get(f"{kind}_rotate_interval", 300)))

    def stop(self):
        self.running = False
        with self._lock:
            for t in self._timers.values():
                t.cancel()
            self._timers.clear()
            self._next_fire.clear()

    def reload(self):
        """Gọi khi user lưu cấu hình mới — chỉ tái lập timer nếu đang chạy (đang stream)."""
        if self.running:
            self.start()

    def _schedule(self, kind, interval):
        with self._lock:
            if not self.running:
                return
            old = self._timers.get(kind)
            if old:
                old.cancel()
            t = threading.Timer(interval, self._fire, args=(kind, interval))
            t.daemon = True
            self._next_fire[kind] = time.time() + interval
            self._timers[kind] = t
            t.start()

    def _fire(self, kind, interval):
        if not self.running:
            return
        try:
            self._rotate(kind)
        except Exception as e:
            log.warning(f"[AutoRotate] {kind} lỗi: {e}")
        if self.running:
            self._schedule(kind, interval)   # lặp lại theo đúng interval

    def _rotate(self, kind):
        src, mode = _ROTATE_KINDS[kind]
        items = [a for a in db.list_scene_assets(kind) if a.enabled]
        if not items:
            return
        pool = [a for a in items if a.id != self._last.get(kind)] or items   # tránh lặp asset vừa rồi
        a = random.choice(pool)
        self._last[kind] = a.id
        if mode == "video":
            self.c.obs.set_video(src, a.file_path)
        else:
            self.c.obs.set_image(src, a.file_path)
        log.info(f"[AutoRotate] {kind} → {a.name or a.file_path}")

    def status(self):
        cfg = self._cfg()
        now = time.time()
        out = {}
        for kind in _ROTATE_KINDS:
            en = bool(cfg.get(f"{kind}_rotate_enabled"))
            d = {"enabled": en}
            if en:
                d["interval"] = self._clamp(cfg.get(f"{kind}_rotate_interval", 300))
                nf = self._next_fire.get(kind)
                d["next_in"] = max(0, int(nf - now)) if (self.running and nf) else None
            out[kind] = d
        return out


class LiveController:
    def __init__(self, host="localhost", port=4455, password="obs123456"):
        setup_logging()
        db.init_db()
        self.obs = OBSController(host=host, port=port, password=password)
        self.rotator = AssetRotator(self)   # tự đổi bg/banner/tvc theo thời gian khi đang stream
        self.current_video = None      # tên file đang phát
        self.current_video_id = None
        self.current_product = None     # tên sản phẩm đang hiển thị
        self.current_product_obj = None  # full record để dashboard hiển thị giá/SKU/...
        self.last_matched_product = None  # SP khớp gần nhất từ comment (context cho intent-only)
        self.on_product_change = None   # callback(product) khi SP đang phát đổi -> ghim Shopee (server gắn)
        self._last_pin_pid = None       # id SP đã ghim gần nhất (chỉ ghim khi đổi SP)
        self._skip = threading.Event()  # cho nút "Next Video" ngắt sleep
        self._waiting = threading.Event()  # đang chờ video hiện tại phát xong (để lọc event media-end)
        self._paused = threading.Event()  # cho nút Tạm dừng
        self._empty_logged = False      # tránh spam log khi playlist rỗng
        self._started = False
        # Playlist đang phát: None = playlist mặc định. Scheduler đặt theo phiên đang live.
        self.active_pl_id = None
        # Rule engine (kịch bản tự động) — trạng thái theo phiên live.
        self._session_plays = {}   # video_id -> số lần phát trong phiên (cho play_limit)
        self._capped = set()       # video_id đã hết giới hạn phát/phiên -> bỏ qua
        self._since_follow = 0     # đếm video kể từ lần chèn video follow gần nhất
        self._last_voucher = time.time()
        self._last_top = time.time()
        # Chuyển video khi OBS báo media phát xong (chính xác hơn đếm thời lượng); fallback = timeout.
        self.obs.enable_events(self._on_media_end)
        self._avatar_active = SRC_VIDEO   # avatar của scene đang chiếu
        self._scene_active = SCENE_MAIN   # scene đang chiếu (A/B) cho Fade chống nháy
        self._applied = {"background": None, "banner": None, "tvc": None, "table": None}  # asset đang lên OBS
        # Hàng đợi ưu tiên: video phát ngay/chèn trước playlist (vd video trả lời comment).
        self._priority = deque()
        self._qlock = threading.Lock()

    # ---------------------------------------------------------------- sync

    @staticmethod
    def _fmt_price(product):
        val = product.sale_price or product.price or 0
        try:
            return f"{float(val):,.0f}đ"
        except Exception:
            return str(val)

    def _bring_up(self, file_path, product):
        """GỐI ĐẦU: hiện clip mới (đè lên clip cũ ĐANG phát) -> cú decode bị clip cũ che -> không nháy/đen;
        rồi ẩn clip cũ. 2 source Avatar Video / Avatar Video 2 đè khít nhau trong scene chính."""
        if self._scene_active != SCENE_MAIN:
            self.obs.switch_scene(SCENE_MAIN)
            self._scene_active = SCENE_MAIN
        other = SRC_VIDEO2 if self._avatar_active == SRC_VIDEO else SRC_VIDEO
        old = self._avatar_active
        self.obs.set_video(other, file_path, restart=False)        # nạp file vào source đang ẩn
        if product and getattr(product, "image_path", None):
            self.obs.set_image(SRC_IMAGE, product.image_path)
        else:
            self.obs.set_image(SRC_IMAGE, "")
        self.obs.set_source_visible(SCENE_MAIN, other, True)       # HIỆN -> tự phát từ giây 0 (restart_on_activate)
        self._avatar_active = other
        if old != other:
            time.sleep(0.15)                                       # gối đầu CỰC NGẮN 150ms (đủ nuốt cú nháy,
            self.obs.set_source_visible(SCENE_MAIN, old, False)    # mắt không kịp thấy 2 khẩu hình đè nhau)
        # Video nói SP nào -> ghim SP đó trên Shopee (chạy thread, chỉ khi SP đổi, không chặn playback).
        pid = getattr(product, "id", None)
        if product and pid != self._last_pin_pid and self.on_product_change:
            self._last_pin_pid = pid
            threading.Thread(target=self._safe_pin, args=(product,), daemon=True).start()

    def _safe_pin(self, product):
        try:
            self.on_product_change(product)
        except Exception as e:
            log.warning(f"Ghim SP lỗi: {e}")

    def _wait_until_near_end(self, src, duration):
        """Chờ tới khi clip CÒN ~1s (để gối đầu clip kế). Trả ngay nếu Next tay / media kết thúc."""
        self._waiting.set()
        self._skip.clear()
        dur_ms = (float(duration) if duration else 0) * 1000
        while True:
            if self._skip.is_set():
                self._skip.clear()
                break
            if self._paused.is_set():
                time.sleep(0.2)
                continue
            state, cursor = self.obs.media_status(src)
            if state in (None, "OBS_MEDIA_STATE_ENDED", "OBS_MEDIA_STATE_STOPPED"):
                break
            if dur_ms and cursor is not None and cursor >= dur_ms - 250:   # gối đầu sát cuối (~250ms)
                break
            time.sleep(0.1)
        self._waiting.clear()

    def sync_to_obs(self, video, product):
        # Gối đầu clip + ẢNH SP theo video (gắn với SP của video đang phát).
        self._bring_up(video.file_path, product)
        self.current_video = Path(video.file_path).name
        self.current_video_id = video.id
        self.current_product = product.name if product else None
        self.current_product_obj = product
        log.info(f"Sync OBS: {product.name if product else '(no product)'}")

    # ---------------------------------------------------------------- 24/7 loop

    def apply_current_assets(self):
        """Đẩy NGAY asset đang bật (first-enabled mỗi loại) lên OBS — KHÔNG random.
        Dùng cho auto-apply khi upload/bật-tắt asset (để 'bật là thấy', khỏi cần bấm Apply)."""
        def first(kind):
            items = [a for a in db.list_scene_assets(kind) if a.enabled]
            a = items[0] if items else None
            return {"id": a.id, "name": a.name, "file_path": a.file_path} if a else None
        pick = {"background": first("background"), "banner": first("banner"),
                "tvc": first("tvc"), "table": first("table"), "seed": None}
        if not any(pick[k] for k in ("background", "banner", "tvc", "table")):
            return None
        return self.apply_scene_random(pick)

    def apply_one_asset(self, asset_id):
        """Áp đúng 1 asset (do người dùng click chọn) lên source tương ứng."""
        a = db.get_scene_asset(int(asset_id))
        if not a:
            return None
        pick = {"background": None, "banner": None, "tvc": None, "table": None, "seed": None}
        if a.kind not in pick:
            return None
        pick[a.kind] = {"id": a.id, "name": a.name, "file_path": a.file_path}
        return self.apply_scene_random(pick)

    def _on_media_end(self, input_name):
        """OBS báo 1 media phát xong. Chỉ chuyển khi đúng video avatar + đang chờ + không tạm dừng
        (tránh ngắt nhầm video vừa mới set)."""
        if input_name and input_name != self._avatar_active:
            return
        if self._paused.is_set() or not self._waiting.is_set():
            return
        self._skip.set()

    def _wait_video(self, duration):
        """Chờ hết video: ưu tiên sự kiện media-end của OBS, lấy thời lượng làm chốt an toàn.
        Nếu không có kênh sự kiện -> chờ theo đúng thời lượng (hành vi cũ)."""
        dur = float(duration or 0)
        if self.obs.events_active():
            timeout = dur + 30 if dur > 0 else 1800
        else:
            timeout = max(1.0, dur or 1)
        self._waiting.set()
        self._skip.wait(timeout=timeout)
        self._waiting.clear()
        self._skip.clear()

    # ---------------------------------------------------------------- rule engine (kịch bản tự động)

    def _rules(self):
        return db.get_setting("rules", {}) or {}

    def _reset_session_rules(self):
        self._session_plays = {}
        self._capped = set()
        self._since_follow = 0
        self._last_voucher = time.time()
        self._last_top = time.time()
        log.info("Rule engine: bắt đầu phiên mới (đếm lại play_limit/follow/voucher/top)")

    def _enqueue_category(self, category):
        """Chèn 1 video theo category (vd 'follow'/'voucher') vào cuối hàng đợi — round-robin theo play_count."""
        vids = db.get_videos_by_category(category)
        if not vids:
            log.warning(f"Rule: không có video category='{category}' để chèn")
            return False
        v = min(vids, key=lambda x: x.play_count)
        with self._qlock:
            self._priority.append(("lib", int(v.id)))
        log.info(f"Rule: chèn video '{category}' → {v.name or v.file_path}")
        return True

    def _apply_time_rules(self, r):
        """Chèn voucher / phát lại top theo mốc thời gian (giây)."""
        now = time.time()
        vm = int(r.get("voucher_every_min", 0) or 0)
        if vm > 0 and (now - self._last_voucher) >= vm * 60:
            if self._enqueue_category(r.get("voucher_category", "voucher")):
                self._last_voucher = now
        tm = int(r.get("top_every_min", 0) or 0)
        if tm > 0 and (now - self._last_top) >= tm * 60:
            vids = db.get_top_commission_videos(int(r.get("top_count", 3) or 3))
            if vids:
                with self._qlock:
                    for v in vids:
                        self._priority.append(("lib", int(v.id)))
                log.info(f"Rule: phát lại top {len(vids)} sản phẩm (hoa hồng cao)")
                self._last_top = now

    def _after_playlist_video(self, video, r):
        """Cập nhật đếm sau khi 1 video playlist phát xong: play_limit + chèn follow theo đếm."""
        vid = int(video.id)
        self._session_plays[vid] = self._session_plays.get(vid, 0) + 1
        if r.get("play_limit_enabled") and video.play_limit and \
                self._session_plays[vid] >= int(video.play_limit):
            self._capped.add(vid)
            log.info(f"Rule: video #{vid} đạt giới hạn {video.play_limit} lần/phiên → tạm ngừng")
        fn = int(r.get("follow_every_n", 0) or 0)
        if fn > 0:
            self._since_follow += 1
            if self._since_follow >= fn:
                if self._enqueue_category(r.get("follow_category", "follow")):
                    self._since_follow = 0

    def run_forever(self):
        self._started = True
        log.info("LiveController bắt đầu vòng lặp 24/7")
        while True:
            try:
                if not self.obs.is_connected():
                    log.warning("OBS mất kết nối, reconnect...")
                    self.obs.reconnect()
                    time.sleep(5)
                    continue

                # Tạm dừng: giữ video hiện tại (OBS đã pause media), không sang video kế.
                if self._paused.is_set():
                    time.sleep(0.3)
                    continue

                # Rule engine: chèn voucher / phát lại top theo mốc thời gian (vào hàng đợi ưu tiên).
                rules = self._rules()
                self._apply_time_rules(rules)

                # Hàng đợi ưu tiên (phát ngay/video trả lời/rule chèn) — TRƯỚC playlist, KHÔNG mark playlist.
                item = self._pop_priority()
                if item is not None:
                    v, product, _ = self._resolve_item(item)
                    if v:
                        self._bring_up(v.file_path, product)
                        log.info(f"[Hàng đợi] phát: {v.file_path}")
                        self._wait_until_near_end(self._avatar_active, v.duration)
                        if item[0] == "ans":
                            db.mark_answer_played(item[1])
                    continue

                pl_id = self.active_pl_id
                pid = pl_id or db.get_default_playlist_id()
                meta = db.get_playlist_meta(pid)
                autoplay = bool(meta.autoplay) if meta else True
                loop_on = bool(meta.loop) if meta else True

                exclude = list(self._capped) if rules.get("play_limit_enabled") else None
                video = db.get_next_unplayed_video(pl_id, exclude)
                if not video:
                    if not self._empty_logged:
                        log.info("Hết playlist / playlist rỗng" + (" — loop lại" if loop_on else " — loop TẮT, dừng phát"))
                        self._empty_logged = True
                    if loop_on:
                        db.reset_all_played(pl_id)
                        # Vẫn rỗng (rỗng thật / mọi video đã đạt giới hạn) -> nghỉ để khỏi quay vòng tốn CPU.
                        if not db.get_next_unplayed_video(pl_id, exclude):
                            time.sleep(5)
                    else:
                        time.sleep(2)   # loop tắt: giữ nguyên, không reset
                    continue
                self._empty_logged = False

                product = db.get_product(video.product_id) if video.product_id else None
                self._bring_up(video.file_path, product)
                log.info(f"Đang phát: {video.file_path}")
                log.info(f"Sản phẩm: {product.name if product else '(no product)'}")

                # chờ tới khi clip còn ~1s -> vòng sau gối đầu clip kế (không nháy)
                self._wait_until_near_end(self._avatar_active, video.duration)

                db.mark_played(video.id, pl_id)
                self._after_playlist_video(video, rules)

                # Tự động phát TẮT: giữ video hiện tại, chờ Next thủ công (hoặc bật lại autoplay).
                while not autoplay and not self._skip.is_set():
                    time.sleep(0.3)
                    m = db.get_playlist_meta(pid)
                    autoplay = bool(m.autoplay) if m else True
                if self._skip.is_set():
                    self._skip.clear()
            except Exception as e:
                log.error(f"Lỗi vòng lặp: {e}")
                time.sleep(5)
                continue

    # ---------------------------------------------------------------- UI controls

    def start_stream(self):
        self._reset_session_rules()   # mỗi phiên live: đếm lại play_limit / follow / voucher / top
        try:                          # mỗi phiên: random lại nền/banner/tvc/layout nếu bật
            r = db.get_setting("scene_random", {}) or {}
            if any(r.get(k, True) for k in ("random_bg", "random_banner", "random_tvc", "random_table")):
                self.apply_scene_random()
            else:
                self.apply_current_assets()
        except Exception as e:
            log.error(f"apply scene random lỗi: {e}")
        self.rotator.start()              # bật tự-đổi asset theo thời gian (nếu có loại bật)
        return self.obs.start_stream()

    # ---------------------------------------------------------------- scene assets / random layout

    def compute_scene_random(self):
        """Chọn ngẫu nhiên nền/banner/tvc (trong số đang bật) + jitter vị trí/size theo settings.
        Trả dict pick (None nếu không có gì). Dùng seed cố định nếu settings đặt 'fix'."""
        r = db.get_setting("scene_random", {}) or {}
        seed = r.get("seed_value") if r.get("seed_mode") == "fix" else None
        rng = random.Random(seed) if seed not in (None, "") else random.Random()

        def pick(kind, do_random):
            items = [a for a in db.list_scene_assets(kind) if a.enabled]
            if not items:
                return None
            a = rng.choice(items) if do_random else items[0]
            return {"id": a.id, "name": a.name, "file_path": a.file_path}

        # CHỈ random nội dung file — KHÔNG đụng vị trí/kích thước (giữ cố định).
        return {
            "background": pick("background", r.get("random_bg", True)),
            "banner": pick("banner", r.get("random_banner", True)),
            "tvc": pick("tvc", r.get("random_tvc", True)),
            "table": pick("table", r.get("random_table", True)),
            "seed": seed,
        }

    def apply_scene_random(self, pick=None):
        """CHỈ đổi file nền/banner/tvc (giữ nguyên vị trí/kích thước). pick=None -> tự tính.
        Tự tạo source nếu thiếu; log chi tiết; trả pick kèm 'results'."""
        if pick is None:
            pick = self.compute_scene_random()
        if not pick:
            log.warning("[SceneRandom] không có pick để áp dụng")
            return None
        bg, banner, tvc, table = pick.get("background"), pick.get("banner"), pick.get("tvc"), pick.get("table")
        if not (bg or banner or tvc or table):
            log.warning("[SceneRandom] KHÔNG có asset nào đang BẬT (enabled) — không có gì để áp dụng")
            pick["results"] = {}
            pick["message"] = "Chưa có asset nào đang Bật. Hãy upload + bấm Bật cho nền/banner/TVC/bàn SP."
            return pick

        # Tự tạo source nếu OBS chưa có (tránh lỗi 'No source found' rồi im lặng).
        existing = {n for n, _ in self.obs.list_sources()}
        missing = {SRC_BG, SRC_BANNER, SRC_TVC} - existing
        if missing:
            log.info(f"[SceneRandom] thiếu source {missing} trong OBS -> tự tạo (ensure_live_sources)")
            self.obs.ensure_live_sources()

        results = {}
        # Background + Banner + Bàn SP: ảnh. (Product Image KHÔNG ở đây — nó theo video.)
        for key, src, asset in (("background", SRC_BG, bg), ("banner", SRC_BANNER, banner),
                                ("table", SRC_TABLE, table)):
            if not asset:
                continue
            path = str(Path(asset["file_path"]).resolve())   # bắt buộc tuyệt đối cho OBS
            exists = Path(path).exists()
            ok = self.obs.set_image(src, path) if exists else False
            log.info(f"[SceneRandom] set {src} = {path} | exists={exists} | ok={ok}")
            results[key] = bool(ok)

        # TVC: có thể là ẢNH hoặc VIDEO -> set đúng source + ẩn cái kia (Media Source không hiện ảnh).
        if tvc:
            path = str(Path(tvc["file_path"]).resolve())
            exists = Path(path).exists()
            is_img = path.lower().endswith(_IMAGE_EXTS)
            if is_img:
                ok = self.obs.set_image(SRC_TVC_IMG, path) if exists else False
                self.obs.set_source_visible(SCENE_MAIN, SRC_TVC_IMG, True)
                self.obs.set_source_visible(SCENE_MAIN, SRC_TVC, False)
            else:
                ok = self.obs.set_video(SRC_TVC, path) if exists else False
                self.obs.set_source_visible(SCENE_MAIN, SRC_TVC, True)
                self.obs.set_source_visible(SCENE_MAIN, SRC_TVC_IMG, False)
            log.info(f"[SceneRandom] set TVC ({'ảnh' if is_img else 'video'}) = {path} | exists={exists} | ok={ok}")
            results["tvc"] = bool(ok)

        self.obs.reorder_sources(SCENE_MAIN)   # giữ z-order (Background ở đáy) — KHÔNG đổi vị trí/size

        for k in ("background", "banner", "tvc"):   # ghi nhận asset đang lên OBS
            if pick.get(k):
                self._applied[k] = pick[k].get("id")
        pick["results"] = results
        ok_all = all(results.values()) if results else False
        pick["message"] = ("Đã áp dụng lên OBS" if ok_all
                           else "Một số source set lỗi — kiểm tra OBS (xem log).")
        log.info(f"[SceneRandom] xong (chỉ đổi file): results={results}")
        return pick

    def stop_stream(self):
        self.rotator.stop()               # dừng mọi timer tự-đổi asset
        return self.obs.stop_stream()

    def next_video(self):
        """Bỏ qua video hiện tại: đánh dấu đã phát + ngắt sleep để loop sang video kế."""
        log.info("Next Video (thủ công)")
        self._skip.set()
        return True

    def previous_video(self):
        """Phát lại video trước đó: đặt lại entry đã phát gần nhất về chưa phát rồi nhảy."""
        log.info("Previous Video (thủ công)")
        db.reset_last_played(self.active_pl_id)
        self._skip.set()
        return True

    def set_active_playlist(self, pl_id):
        """Đổi playlist đang phát (scheduler gọi khi phiên start/stop). None = playlist mặc định.
        Ngắt video hiện tại để chuyển ngay sang playlist mới."""
        pl_id = int(pl_id) if pl_id else None
        if pl_id == self.active_pl_id:
            return True
        self.active_pl_id = pl_id
        self._empty_logged = False
        log.info(f"Đổi playlist đang phát → {pl_id if pl_id else 'mặc định'}")
        self._skip.set()
        return True

    # ---------------------------------------------------------------- hàng đợi ưu tiên
    # Hàng đợi chứa tuple (kind, id): kind='lib' (video thư viện) hoặc 'ans' (video trả lời).

    def _resolve_item(self, item):
        """(kind,id) -> (video-like ns, product, file_path) để sync_to_obs."""
        kind, oid = item
        if kind == "ans":
            av = db.get_answer_video(oid)
            if not av:
                return None, None, None
            product = db.get_product(av.product_id) if av.product_id else None
            v = SimpleNamespace(id=None, file_path=av.file_path, duration=av.duration, name=av.name)
            return v, product, av.file_path
        v = db.get_video(oid)
        if not v:
            return None, None, None
        product = db.get_product(v.product_id) if v.product_id else None
        return v, product, v.file_path

    def _pop_priority(self):
        with self._qlock:
            return self._priority.popleft() if self._priority else None

    def _last_basename(self):
        """Tên file của item cuối hàng đợi (hoặc video đang phát) — để chặn trùng liên tiếp."""
        if self._priority:
            _, _, fp = self._resolve_item(self._priority[-1])
            return Path(fp).name if fp else None
        return self.current_video  # đã là basename

    def play_now(self, video_id):
        """Phát video THƯ VIỆN này NGAY: chèn đầu hàng đợi + ngắt video hiện tại."""
        with self._qlock:
            self._priority.appendleft(("lib", int(video_id)))
        log.info(f"Phát ngay video #{video_id}")
        self._skip.set()
        return True

    def enqueue(self, video_id):
        """Đưa video THƯ VIỆN vào cuối hàng đợi. Chặn 2 video giống nhau liên tiếp → False."""
        v = db.get_video(int(video_id))
        if not v:
            return False
        with self._qlock:
            if self._last_basename() == Path(v.file_path).name:
                log.warning(f"Bỏ qua: video #{video_id} trùng video liền trước trong hàng đợi")
                return False
            self._priority.append(("lib", int(video_id)))
        log.info(f"Thêm video #{video_id} vào hàng đợi phát")
        return True

    def enqueue_answer(self, answer_video_id, mode="enqueue"):
        """Đưa VIDEO TRẢ LỜI vào hàng đợi (mode='play_now' chèn đầu + ngắt, 'enqueue' nối đuôi).
        Chặn trùng liên tiếp. Trả True/False."""
        av = db.get_answer_video(int(answer_video_id))
        if not av:
            return False
        with self._qlock:
            if mode != "play_now" and self._last_basename() == Path(av.file_path).name:
                log.warning(f"Bỏ qua answer #{answer_video_id}: trùng video liền trước")
                return False
            if mode == "play_now":
                self._priority.appendleft(("ans", int(answer_video_id)))
            else:
                self._priority.append(("ans", int(answer_video_id)))
        if mode == "play_now":
            self._skip.set()
        log.info(f"[Trả lời] {mode} answer #{answer_video_id} ({Path(av.file_path).name})")
        return True

    def clear_queue(self):
        with self._qlock:
            self._priority.clear()
        return True

    def queue_list(self):
        with self._qlock:
            return list(self._priority)

    def pause(self):
        log.info("Tạm dừng")
        self._paused.set()
        return self.obs.media_action(self._avatar_active, "pause")

    def resume(self):
        log.info("Tiếp tục")
        self._paused.clear()
        return self.obs.media_action(self._avatar_active, "play")

    def is_paused(self):
        return self._paused.is_set()

    def start_recording(self):
        return self.obs.start_recording()

    def stop_recording(self):
        return self.obs.stop_recording()

    def set_scene(self, name):
        return self.obs.switch_scene(name)

    def set_source_visible(self, source, visible, scene=SCENE_MAIN):
        return self.obs.set_source_visible(scene, source, visible)

    def reload_media(self):
        return self.obs.media_action(self._avatar_active, "restart")

    def set_volume(self, source, mul):
        return self.obs.set_volume(source, mul)

    def toggle_mute(self, source):
        """Bật/tắt tiếng 1 source (mic, nhạc nền…)."""
        return self.obs.toggle_mute(source)

    # ---------------------------------------------------------------- status

    def get_status(self, obs=None):
        """Trạng thái đầy đủ cho dashboard (gọi định kỳ qua WebSocket).
        obs: kết nối OBS dùng cho các lệnh ĐỌC (giám sát) — truyền kết nối riêng để không
        chiếm lock của kết nối điều khiển/phát (giảm trễ khi bấm Next/đổi scene)."""
        obs = obs or self.obs
        p = self.current_product_obj
        product = None
        if p:
            product = {
                "id": p.id, "name": p.name, "link": getattr(p, "link", None),
                "image_path": p.image_path,
            }
        stream = obs.get_stream_status_detail()
        queue = []
        for item in self.queue_list():
            kind, oid = item
            if kind == "ans":
                av = db.get_answer_video(oid)
                if av:
                    it = db.get_intent(av.intent_id) if av.intent_id else None
                    queue.append({"kind": "ans", "name": av.name or Path(av.file_path).name,
                                  "intent": it.name if it else ""})
            else:
                v = db.get_video(oid)
                if v:
                    queue.append({"kind": "lib", "name": v.name or Path(v.file_path).name, "intent": ""})
        return {
            "queue": queue,
            "active_pl_id": self.active_pl_id,
            "obs_connected": obs.is_connected(),
            "streaming": bool(stream.get("active")),
            "paused": self.is_paused(),
            "current_video": self.current_video,
            "current_video_id": self.current_video_id,
            "current_product": self.current_product,
            "product": product,
            "scene": self._safe(lambda: obs.client.get_current_program_scene().current_program_scene_name),
            "stats": obs.get_stats(),
            "stream": stream,
            "record": obs.get_record_status(),
            "logs": list(_LOG_BUFFER)[-30:],
        }

    @staticmethod
    def _safe(fn, default=None):
        try:
            return fn()
        except Exception:
            return default

    def recent_logs(self, n=20):
        return list(_LOG_BUFFER)[-n:]


if __name__ == "__main__":
    # Test thủ công: sync 1 video lên OBS (cần có sản phẩm + video trong live.db).
    import sys
    setup_logging()
    db.init_db()
    c = LiveController()
    print("OBS connected:", c.obs.is_connected())
    vids = db.get_all_videos()
    if not vids:
        print("Chưa có video trong live.db — thêm bằng test bên dưới.")
        sys.exit(0)
    v = db.get_next_unplayed_video() or vids[0]
    p = db.get_product(v.product_id) if v.product_id else None
    c.sync_to_obs(v, p)
    print(f"Đã sync video {v.file_path} / product {p.name if p else None} lên OBS.")
