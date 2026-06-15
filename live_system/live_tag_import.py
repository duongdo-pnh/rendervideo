"""Quét thư mục downloads/ nạp video render xong vào live library + TỰ ĐỊNH NGHĨA sản phẩm.

Mỗi video.mp4 có thể đi kèm sidecar video.mp4.json (do queue_worker ghi lúc render) mang
shopee_item_id/kind/intent_code -> gắn deterministic. Không có sidecar -> fuzzy tên file (ngưỡng
settings ai_threshold). Idempotent theo file_path (đã có trong DB thì bỏ qua).

CLI:  python live_tag_import.py   (quét downloads/ cạnh repo)
"""
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import live_database as db

DOWNLOADS = Path(__file__).resolve().parent.parent / "downloads"


def _duration(path):
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, timeout=30)
        return float((out.stdout or "").strip() or 0)
    except Exception:
        return 0.0


def _read_sidecar(mp4):
    sc = Path(str(mp4) + ".json")
    if not sc.exists():
        return None
    try:
        return json.loads(sc.read_text(encoding="utf-8"))
    except Exception:
        return None


def import_tagged(downloads_dir=None):
    """Nạp các *.mp4 chưa có trong DB. Trả report số liệu."""
    downloads = Path(downloads_dir or DOWNLOADS)
    rep = {"added": 0, "linked": 0, "answer": 0, "created_products": 0,
           "review": 0, "unmatched": 0, "skipped": 0}
    if not downloads.exists():
        return rep
    thr = float(db.get_setting("ai_threshold", 0.85))
    for mp4 in sorted(downloads.glob("*.mp4")):
        fp = str(mp4)
        if db.find_video_by_path(fp) or db.find_answer_by_path(fp):
            rep["skipped"] += 1
            continue
        man = _read_sidecar(mp4)
        dur = _duration(fp)
        name = mp4.stem

        # (a) Video TRẢ LỜI theo intent (sidecar kind=answer) -> answer_videos
        if man and man.get("kind") == "answer" and man.get("intent_code"):
            it = db.get_intent_by_name(man["intent_code"])
            if it:
                pid = None
                if man.get("product_item_id"):
                    existed = db.find_product_by_item_id(man["product_item_id"])
                    pid = db.get_or_create_product_from_item(
                        man["product_item_id"], man.get("shop_id"), man.get("product_name"))
                    if not existed:
                        rep["created_products"] += 1
                avid = db.add_answer_video(it.id, fp, name, dur, pid)
                db.update_answer_video(avid, match_status="manifest", match_score=1.0)
                rep["added"] += 1
                rep["answer"] += 1
                if pid:
                    rep["linked"] += 1
                continue
            # intent_code không khớp -> rớt xuống xử lý như video thường

        # (b) Video GIỚI THIỆU / thư viện -> videos, resolve product
        created_before = bool(man and man.get("product_item_id")
                              and not db.find_product_by_item_id(man["product_item_id"]))
        pid, score, status, cand = db.resolve_product_for_video(fp, manifest=man, thr_auto=thr)
        if created_before and status == "manifest":
            rep["created_products"] += 1
        db.add_video(fp, pid, dur,
                     category=(man.get("intent_code") if man else None),
                     match_status=status, match_score=score, match_candidates=cand)
        rep["added"] += 1
        if pid:
            rep["linked"] += 1
        if status == "review":
            rep["review"] += 1
        elif status == "unmatched":
            rep["unmatched"] += 1
    return rep


if __name__ == "__main__":
    db.init_db()
    print(import_tagged())
