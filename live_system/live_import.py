"""Import hàng loạt sản phẩm / video từ CSV hoặc Excel (.xlsx).

Tách riêng khỏi UI để tái dùng (CLI, hoặc khi đóng gói thành tool). Header hỗ trợ song ngữ
Việt/Anh qua bảng ánh xạ; Google Sheet → export ra CSV/XLSX rồi dùng.
"""
import csv
from pathlib import Path

import live_database as db

ROOT = Path(__file__).parent.parent
DOWNLOADS_DIR = ROOT / "downloads"
VIDEOS_DIR = Path(__file__).parent / "videos"   # nơi multi-upload lưu video


def _resolve_file(fp):
    """File trong CSV chỉ là tên -> tìm trong videos/ rồi downloads/. Tuyệt đối thì giữ nguyên."""
    fp = str(fp)
    if "/" in fp or Path(fp).is_absolute():
        return fp
    for d in (VIDEOS_DIR, DOWNLOADS_DIR):
        cand = d / fp
        if cand.exists():
            return str(cand)
    return fp

# Ánh xạ header (đã hạ chữ thường, bỏ khoảng trắng thừa) -> tên cột chuẩn.
_PRODUCT_MAP = {
    "ten": "name", "ten san pham": "name", "tên": "name", "tên sản phẩm": "name", "name": "name",
    "ma": "sku", "sku": "sku", "ma san pham": "sku", "mã": "sku", "mã sp": "sku", "mã sản phẩm": "sku",
    "link": "link", "link san pham": "link", "link sản phẩm": "link", "url": "link",
    "gia": "price", "giá": "price", "gia goc": "price", "giá gốc": "price", "price": "price",
    "gia km": "sale_price", "giá km": "sale_price", "gia khuyen mai": "sale_price",
    "giá khuyến mãi": "sale_price", "sale": "sale_price", "sale_price": "sale_price",
    "hoa hong": "commission", "hoa hồng": "commission", "commission": "commission",
    "ton kho": "stock", "tồn kho": "stock", "stock": "stock", "kho": "stock",
    "mo ta": "description", "mô tả": "description", "description": "description", "desc": "description",
    "script": "script", "kich ban": "script", "kịch bản": "script",
    "nhom": "group_name", "nhóm": "group_name", "group": "group_name", "group_name": "group_name",
    "ghim": "pin_order", "thu tu ghim": "pin_order", "thứ tự ghim": "pin_order", "pin": "pin_order",
    "anh": "image_path", "ảnh": "image_path", "image": "image_path", "image_path": "image_path",
}
_VIDEO_MAP = {
    "ten": "name", "tên": "name", "ten video": "name", "tên video": "name", "name": "name",
    "file": "file_path", "file path": "file_path", "duong dan": "file_path", "đường dẫn": "file_path",
    "file_path": "file_path", "video": "file_path",
    "item_id": "item_id", "item id": "item_id", "shopee_item_id": "item_id",
    "ma item": "item_id", "mã item": "item_id", "itemid": "item_id",
    "san pham": "product_name", "sản phẩm": "product_name", "product": "product_name",
    "product_name": "product_name", "ten san pham": "product_name",
    "thoi luong": "duration", "thời lượng": "duration", "duration": "duration",
    "nhom": "group_name", "nhóm": "group_name", "group_name": "group_name",
    "category": "category", "danh muc": "category", "danh mục": "category",
    "loai": "category", "loại": "category", "phan loai": "category",
    "intent": "intent_code", "y dinh": "intent_code", "ý định": "intent_code",
    "uu tien": "priority", "ưu tiên": "priority", "priority": "priority",
    "gioi han phat": "play_limit", "giới hạn phát": "play_limit", "play_limit": "play_limit",
}
_NUMERIC = {"price", "sale_price", "commission", "duration", "priority"}
_INT = {"stock", "pin_order", "play_limit", "priority"}


