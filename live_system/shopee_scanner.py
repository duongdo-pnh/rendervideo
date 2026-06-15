"""Quét comment livestream Shopee Creator — tự động dò phiên live + poll comment.

Dùng cookie + User-Agent đã được extension EcomTools đẩy vào hệ thống (bảng shopee_cookies).
Tham chiếu API: GET creator.shopee.{tld}/supply/api/lm/sellercenter/realtime/...
  - sessionList                                  -> danh sách phiên (auto lấy phiên đang live)
  - dashboard/livestream/comments?sessionId=&startTimestamp=  -> comment realtime

Một CommentScanner (singleton) chạy 1 thread poll. Frontend lấy comment qua
GET /api/shopee/scan/comments?since=<seq> (long-poll đơn giản theo seq tăng dần).
"""
import hashlib
import logging
import threading
import time
from collections import deque

import httpx

import live_database as db

log = logging.getLogger("live")
SESSION_RECHECK_SEC = 20   # dò lại phiên live mới mỗi 20s (mở lại live -> tự chuyển)

# Mã quốc gia -> creator domain (theo tài liệu API Shopee).
_CREATOR_DOMAIN = {
    "VN": "creator.shopee.vn",
    "PH": "creator.shopee.ph",
    "MY": "creator.shopee.com.my",
    "TH": "creator.shopee.co.th",
    "ID": "creator.shopee.co.id",
    "SG": "creator.shopee.sg",
    "TW": "creator.shopee.tw",
}
_LANG = {"VN": "vi", "TH": "th", "ID": "id", "MY": "ms",
         "PH": "en", "TW": "zh-Hant", "SG": "en"}

POLL_INTERVAL_SEC = 2.0
_UA_FALLBACK = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36")


def _domain(code):
    return _CREATOR_DOMAIN.get((code or "").upper())


def _build_headers(cookie, ua, code):
    dom = _domain(code)
    return {
        "cookie": cookie,
        "accept": "application/json",
        "content-type": "application/json",
        "accept-language": _LANG.get((code or "").upper(), "en"),
        "language": _LANG.get((code or "").upper(), "en"),
        "referer": f"https://{dom}/",
        "user-agent": ua or _UA_FALLBACK,
        "x-requested-with": "XMLHttpRequest",
    }


