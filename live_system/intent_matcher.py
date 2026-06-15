"""Khớp comment livestream → (sản phẩm, intent).

- normalize(): lowercase + bỏ dấu tiếng Việt + gom khoảng trắng (khớp ổn định, không lệ thuộc dấu).
- Tầng 1 (keyword/rule, free):
    * product: khớp keyword bảng product_triggers HOẶC đủ-từ-của-tên-SP.
    * intent: khớp keyword các intent (intents.keywords), chọn theo độ ưu tiên INTENT_PRIORITY.
- Tầng 2 (AI fallback): chỉ gọi khi tầng 1 thiếu product HOẶC intent. 1 lời gọi trả CẢ product_id + intent.

Triggers cache, tự reload 60s.
"""
import re
import threading
import time

import live_database as db
from core import ai_helper
# Helper khớp tên/chuỗi tách ra match_util (thuần, tránh vòng import với live_database/shopee_api).
from match_util import (normalize, _tokens, _edit_dist1, _phrase_in,
                        _longest_token_run, _token_fuzzy_subset)

DEFAULT_THRESHOLD = 0.85


def get_threshold():
    try:
        return float(db.get_setting("ai_threshold", DEFAULT_THRESHOLD))
    except Exception:
        return DEFAULT_THRESHOLD


def ai_enabled():
    if db.get_setting("ai_enabled", True) is False:
        return False
    return ai_helper.is_configured()


class IntentMatcher:
    def __init__(self):
        self._lock = threading.Lock()
        self.triggers = []
        self.last_reload = 0
        self.reload()

    def reload(self):
        with self._lock:
            self.triggers = db.get_all_triggers()
            self.last_reload = time.time()

    def auto_reload(self):
        if time.time() - self.last_reload > 60:
            self.reload()

    # ---- tầng 1: product (keyword/tên) ----
    def match_product(self, text):
        t = normalize(text)
        if not t:
            return None, 0.0, "no_match"
        cl = t.split()
        with self._lock:
            triggers = list(self.triggers)
        for trg in triggers:
            kw = normalize(trg.keyword)
            if kw and _phrase_in(kw, cl):
                return db.get_product(trg.product_id), 1.0, "keyword"
        ctoks = set(re.findall(r"\w+", t))
        prods = sorted(db.get_all_products(), key=lambda p: -len(_tokens(p.name)))
        # 1) khớp chính xác (đủ từ)
        for p in prods:
            ntoks = _tokens(p.name)
            if ntoks and ntoks <= ctoks:
                return p, 1.0, "keyword"
        # 2) khớp mờ (sai/thiếu 1 ký tự mỗi từ) — bắt lỗi gõ 'keo qua'↔'keo que'
        for p in prods:
            ntoks = _tokens(p.name)
            if ntoks and _token_fuzzy_subset(ntoks, ctoks):
                return p, 0.9, "fuzzy"
        # 3) khớp theo ĐOẠN TỪ LIÊN TIẾP DÀI NHẤT: khách gõ NGẮN ('3 kẹp phồng tóc') vẫn ra
        #    SP tên DÀI chứa nguyên cụm đó ('combo 3 kẹp phồng tóc mái...'). Chọn SP có cụm
        #    liên tiếp trùng dài nhất (≥2 từ) — tránh nhiễu từ rời rạc trùng tình cờ.
        cl = normalize(t).split()
        best, best_run = None, 0
        for p in prods:
            run = _longest_token_run(cl, normalize(p.name).split())
            if run > best_run:
                best, best_run = p, run
        if best and best_run >= 2:
            return best, 0.8, "overlap"
        return None, 0.0, "no_match"

    # ---- tầng 1: intent (keyword) ----
    def match_intent(self, text):
        t = normalize(text)
        if not t:
            return None, "no_match"
        cl = t.split()
        matched = {}
        for it in db.list_intents():
            if not it.enabled:
                continue
            for kw in (it.keywords or "").split(","):
                kw = normalize(kw)
                if kw and _phrase_in(kw, cl):
                    matched[it.name] = it
                    break
        if not matched:
            return None, "no_match"
        # chọn intent có priority cao nhất (cột intents.priority — chỉnh được trong UI Kịch bản).
        best = max(matched.values(), key=lambda it: (getattr(it, "priority", 0) or 0))
        return best, "keyword"

    # ---- tầng 2: AI phân loại gộp product + intent ----
    def _ai_prompt(self, comment, products, intents):
        plist = "\n".join(
            f"- ID {p.id}: {p.name}" +
            (f" (từ khóa: {', '.join(t.keyword for t in db.get_triggers_by_product(p.id))})"
             if db.get_triggers_by_product(p.id) else "")
            for p in products) or "(chưa có sản phẩm)"
        ilist = "\n".join(f"- {it.name}" for it in intents) or "(không có)"
        return f"""Bạn là AI phân tích comment livestream bán hàng.

Sản phẩm đang bán:
{plist}

Các nhóm intent (ý định) có thể:
{ilist}

Comment của khách: "{comment}"

Lưu ý: comment khách thường GÕ SAI CHÍNH TẢ, viết KHÔNG DẤU, thiếu/thừa chữ (vd "keo qua" = "kẹo que",
"bun bo" = "bún bò"). Hãy suy luận về sản phẩm GẦN GIỐNG nhất trong danh sách trên.

Nhiệm vụ: xác định khách hỏi SẢN PHẨM nào và Ý ĐỊNH gì. Chỉ trả JSON, không giải thích thừa:
{{"product_id": <id sản phẩm hoặc null>, "intent": "<TÊN_INTENT hoặc null>", "confidence": <0.0-1.0>, "reason": "<ngắn>"}}

- Nếu comment gần giống tên/biến thể 1 sản phẩm → trả product_id đó (đừng để null chỉ vì sai chính tả).
- Không rõ ý định → intent null. confidence là độ chắc chắn tổng thể."""

    def ai_classify(self, comment):
        products = db.get_all_products()
        intents = [it for it in db.list_intents() if it.enabled]
        try:
            data = ai_helper.call_ai_json(self._ai_prompt(comment, products, intents))
        except Exception as e:
            return None, None, 0.0, f"AI lỗi: {e}"
        pid = data.get("product_id")
        iname = data.get("intent")
        conf = float(data.get("confidence") or 0)
        reason = data.get("reason") or ""
        product = db.get_product(int(pid)) if pid else None
        intent = db.get_intent_by_name(iname) if iname else None
        return product, intent, conf, reason


# singleton
matcher = IntentMatcher()