def _norm(h):
    return str(h).strip().lower()


def _coerce(col, val):
    if val is None:
        return None
    s = str(val).strip()
    if s == "" or s.lower() in ("nan", "none"):
        return None
    if col in _NUMERIC or col in _INT:
        s2 = s.replace(",", "").replace("đ", "").replace("%", "").replace(" ", "")
        try:
            return int(float(s2)) if col in _INT else float(s2)
        except ValueError:
            return None
    return s


def _read_rows(path):
    """Đọc file -> list[dict] header thô (chưa map). Hỗ trợ .csv và .xlsx."""
    path = str(path)
    if path.lower().endswith((".xlsx", ".xls")):
        import pandas as pd
        df = pd.read_excel(path, dtype=str)
        return [{k: r[k] for k in df.columns} for _, r in df.iterrows()]
    # CSV (tự nhận diện encoding utf-8-sig để nuốt BOM từ Excel)
    with open(path, newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def _map_rows(raw_rows, mapping):
    out = []
    for raw in raw_rows:
        rec = {}
        for k, v in raw.items():
            col = mapping.get(_norm(k))
            if not col:
                continue
            cv = _coerce(col, v)
            if cv is not None:        # bỏ ô trống -> để DB dùng giá trị mặc định
                rec[col] = cv
        if rec:
            out.append(rec)
    return out


def import_products(path):
    """ĐÃ BỎ — sản phẩm giờ tự đồng bộ từ phiên live (Shopee item_id), không import CSV nữa.
    Giữ chữ ký (0,0,0) để UI cũ không vỡ."""
    return (0, 0, 0)


def import_videos(path):
    """Import video từ CSV/XLSX -> link product (item_id/tên/parse tên) + intent (answer_videos).
    File chỉ là tên -> tìm trong videos/ rồi downloads/. Trả dict {added,linked,answer,review,...}."""
    rows = _map_rows(_read_rows(path), _VIDEO_MAP)
    for r in rows:
        fp = r.get("file_path") or r.get("name")
        if fp:
            r["file_path"] = _resolve_file(fp)
    return db.bulk_add_videos(rows)


# ---- Templates -------------------------------------------------------------

_PRODUCT_TEMPLATE_HEADERS = ["Tên sản phẩm", "SKU", "Link", "Giá gốc", "Giá KM",
                             "Hoa hồng", "Tồn kho", "Nhóm", "Thứ tự ghim", "Mô tả", "Script"]
_VIDEO_TEMPLATE_HEADERS = ["Tên video", "File", "item_id", "Intent", "Thời lượng", "Nhóm", "Ưu tiên"]


def write_templates(dirpath=None):
    dirpath = Path(dirpath or (Path(__file__).parent / "templates"))
    dirpath.mkdir(parents=True, exist_ok=True)
    p = dirpath / "products_template.csv"
    v = dirpath / "videos_template.csv"
    with open(p, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(_PRODUCT_TEMPLATE_HEADERS)
        w.writerow(["Serum Dưỡng Ẩm HA", "SP001", "https://shopee.vn/product/123",
                    "450000", "299000", "20", "120", "serum", "1",
                    "Cấp ẩm sâu, ngừa lão hóa", "Serum HA giúp da căng mịn..."])
    with open(v, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(_VIDEO_TEMPLATE_HEADERS)
        w.writerow(["Set kẹp tóc - giới thiệu", "set_kep_toc.mp4", "23525384022", "", "", "kẹp", "5"])
        w.writerow(["Set kẹp tóc - hỏi giá", "set_kep_toc_gia.mp4", "23525384022", "ASK_PRICE", "", "kẹp", "5"])
        w.writerow(["Hướng dẫn đặt hàng chung", "huong_dan_mua.mp4", "", "ASK_BUY", "", "", "5"])
    return str(p), str(v)


if __name__ == "__main__":
    print("templates:", write_templates())
