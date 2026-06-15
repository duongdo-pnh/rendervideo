"""Xử lý 1 comment livestream theo luồng: normalize → product + intent (keyword → AI fallback)
→ chọn video phù hợp → phát OBS → ghim SP → phát xong về playlist.

Luồng chọn video (theo spec):
  6. product + intent      → video trả lời đúng product+intent (answer_videos).
  7. chỉ intent (no product)→ dùng SP đang phát/ghim (context) → video product+intent; nếu không có → video chung intent.
  8. chỉ product (no intent)→ video giới thiệu SP (video thư viện gắn SP).
  9. intent chung (no product, no context) → video trả lời chung của shop (answer product_id NULL).
 10. không gì       → bỏ qua / đưa vào hàng chờ nhân viên.

Cooldown theo (product, intent) + gộp comment trùng. Không block vòng scan (chạy nhanh; AI có timeout).
"""
import threading
import time
from pathlib import Path

import live_database as db
from intent_matcher import matcher, normalize, ai_enabled, get_threshold

DEFAULT_COOLDOWN = 45      # giây, cho cùng (product,intent)
DEDUPE_WINDOW = 6          # giây, gộp comment y hệt


class CommentHandler:
    def __init__(self, controller=None):
        self.controller = controller
        self._lock = threading.Lock()
        self._cooldown = {}     # (product_id,intent_id) -> last_fire_ts
        self._recent = {}       # normalized_content -> last_seen_ts (gộp trùng)
        self.staff_queue = []   # comment không match được (bước 10)

    # ---------- quyết định video ----------
    def _intro_video(self, product):
        return db.get_intro_video(product.id, pick=db.get_setting("video_pick", "rotate")) if product else None

    def _decide(self, product, intent, ctx_product):
        """Trả dict mô tả video sẽ phát, hoặc None nếu không có gì để phát."""
        eff = product or ctx_product
        # 6/7: intent + (product rõ hoặc context) → answer video product+intent
        if intent and eff:
            av, scope = db.pick_answer(intent.id, eff.id)
            if av:
                return {"kind": "answer", "av": av, "product": eff, "intent": intent,
                        "scope": "product" if scope == "product" else "general",
                        "video": Path(av.file_path).name}
        # 9: intent chung, không có product/context → answer chung của shop
        if intent and not eff:
            av, _ = db.pick_answer(intent.id, None)
            if av:
                return {"kind": "answer", "av": av, "product": None, "intent": intent,
                        "scope": "shop", "video": Path(av.file_path).name}
        # 8: có product rõ nhưng không có answer phù hợp → video giới thiệu SP
        if product:
            v = self._intro_video(product)
            if v:
                return {"kind": "intro", "video_obj": v, "product": product, "intent": intent,
                        "scope": "intro", "video": Path(v.file_path).name}
        # 7 (fallback): intent + context nhưng không có answer/intro của product rõ → giới thiệu SP context
        if intent and eff:
            v = self._intro_video(eff)
            if v:
                return {"kind": "intro", "video_obj": v, "product": eff, "intent": intent,
                        "scope": "intro_ctx", "video": Path(v.file_path).name}
        return None

    # ---------- phát ----------
    def _play(self, decision):
        if not self.controller:
            return
        mode = db.get_setting("trigger_mode", "play_now")
        product = decision.get("product")
        if product:
            try:
                from live_controller import SRC_IMAGE
                self.controller.obs.set_image(SRC_IMAGE, product.image_path or "")
            except Exception:
                pass
            self.controller.current_product = product.name
            self.controller.current_product_obj = product
            self.controller.last_matched_product = product
            # KHÔNG ghim ở đây — video sẽ lên qua _bring_up của controller, ghim 1 chỗ duy nhất ở đó.
        if decision["kind"] == "answer":
            self.controller.enqueue_answer(decision["av"].id, mode=mode)
        else:  # intro = video thư viện gắn SP
            v = decision["video_obj"]
            db.mark_video_triggered(v.id)
            if mode == "enqueue":
                self.controller.enqueue(v.id)
            else:
                self.controller.play_now(v.id)

    def _pin_shopee(self, product):
        """Ghim SP trên Shopee qua api.relive.vn (nếu bật + tìm được item trong phiên live)."""
        if db.get_setting("shopee_pin", True) is False:
            return
        try:
            from shopee_scanner import scanner
            import shopee_api
            sid = scanner.session_id
            code = scanner.code or "VN"
            if not sid:
                return
            it = shopee_api.find_item_for_product(sid, product, code)
            if not it:
                return
            shopee_api.pin_item(sid, it["item_id"], it["shop_id"], code)
        except Exception:
            pass    # không để lỗi ghim làm hỏng luồng phát video

    # ---------- cooldown / dedupe ----------
    def _cooled_down(self, product, intent):
        key = (product.id if product else 0, intent.id if intent else 0)
        cd = DEFAULT_COOLDOWN
        try:
            cd = int(db.get_setting("answer_cooldown_sec", DEFAULT_COOLDOWN))
        except Exception:
            pass
        if intent and getattr(intent, "cooldown_sec", None):
            cd = int(intent.cooldown_sec)
        now = time.time()
        last = self._cooldown.get(key, 0)
        if now - last < cd:
            return False        # còn trong cooldown → KHÔNG phát
        self._cooldown[key] = now
        return True

    def _is_duplicate(self, content):
        now = time.time()
        k = normalize(content)
        last = self._recent.get(k, 0)
        self._recent[k] = now
        if len(self._recent) > 500:
            for kk in [kk for kk, ts in self._recent.items() if now - ts > 60]:
                self._recent.pop(kk, None)
        return (now - last) < DEDUPE_WINDOW

    # ---------- API chính ----------
    def handle(self, comment, test_only=False):
        content = comment.get("content") or ""

        product, p_conf, p_method = matcher.match_product(content)
        intent, i_method = matcher.match_intent(content)
        method = p_method if product else ("keyword" if intent else "no_match")
        reason = ""

        # AI fallback nếu thiếu product HOẶC intent.
        if (product is None or intent is None) and ai_enabled():
            ap, ai_intent, aconf, areason = matcher.ai_classify(content)
            reason = areason
            if product is None and ap and aconf >= get_threshold():
                product, p_conf, p_method, method = ap, aconf, "ai", "ai"
            if intent is None and ai_intent:
                intent, i_method = ai_intent, "ai"
                if method == "no_match":
                    method = "ai"

        ctx_product = (product or
                       (self.controller.current_product_obj if self.controller else None) or
                       (getattr(self.controller, "last_matched_product", None) if self.controller else None))

        decision = self._decide(product, intent, ctx_product)

        result = {
            "matched": bool(product or intent),
            "product": {"id": product.id, "name": product.name} if product else None,
            "intent": intent.name if intent else None,
            "confidence": round(float(p_conf) * 100),
            "method": method,                 # keyword | ai | no_match
            "reason": reason,
            "video": decision["video"] if decision else None,
            "scope": decision["scope"] if decision else None,   # product|general|shop|intro|intro_ctx
            "triggered": False,
            "ctx_product": (ctx_product.name if ctx_product and not product else None),
        }

        if not decision:
            # bước 10: không có gì để phát → hàng chờ nhân viên (chỉ khi không match gì cả)
            if not test_only and not (product or intent):
                self.staff_queue.append({"content": content, "ts": int(time.time()),
                                         "user": comment.get("username") or comment.get("user")})
            self._log(comment, content, product, intent, method, p_conf, False)
            return result

        if test_only:
            result["would_play"] = decision["video"]
            return result

        # dedupe + cooldown trước khi phát
        if self._is_duplicate(content):
            result["skipped"] = "duplicate"
            self._log(comment, content, product, intent, method, p_conf, False)
            return result
        if not self._cooled_down(product, intent):
            result["skipped"] = "cooldown"
            self._log(comment, content, product, intent, method, p_conf, False)
            return result

        do_trigger = db.get_setting("auto_trigger", True) is not False
        if do_trigger:
            self._play(decision)
            result["triggered"] = True

        self._log(comment, content, product, intent, method, p_conf, result["triggered"])
        return result

    def _log(self, comment, content, product, intent, method, conf, triggered):
        try:
            db.log_comment(
                comment_id=comment.get("id"),
                user_id=str(comment.get("userId") or comment.get("user_id") or comment.get("user") or ""),
                content=content,
                matched_product_id=product.id if product else None,
                confidence=conf,
                match_method=(method + (":" + intent.name if intent else "")) if method != "no_match" else "no_match",
                triggered=triggered)
        except Exception:
            pass


# singleton (controller gắn trong server.py)
handler = CommentHandler()
