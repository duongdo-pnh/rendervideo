"""Gọi API Shopee Live qua dịch vụ api.relive.vn: lấy sản phẩm trong phiên live + ghim sản phẩm.

Tham chiếu API_Shopee.txt:
  POST https://api.relive.vn/livestream/items  -> danh sách SP trong live (item_id, shop_id...)
  POST https://api.relive.vn/livestream/show   -> ghim 1 SP (item = {"item_id","shop_id"})
Cookie lấy từ bảng shopee_cookies (extension đẩy về). session_id = phiên đang live.
"""
import json
import time
from pathlib import Path

import httpx

import live_database as db
from match_util import _name_sim

APIKEY = "AZl(hddz%3Uj=6MgWI#_bNTriQ0XKWhoS=Qx2tk#JDXG=hUpmTb_DXZM$Ej@Y}p}"
BASE = "https://api.relive.vn"
IMAGES_DIR = Path(__file__).parent / "product_images"   # cùng nơi server đọc ảnh SP

_items_cache = {}   # session_id -> (ts, items)
_ITEMS_TTL = 30

# code quốc gia -> tên miền trang mua (để dựng link SP)
_BUYER_DOMAIN = {"VN": "shopee.vn", "MY": "shopee.com.my", "TH": "shopee.co.th",
                 "ID": "shopee.co.id", "PH": "shopee.ph", "TW": "shopee.tw", "SG": "shopee.sg"}


def _image_url(img_hash, code="VN"):
    if not img_hash:
        return None
    cc = (code or "vn").lower()
    return f"https://down-{cc}.img.susercontent.com/file/{img_hash}"


def _product_link(shop_id, item_id, code="VN"):
    if not (shop_id and item_id):
        return None
    return f"https://{_BUYER_DOMAIN.get((code or 'VN').upper(), 'shopee.vn')}/product/{shop_id}/{item_id}"


def _clean_price(v):
    """Giá Shopee đôi khi ở dạng micro (×100000). Trả chuỗi VND gọn."""
    try:
        n = float(v)
    except (TypeError, ValueError):
        return str(v) if v not in (None, "") else None
    if n >= 1e8:        # micro -> chia 100000
        n = n / 100000
    return str(int(n))


def _download_image(img_hash, item_id, code="VN"):
    """Tải ảnh SP từ Shopee CDN về product_images/ (cho OBS dùng file local). Trả path hoặc None."""
    url = _image_url(img_hash, code)
    if not url:
        return None
    try:
        IMAGES_DIR.mkdir(parents=True, exist_ok=True)
        dst = IMAGES_DIR / f"live_{item_id}.jpg"
        if dst.exists() and dst.stat().st_size > 0:
            return str(dst)
        r = httpx.get(url, timeout=15, follow_redirects=True)
        if r.status_code == 200 and r.content:
            dst.write_bytes(r.content)
            return str(dst)
    except Exception:
        pass
    return None


def _cookie(code="VN"):
    row = db.get_shopee_cookie(code)
    if not row or not row.get("cookie"):
        raise RuntimeError(f"Chưa có cookie {code} (lấy qua extension).")
    return row["cookie"]


def _post(path, session_id, code="VN", extra=None):
    payload = {"apikey": APIKEY, "cookie": _cookie(code), "session_id": int(session_id),
               "country": (code or "vn").lower(), "proxy": ""}
    if extra:
        payload.update(extra)
    r = httpx.post(f"{BASE}{path}", json=payload, timeout=30)
    r.raise_for_status()
    return r.json()


def get_live_items(session_id, code="VN", use_cache=True):
    """Danh sách SP trong phiên live: [{item_id, shop_id, name, price, ...}]."""
    sid = int(session_id)
    if use_cache:
        c = _items_cache.get(sid)
        if c and time.time() - c[0] < _ITEMS_TTL:
            return c[1]
    body = _post("/livestream/items", sid, code)
    # bóc: data.data.items
    data = (body or {}).get("data") or {}
    inner = data.get("data") if isinstance(data, dict) else {}
    items = (inner or {}).get("items") or []
    out = []
    for it in items:
        if not isinstance(it, dict):
            continue
        iid = it.get("item_id") or it.get("itemId")
        shop = it.get("shop_id") or it.get("shopId")
        img = it.get("image") or it.get("image_hash") or ""
        out.append({
            "item_id": iid,
            "shop_id": shop,
            "name": it.get("name") or it.get("title") or "",
            "price": _clean_price(it.get("price") or it.get("price_min")),
            "image": img,
            "image_url": _image_url(img, code),
            "link": _product_link(shop, iid, code),
            "sold": it.get("sold"),
            "stock": it.get("display_total_stock") or it.get("normal_stock"),
            "raw": it,
        })
    _items_cache[sid] = (time.time(), out)
    return out


