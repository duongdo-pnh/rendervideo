"""Bộ hẹn giờ phiên live: tự set RTMP + scene + start/stop stream theo lịch.

State machine: scheduled → live → ended (+ canceled/error/skipped). Stop thủ công ⇒ ended
(scheduler KHÔNG tự start lại). 1 OBS = 1 phiên live tại một thời điểm. Mọi nhánh bọc try/except
để vòng lặp 24/7 không sập. Dùng chung obs_controller + live_database với LiveController.
"""
import logging
import time
from datetime import datetime

import live_database as db

log = logging.getLogger("live")
MAX_RECOVER = 5


class LiveScheduler:
    def __init__(self, controller):
        self.c = controller
        self.obs = controller.obs
        self._recover = {}     # session_id -> số lần thử start lại
        self._manual_stopped = set()  # phiên bị dừng tay (không auto-recover)
        self.reconcile()

    def reconcile(self):
        """Khi khởi động: hòa giải phiên còn 'live' trong DB (do tiến trình trước tắt đột ngột).
        Quá giờ → ended; còn trong giờ → scheduled (tick sẽ start lại + lập lại stream)."""
        now = self._now()
        for s in db.get_sessions_by_status("live"):
            if s.end_at and s.end_at <= now:
                db.set_session_status(s.id, "ended", ended_at=now, error="đóng khi khởi động lại (đã quá giờ)")
                log.info(f"Reconcile: phiên #{s.id} '{s.name}' quá giờ → ended")
            else:
                db.set_session_status(s.id, "scheduled")
                log.info(f"Reconcile: phiên #{s.id} '{s.name}' còn trong giờ → scheduled (sẽ start lại)")

    @staticmethod
    def _now():
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # ---------------------------------------------------------------- core

    def _start_session(self, s):
        if getattr(s, "profile", None):
            self.obs.set_profile(s.profile)   # đổi OBS profile (vd "Shopee Live 1080p") trước khi set RTMP
        self.obs.set_stream_service(s.rtmp_server, s.stream_key)
        if s.scene:
            self.obs.switch_scene(s.scene)
        # Playlist riêng của phiên (None = playlist mặc định).
        self.c.set_active_playlist(getattr(s, "pl_id", None))
        if self.c.start_stream():
            db.set_session_status(s.id, "live", started_at=self._now())
            self._recover[s.id] = 0
            self._manual_stopped.discard(s.id)
            log.info(f"Phiên #{s.id} '{s.name}' BẮT ĐẦU live ({s.platform})")
            return True
        # KHÔNG đánh 'error' vĩnh viễn: giữ 'scheduled' để tick sau thử lại trong khung giờ
        # (OBS có thể chỉ trục trặc tạm thời). Hết giờ mà vẫn chưa lên được → đánh 'missed' ở tick.
        db.set_session_status(s.id, "scheduled",
                              error="start_stream thất bại — sẽ thử lại (kiểm tra RTMP/đích live)")
        log.error(f"Phiên #{s.id} '{s.name}' start lỗi — giữ scheduled để thử lại")
        return False

    def _stop_session(self, s, status="ended", manual=False):
        self.c.stop_stream()
        self.c.set_active_playlist(None)   # về playlist mặc định khi hết phiên
        db.set_session_status(s.id, status, ended_at=self._now())
        self._recover.pop(s.id, None)
        if manual:
            self._manual_stopped.add(s.id)
        log.info(f"Phiên #{s.id} '{s.name}' → {status}")

    def tick(self):
        now = self._now()
        for s in db.get_due_to_stop(now):
            self._stop_session(s, "ended")

        # phiên scheduled đã quá giờ mà chưa lên được → missed (khỏi nằm "Chờ" mãi)
        for s in db.get_expired_scheduled(now):
            db.set_session_status(s.id, "missed", error="quá giờ mà chưa start được")
            log.warning(f"Phiên #{s.id} '{s.name}' bỏ lỡ (quá giờ chưa lên live)")

        active = db.get_active_session()
        for s in db.get_due_to_start(now):
            if active:
                db.set_session_status(s.id, "skipped", error="trùng giờ với phiên đang live")
                log.warning(f"Phiên #{s.id} bị bỏ qua (đã có phiên live #{active.id})")
                continue
            if self._start_session(s):
                active = db.get_session(s.id)

        # auto-recover: phiên live trong khung giờ mà stream rớt -> start lại (giới hạn)
        active = db.get_active_session()
        if active and active.auto_recover and active.end_at and active.end_at > now \
                and active.id not in self._manual_stopped:
            if not self.obs.is_streaming():
                n = self._recover.get(active.id, 0)
                if n < MAX_RECOVER:
                    self._recover[active.id] = n + 1
                    log.warning(f"Phiên #{active.id} stream rớt — thử start lại ({n + 1}/{MAX_RECOVER})")
                    self.obs.set_stream_service(active.rtmp_server, active.stream_key)
                    self.c.start_stream()
            else:
                self._recover[active.id] = 0

    def run_forever(self, interval=15):
        log.info("LiveScheduler bắt đầu (hẹn giờ phiên live)")
        while True:
            try:
                self.tick()
            except Exception as e:
                log.error(f"Scheduler tick lỗi: {e}")
            time.sleep(interval)

    # ---------------------------------------------------------------- manual

    def start_session(self, sid):
        s = db.get_session(sid)
        if not s:
            return False
        if db.get_active_session():
            log.warning("Đã có phiên đang live — dừng phiên đó trước khi start phiên mới")
            return False
        return self._start_session(s)

    def stop_session(self, sid):
        s = db.get_session(sid)
        if not s:
            return False
        self._stop_session(s, "ended", manual=True)
        return True

    def cancel_session(self, sid):
        db.set_session_status(sid, "canceled")
        return True