def _extract_list(data, *keys):
    """Lấy list từ body chịu nhiều dạng: data.<key> hoặc data là list trực tiếp."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for k in keys:
            v = data.get(k)
            if isinstance(v, list):
                return v
        # đôi khi list nằm sâu 1 lớp: data.<key>.list
        for k in keys:
            v = data.get(k)
            if isinstance(v, dict) and isinstance(v.get("list"), list):
                return v["list"]
    return []


def _first(d, *keys):
    for k in keys:
        if isinstance(d, dict) and d.get(k) not in (None, ""):
            return d[k]
    return None


class ShopeeAuthError(RuntimeError):
    pass


class CommentScanner:
    def __init__(self):
        self._thread = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self.code = None
        self.session_id = None
        self.session_title = None
        self.running = False
        self.error = None
        self.last_poll = None
        self._comments = deque(maxlen=500)   # mỗi item: {seq,id,user,content,ts,match}
        self._seen = set()
        self._seen_order = deque(maxlen=3000)
        self._seq = 0
        self.on_comment = None       # hook(comment_dict) -> match dict | None (gắn từ server)
        self.on_live_session = None  # hook(session_id) khi khóa được phiên live MỚI -> auto sync+rematch
        self.manual = False          # True = khóa phiên THỦ CÔNG, _loop không tự đổi/reset (status chập chờn)
        self._reset_start_ts = False

    # ---- HTTP ----
    def _cookie_row(self, code):
        row = db.get_shopee_cookie(code)
        if not row or not row.get("cookie"):
            raise RuntimeError(f"Chưa có cookie cho quốc gia {code}. Mở extension trên trang Shopee đã đăng nhập.")
        return row

    def _get(self, code, path, params):
        dom = _domain(code)
        if not dom:
            raise RuntimeError(f"Quốc gia không hỗ trợ: {code}")
        row = self._cookie_row(code)
        url = f"https://{dom}/supply/api/lm/sellercenter/realtime/{path}"
        r = httpx.get(url, params=params,
                      headers=_build_headers(row["cookie"], row.get("user_agent"), code),
                      timeout=15.0, follow_redirects=True)
        if r.status_code in (401, 403):
            raise ShopeeAuthError("Cookie hết hạn / không đủ quyền (401/403). Lấy lại cookie qua extension.")
        if r.status_code == 429:
            raise RuntimeError("Bị giới hạn tần suất (429). Giảm tốc độ poll.")
        r.raise_for_status()
        try:
            return r.json()
        except Exception:
            raise RuntimeError("Phản hồi không phải JSON (có thể bị chặn / sai cookie).")

    def list_sessions(self, code):
        body = self._get(code, "sessionList",
                         {"page": 1, "pageSize": 10, "name": "", "orderBy": "", "sort": ""})
        data = body.get("data") if isinstance(body, dict) else None
        if data is None:
            raise ShopeeAuthError("Shopee trả data=null (cookie rỗng/hết hạn).")
        items = _extract_list(data, "list", "sessions", "sessionList", "records")
        out = []
        for it in items:
            if not isinstance(it, dict):
                continue
            sid = _first(it, "sessionId", "session_id", "id")
            if sid is None:
                continue
            out.append({
                "session_id": str(sid),
                "title": _first(it, "title", "name", "sessionTitle") or "",
                "status": _first(it, "status", "sessionStatus", "state"),
                "raw": it,
            })
        return out

    def _pick_live(self, sessions):
        """Chọn phiên ĐANG live (status gợi ý 'live/ongoing/1'). KHÔNG fallback phiên đã kết thúc
        (status=2) — trả None để báo rõ 'chưa có phiên live'."""
        def is_live(s):
            st = s.get("status")
            if st is None:
                return False
            t = str(st).lower()
            return t in ("1", "live", "ongoing", "living", "streaming", "started", "on")
        for s in sessions:
            if is_live(s):
                return s
        return None

    def get_comments(self, code, session_id, start_ts):
        body = self._get(code, "dashboard/livestream/comments",
                         {"sessionId": session_id, "startTimestamp": start_ts})
        data = body.get("data") if isinstance(body, dict) else None
        if data is None:
            raise ShopeeAuthError("Shopee trả data=null (cookie rỗng/hết hạn).")
        return _extract_list(data, "comments", "list", "records")

    def _key(self, c):
        cid = _first(c, "id", "msg_id", "msgId")
        if cid is not None:
            return f"id:{cid}"
        # Comment Shopee thực tế không có id → dedupe theo user+timestamp+content.
        who = _first(c, "userId", "user_id", "username", "userName")
        raw = f"{who}|{_first(c,'timestamp','ts')}|{_first(c,'content','text','msg')}"
        return "h:" + hashlib.md5(raw.encode("utf-8", "ignore")).hexdigest()

    def _ingest(self, raw_comments):
        added = 0
        for c in raw_comments:
            if not isinstance(c, dict):
                continue
            k = self._key(c)
            if k in self._seen:
                continue
            self._seen.add(k)
            self._seen_order.append(k)
            if len(self._seen_order) >= self._seen_order.maxlen:
                old = self._seen_order.popleft()
                self._seen.discard(old)
            item = {
                "seq": None,
                "id": _first(c, "id", "msg_id", "msgId"),
                "user": _first(c, "username", "userName", "nickname", "nickName", "userId") or "?",
                "content": _first(c, "content", "text", "msg", "comment") or "",
                "ts": _first(c, "timestamp", "ts", "createTime") or int(time.time()),
                "match": None,
            }
            # Khớp sản phẩm + (tùy chọn) trigger phát video. Không để lỗi hook làm chết poll.
            if self.on_comment:
                try:
                    item["match"] = self.on_comment(c)
                except Exception as e:
                    item["match"] = {"matched": False, "method": "error", "reason": str(e)}
            with self._lock:
                self._seq += 1
                item["seq"] = self._seq
                self._comments.append(item)
            added += 1
        return added

    # ---- poll loop ----
    def _loop(self, code):
        start_ts = int(time.time())
        last_check = 0.0
        fails = 0
        while not self._stop.is_set():
            now = time.time()
            if self._reset_start_ts:                      # vừa khóa phiên thủ công -> comment tính từ giờ
                start_ts = int(time.time()); self._reset_start_ts = False
            # Dò lại phiên đang live định kỳ -> tự chuyển khi mở lại live. BỎ QUA nếu đã khóa thủ công.
            if not self.manual and (not self.session_id or now - last_check > SESSION_RECHECK_SEC):
                last_check = now
                try:
                    live = self._pick_live(self.list_sessions(code))
                    if live and str(live["session_id"]) != str(self.session_id):
                        self.session_id = live["session_id"]
                        self.session_title = live["title"]
                        start_ts = int(time.time())     # comment realtime tính từ lúc chuyển phiên
                        self.error = None
                        log.info(f"[Scan] chuyển sang phiên live {self.session_id} — {self.session_title}")
                        # Khóa được phiên live -> tự đồng bộ SP + khớp lại video (chạy thread, không chặn poll).
                        if self.on_live_session:
                            sid = self.session_id
                            threading.Thread(target=self._safe_live_hook, args=(sid,), daemon=True).start()
                    elif not live:
                        self.session_id = None
                        self.session_title = None
                        self.error = "Chưa có phiên đang live — đang chờ..."
                except ShopeeAuthError as e:
                    self.error = str(e); self.running = False; return   # cookie chết -> dừng
                except Exception as e:
                    self.error = f"Lỗi dò phiên: {e}"
            if not self.session_id:
                self._stop.wait(5)
                continue
            try:
                cs = self.get_comments(code, self.session_id, start_ts)
                self._ingest(cs)
                self.last_poll = int(time.time())
                self.error = None
                fails = 0
            except ShopeeAuthError as e:
                self.error = str(e); self.running = False; return
            except Exception as e:
                fails += 1
                self.error = f"Lỗi poll ({fails}): {e}"
                if fails >= 5:
                    time.sleep(10)
            self._stop.wait(POLL_INTERVAL_SEC)
        self.running = False

    def _safe_live_hook(self, session_id):
        try:
            self.on_live_session(session_id)
        except Exception as e:
            log.warning(f"[Scan] auto sync/rematch lỗi: {e}")

    def ingest_external(self, comments):
        """Nhận comment THẬT do extension cào từ trang live đẩy về — xử lý y hệt poll
        (dedupe + khớp SP + trigger OBS + đẩy vào feed). Trả số comment mới."""
        if not isinstance(comments, list):
            return 0
        return self._ingest(comments)

    def inject_local(self, content, user="local-test"):
        """Bơm 1 comment GIẢ (test local): chạy hook (khớp + trigger OBS) + đẩy vào feed.
        Dùng để diễn tập trước khi live thật mà không cần phiên live Shopee."""
        c = {"id": None, "username": user, "content": content, "timestamp": int(time.time())}
        item = {"seq": None, "id": None, "user": user, "content": content,
                "ts": c["timestamp"], "match": None}
        if self.on_comment:
            try:
                item["match"] = self.on_comment(c)
            except Exception as e:
                item["match"] = {"matched": False, "method": "error", "reason": str(e)}
        with self._lock:
            self._seq += 1
            item["seq"] = self._seq
            self._comments.append(item)
        return item

    # ---- public API ----
    def start(self, code):
        code = (code or "").strip().upper()
        if self.running:
            self.stop()
        self._stop.clear()
        self.code = code
        self.session_id = None
        self.session_title = None
        self.manual = False          # start() = chế độ tự dò
        self.error = None
        self.running = True
        self._thread = threading.Thread(target=self._loop, args=(code,), daemon=True)
        self._thread.start()
        return {"running": True, "code": code}

    def use_session(self, session_id, code=None, title=None):
        """Khóa CỨNG 1 phiên (thủ công) cho comment + ghim SP — _loop KHÔNG tự đổi/reset
        (tránh status sessionList chập chờn). Tự khởi động loop nếu chưa chạy."""
        self.code = (code or self.code or "VN").strip().upper()
        self.session_id = str(session_id)
        self.session_title = title or self.session_title
        self.manual = True
        self.error = None
        self._reset_start_ts = True
        if not self.running:
            self._stop.clear()
            self.running = True
            self._thread = threading.Thread(target=self._loop, args=(self.code,), daemon=True)
            self._thread.start()
        return self.status()

    def stop(self):
        self._stop.set()
        self.running = False
        if self._thread:
            self._thread.join(timeout=3)
        self._thread = None
        return {"running": False}

    def status(self):
        return {
            "running": self.running,
            "code": self.code,
            "session_id": self.session_id,
            "session_title": self.session_title,
            "manual": self.manual,
            "error": self.error,
            "last_poll": self.last_poll,
            "count": len(self._comments),
            "last_seq": self._seq,
        }

    def comments_since(self, since=0):
        since = int(since or 0)
        with self._lock:
            items = [c for c in self._comments if c["seq"] > since]
        return items


# Singleton dùng chung trong tiến trình server.
scanner = CommentScanner()