def pin_item(session_id, item_id, shop_id, code="VN"):
    """Ghim 1 SP lên live. Trả (ok, message)."""
    item = json.dumps({"item_id": int(item_id), "shop_id": int(shop_id)})
    body = _post("/livestream/show", int(session_id), code, extra={"item": item})
    ok = bool(body.get("success"))
    inner = (body.get("data") or {})
    msg = inner.get("err_msg") or body.get("message") or ("OK" if ok else "thất bại")
    # err_code 0 = thành công
    if isinstance(inner, dict) and inner.get("err_code") not in (0, None):
        ok = False
    return ok, msg


def sync_products_from_live(session_id, code="VN", threshold=0.85):
    """Dựng/đồng bộ catalog products từ SP trong phiên live (theo shopee_item_id).
    - item đã có product (theo item_id) -> cập nhật tên + shop_id.
    - chưa có: nếu trùng tên 1 product CHƯA gắn item_id (>=threshold) -> attach (tránh nhân bản);
      ngược lại tạo product mới. Trả {created, updated, attached, total}."""
    items = get_live_items(session_id, code, use_cache=False)
    created = updated = attached = 0
    prods = db.get_all_products()
    for it in items:
        iid = it.get("item_id")
        if iid in (None, ""):
            continue
        # Đủ thông tin: tên, shop_id, link, giá, sold, tồn, ẢNH (tải về file local cho OBS).
        fields = {
            "name": it["name"] or f"SP {iid}",
            "shop_id": str(it["shop_id"]) if it.get("shop_id") else None,
            "link": it.get("link"), "price": it.get("price"),
            "sold": it.get("sold"), "stock": it.get("stock"),
        }
        img_path = _download_image(it.get("image"), iid, code)
        if img_path:
            fields["image_path"] = img_path
        existing = db.find_product_by_item_id(iid)
        if existing:
            db.update_product(existing.id, **fields)
            updated += 1
            continue
        # chưa có item_id này: thử attach vào product cùng tên chưa gắn item_id (tránh nhân bản)
        cand, best = None, 0.0
        for p in prods:
            if getattr(p, "shopee_item_id", None):
                continue
            s = _name_sim(p.name, it["name"])
            if s > best:
                cand, best = p, s
        if cand and best >= threshold:
            db.update_product(cand.id, shopee_item_id=str(iid), **fields)
            attached += 1
        else:
            pid = db.add_product(fields["name"], fields.get("image_path"), fields.get("link"))
            db.update_product(pid, shopee_item_id=str(iid), **{k: v for k, v in fields.items()
                                                               if k not in ("name", "image_path", "link")})
            created += 1
        prods = db.get_all_products()
    return {"created": created, "updated": updated, "attached": attached, "total": len(items)}


def auto_match_products(session_id, code="VN", threshold=0.85, only_missing=False):
    """Tự gán shopee_item_id cho SP bằng cách khớp TÊN với SP trong live (fuzzy ≥ threshold).
    Trả báo cáo [{product_id, product, item_id, item_name, score, set}]."""
    items = get_live_items(session_id, code, use_cache=False)
    report = []
    for p in db.get_all_products():
        if only_missing and getattr(p, "shopee_item_id", None):
            continue
        best, best_score = None, 0.0
        for it in items:
            s = _name_sim(p.name, it["name"])
            if s > best_score:
                best, best_score = it, s
        row = {"product_id": p.id, "product": p.name, "score": round(best_score, 2),
               "item_id": best["item_id"] if best else None,
               "item_name": best["name"] if best else None, "set": False}
        if best and best_score >= threshold:
            db.update_product(p.id, shopee_item_id=str(best["item_id"]))
            row["set"] = True
        report.append(row)
    return report


def pin_product(product, session_id, code="VN"):
    """Ghim SP (theo product) lên live nếu bật setting shopee_pin + tìm được item. Trả (ok,msg) hoặc None."""
    import logging
    log = logging.getLogger("live")
    if db.get_setting("shopee_pin", True) is False:
        log.info("Ghim: TẮT (shopee_pin=false)"); return None
    if not session_id:
        log.info("Ghim: bỏ qua — chưa có session_id (scanner chưa chạy?)"); return None
    if not product:
        return None
    it = find_item_for_product(session_id, product, code)
    if not it:
        log.info(f"Ghim: SP '{product.name}' (item_id={getattr(product,'shopee_item_id',None)}) "
                 f"KHÔNG khớp item nào trong live {session_id}"); return None
    res = pin_item(session_id, it["item_id"], it["shop_id"], code)
    log.info(f"Ghim SP '{product.name}' -> item {it['item_id']}: {res}")
    return res


def find_item_for_product(session_id, product, code="VN"):
    """Tìm item_id+shop_id của 1 sản phẩm: ưu tiên product.shopee_item_id, fallback khớp tên."""
    items = get_live_items(session_id, code)
    if not items:
        return None
    sid_item = getattr(product, "shopee_item_id", None)
    if sid_item:
        for it in items:
            if str(it["item_id"]) == str(sid_item):
                return it
    # fallback: khớp tên gần đúng
    name = (product.name or "").lower().strip()
    for it in items:
        if name and name in (it["name"] or "").lower():
            return it
    return None
