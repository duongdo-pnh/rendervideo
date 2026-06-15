"""SQLite store cho hệ thống live (live_system/live.db).

3 bảng: products, videos, playlist. Các record đơn (get_product / get_next_unplayed_video)
trả về SimpleNamespace để truy cập theo thuộc tính (vd video.file_path, product.name) đúng
như pseudocode controller; các hàm list trả về list SimpleNamespace.
"""
import json
import re
import sqlite3
from pathlib import Path
from types import SimpleNamespace

from match_util import (_name_sim, _tokens, _token_fuzzy_subset,  # helper thuần, không vòng import
                        _longest_token_run, normalize, _token_list, _seq_contains, _seq_run)

DB_PATH = Path(__file__).parent / "live.db"


def _connect():
    con = sqlite3.connect(DB_PATH, timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=5000")
    con.execute("PRAGMA foreign_keys=ON")
    return con


def _ns(row):
    return SimpleNamespace(**dict(row)) if row else None


# Cột mở rộng thêm sau schema v1 (kèm SQL default). init_db() ALTER vào DB cũ để không mất dữ liệu.
# products đã đơn giản hóa còn 4 field (id/name/image_path/link) — không còn cột mở rộng.
_PRODUCT_COLUMNS = {}
_VIDEO_COLUMNS = {
    "name":        "TEXT",
    "group_name":  "TEXT",
    "category":    "TEXT",                         # danh mục/intent cho AI chọn video trả lời
    "priority":    "INTEGER NOT NULL DEFAULT 0",
    "play_limit":  "INTEGER NOT NULL DEFAULT 0",   # 0 = không giới hạn
    "is_error":    "INTEGER NOT NULL DEFAULT 0",
    "last_played_at": "TEXT",                      # lần cuối được trigger phát (xoay vòng video/SP)
}
# Cột được phép cập nhật động qua update_product / update_video.
_PRODUCT_EDITABLE = {"name", "image_path", "link", "shopee_item_id", "intro_video_id", "shop_id",
                     "price", "sold", "stock"}
_MATCH_EDITABLE = {"match_status", "match_score", "match_candidates"}
_VIDEO_EDITABLE = {"file_path", "product_id", "duration", "play_count"} | set(_VIDEO_COLUMNS) | _MATCH_EDITABLE


def _simplify_products(con):
    """Rút bảng products về 4 field (id/name/image_path/link). Tạo bảng mới, copy, drop, rename —
    GIỮ id (để video.product_id còn đúng). Bỏ bảng platform_products. Idempotent."""
    con.execute("DROP TABLE IF EXISTS platform_products")
    cols = {r["name"] for r in con.execute("PRAGMA table_info(products)")}
    if not cols:
        return  # chưa có bảng (init_db đã CREATE 4-field ở trên)
    # Chỉ rút gọn khi CÒN cột legacy 'sku' (schema products đời cũ). KHÔNG đụng các cột mới
    # hợp lệ (shopee_item_id/intro_video_id/shop_id...) — tránh xoá nhầm dữ liệu mapping.
    if "sku" not in cols:
        return
    has_link = "link" in cols
    con.execute("PRAGMA foreign_keys=OFF")
    con.execute("""CREATE TABLE products_new (
        id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, image_path TEXT, link TEXT,
        created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')))""")
    con.execute(f"INSERT INTO products_new (id, name, image_path, link) "
                f"SELECT id, name, image_path, {'link' if has_link else 'NULL'} FROM products")
    con.execute("DROP TABLE products")
    con.execute("ALTER TABLE products_new RENAME TO products")
    con.execute("PRAGMA foreign_keys=ON")


def _migrate(con, table, columns):
    existing = {r["name"] for r in con.execute(f"PRAGMA table_info({table})")}
    for col, decl in columns.items():
        if col not in existing:
            con.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")


def init_db():
    with _connect() as con:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS products (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                name        TEXT    NOT NULL,
                image_path  TEXT,
                link        TEXT,
                created_at  TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
            )
            """
        )
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS videos (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                file_path   TEXT    NOT NULL,
                product_id  INTEGER REFERENCES products(id) ON DELETE SET NULL,
                duration    REAL    NOT NULL DEFAULT 0,
                play_count  INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS playlists (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                name         TEXT    NOT NULL,
                play_mode    TEXT    NOT NULL DEFAULT 'order',
                group_filter TEXT,
                created_at   TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
            )
            """
        )
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS playlist (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                video_id    INTEGER NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
                pl_id       INTEGER REFERENCES playlists(id) ON DELETE CASCADE,
                order_index INTEGER NOT NULL DEFAULT 0,
                is_played   INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                name         TEXT    NOT NULL,
                platform     TEXT,
                rtmp_server  TEXT,
                stream_key   TEXT,
                scene        TEXT,
                start_at     TEXT,
                end_at       TEXT,
                auto_start   INTEGER NOT NULL DEFAULT 1,
                auto_stop    INTEGER NOT NULL DEFAULT 1,
                auto_recover INTEGER NOT NULL DEFAULT 1,
                pl_id        INTEGER REFERENCES playlists(id) ON DELETE SET NULL,
                status       TEXT    NOT NULL DEFAULT 'scheduled',
                error        TEXT,
                created_at   TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
                started_at   TEXT,
                ended_at     TEXT
            )
            """
        )
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS intents (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                name         TEXT    NOT NULL,
                keywords     TEXT,
                trigger_mode TEXT    NOT NULL DEFAULT 'enqueue',
                cooldown_sec INTEGER NOT NULL DEFAULT 30,
                enabled      INTEGER NOT NULL DEFAULT 1,
                created_at   TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
            )
            """
        )
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS answer_videos (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                intent_id      INTEGER REFERENCES intents(id) ON DELETE CASCADE,
                name           TEXT,
                file_path      TEXT    NOT NULL,
                duration       REAL    NOT NULL DEFAULT 0,
                product_id     INTEGER REFERENCES products(id) ON DELETE SET NULL,
                enabled        INTEGER NOT NULL DEFAULT 1,
                play_count     INTEGER NOT NULL DEFAULT 0,
                last_played_at TEXT,
                last_played_seq INTEGER NOT NULL DEFAULT 0,
                created_at     TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
            )
            """
        )
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT
            )
            """
        )
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS scene_assets (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                kind       TEXT    NOT NULL,           -- 'background' | 'banner' | 'tvc'
                name       TEXT,
                file_path  TEXT    NOT NULL,
                enabled    INTEGER NOT NULL DEFAULT 1,
                created_at TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
            )
            """
        )
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS shopee_cookies (
                code       TEXT PRIMARY KEY,          -- VN | MY | PH | TW | TH | ID
                domain     TEXT,
                cookie     TEXT NOT NULL,
                user_agent TEXT,
                updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
            )
            """
        )
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS product_triggers (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
                keyword    TEXT    NOT NULL,
                created_at TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
            )
            """
        )
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS comment_logs (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                comment_id        TEXT,
                user_id           TEXT,
                content           TEXT,
                matched_product_id INTEGER REFERENCES products(id) ON DELETE SET NULL,
                confidence        REAL    NOT NULL DEFAULT 0,
                match_method      TEXT    NOT NULL DEFAULT 'no_match',  -- keyword | ai | no_match
                triggered         INTEGER NOT NULL DEFAULT 0,
                created_at        TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
            )
            """
        )
        _simplify_products(con)   # rút products còn 4 field + bỏ bảng platform_products
        # Field mở rộng cho mapping product+intent+video (spec): item Shopee để ghim, video giới thiệu.
        _migrate(con, "products", {"shopee_item_id": "TEXT", "intro_video_id": "INTEGER",
                                   "shop_id": "TEXT", "price": "TEXT", "sold": "INTEGER",
                                   "stock": "INTEGER"})
        _migrate(con, "intents", {"priority": "INTEGER NOT NULL DEFAULT 100"})
        _migrate(con, "answer_videos", {"priority": "INTEGER NOT NULL DEFAULT 0"})
        _migrate(con, "videos", _VIDEO_COLUMNS)
        # Trạng thái auto-define video↔SP lúc import: manifest|auto|review|unmatched|confirmed.
        _MATCH_COLS = {"match_status": "TEXT NOT NULL DEFAULT 'unmatched'",
                       "match_score": "REAL NOT NULL DEFAULT 0", "match_candidates": "TEXT"}
        _migrate(con, "videos", _MATCH_COLS)
        _migrate(con, "answer_videos", _MATCH_COLS)
        con.execute("CREATE INDEX IF NOT EXISTS ix_products_item ON products(shopee_item_id)")
        con.execute("CREATE INDEX IF NOT EXISTS ix_products_name ON products(name)")
        # Backfill: video/answer đã có product_id sẵn (link tay trước đây) coi như đã xác nhận.
        con.execute("UPDATE videos SET match_status='confirmed' "
                    "WHERE product_id IS NOT NULL AND match_status='unmatched'")
        con.execute("UPDATE answer_videos SET match_status='confirmed' "
                    "WHERE product_id IS NOT NULL AND match_status='unmatched'")
        # Cột pl_id thêm sau cho DB cũ (gắn playlist vào nhóm + gắn phiên với playlist).
        _migrate(con, "playlist", {"pl_id": "INTEGER REFERENCES playlists(id) ON DELETE CASCADE"})
        _migrate(con, "playlists", {"play_mode": "TEXT NOT NULL DEFAULT 'order'", "group_filter": "TEXT",
                                    "autoplay": "INTEGER NOT NULL DEFAULT 1",
                                    "loop": "INTEGER NOT NULL DEFAULT 1"})
        _migrate(con, "sessions", {"pl_id": "INTEGER REFERENCES playlists(id) ON DELETE SET NULL",
                                    "profile": "TEXT"})
        _ensure_default_playlist(con)
        _seed_default_intents(con)


# Nhóm intent mặc định (keyword KHÔNG dấu — matcher chuẩn hóa bỏ dấu). priority cao = ưu tiên hơn
# khi 1 comment khớp nhiều intent (mua/giá/sp > ship/chất lượng — theo spec).
DEFAULT_INTENTS = [
    # (code, keywords, priority)
    ("ASK_BUY",      "mua sao,chot don,dat hang,mua o dau,lam sao mua,cach mua,order,chot,dat mua,mua nhu nao", 90),
    ("ASK_PRICE",    "gia,bao nhieu,bao nhiu,nhieu tien,gia sao,may tien,gia ca,nhiu tien", 85),
    ("ASK_PRODUCT",  "cho xem,xem san pham,san pham gi,xem hang,cho coi,coi hang", 80),
    ("ASK_STOCK",    "con hang,con khong,con size,con mau,het hang,con bao nhieu,con ko,con hang khong,co khong,co k,con k,co con,con khong shop", 70),
    ("ASK_VOUCHER",  "voucher,ma giam,giam gia,khuyen mai,ma giam gia,code,ma khuyen mai,co ma khong", 60),
    ("ASK_SHIPPING", "ship,phi ship,freeship,free ship,giao hang,bao lau toi,van chuyen,phi van chuyen,ship bao nhieu", 55),
    ("ASK_QUALITY",  "co tot khong,chat luong,co that khong,co xin khong,co ben khong,review,danh gia,co dep khong,xai co tot", 50),
    ("ASK_USAGE",    "cach dung,huong dan,su dung,dung sao,xai sao,cach su dung,dung the nao,dung nhu the nao", 40),
    ("ASK_RETURN",   "doi tra,bao hanh,tra hang,loi thi sao,bao hanh sao,doi hang,hoan tra", 30),
]


def _seed_default_intents(con):
    """Tạo intent ASK_* mặc định nếu chưa có (idempotent, không ghi đè keyword/priority đã sửa)."""
    rows = {r["name"]: r["keywords"] for r in con.execute("SELECT name, keywords FROM intents")}
    for name, kws, pri in DEFAULT_INTENTS:
        if name not in rows:
            con.execute("INSERT INTO intents (name, keywords, trigger_mode, cooldown_sec, enabled, priority) "
                        "VALUES (?,?,?,?,1,?)", (name, kws, "play_now", 45, pri))
        else:
            # Sửa priority cho intent ASK_* đang ở mức mặc định 100 (do migration) về đúng spec.
            con.execute("UPDATE intents SET priority=? WHERE name=? AND priority=100", (pri, name))
            # Bổ sung keyword default còn THIẾU vào intent đã có (union, giữ keyword user thêm).
            cur = [k.strip() for k in (rows[name] or "").split(",") if k.strip()]
            curset = {k.lower() for k in cur}
            added = [k for k in kws.split(",") if k.strip() and k.strip().lower() not in curset]
            if added:
                con.execute("UPDATE intents SET keywords=? WHERE name=?",
                            (",".join(cur + added), name))


# ---------------------------------------------------------------- playlists (nhóm)

DEFAULT_PLAYLIST_NAME = "Mặc định"


def _ensure_default_playlist(con):
    """Đảm bảo có ≥1 playlist; gán mọi entry mồ côi (pl_id NULL) vào playlist đầu tiên.
    Trả id của playlist mặc định."""
    row = con.execute("SELECT id FROM playlists ORDER BY id LIMIT 1").fetchone()
    if row:
        did = row["id"]
    else:
        did = con.execute("INSERT INTO playlists (name) VALUES (?)",
                          (DEFAULT_PLAYLIST_NAME,)).lastrowid
    con.execute("UPDATE playlist SET pl_id=? WHERE pl_id IS NULL", (did,))
    return did


def _resolve_pl(con, pl_id):
    """pl_id rỗng/None -> playlist mặc định (đầu tiên)."""
    if pl_id:
        return int(pl_id)
    return _ensure_default_playlist(con)


def get_default_playlist_id():
    with _connect() as con:
        return _ensure_default_playlist(con)


_PLAYLIST_EDITABLE = {"name", "play_mode", "group_filter", "autoplay", "loop"}


def list_playlists():
    """Danh sách playlist kèm số video + chế độ phát + lọc nhóm + autoplay/loop."""
    with _connect() as con:
        rows = con.execute(
            """
            SELECT pl.id, pl.name, pl.play_mode, pl.group_filter, pl.autoplay, pl.loop,
                   (SELECT COUNT(*) FROM playlist p WHERE p.pl_id = pl.id) AS count
              FROM playlists pl
          ORDER BY pl.id
            """
        )
        return [_ns(r) for r in rows]


def add_playlist(name):
    name = (name or "").strip() or DEFAULT_PLAYLIST_NAME
    with _connect() as con:
        return con.execute("INSERT INTO playlists (name) VALUES (?)", (name,)).lastrowid


def rename_playlist(pl_id, name):
    name = (name or "").strip()
    if not name:
        return
    with _connect() as con:
        con.execute("UPDATE playlists SET name=? WHERE id=?", (name, int(pl_id)))


def update_playlist(pl_id, **fields):
    """Cập nhật thiết lập playlist: name, play_mode ('order'|'random'|'priority'), group_filter."""
    cols = {k: v for k, v in fields.items() if k in _PLAYLIST_EDITABLE}
    if not cols:
        return
    cols = {k: (v or None if k == "group_filter" else v) for k, v in cols.items()}
    setexpr = ",".join(f"{k}=?" for k in cols)
    with _connect() as con:
        con.execute(f"UPDATE playlists SET {setexpr} WHERE id=?", (*cols.values(), int(pl_id)))


def delete_playlist(pl_id):
    """Xóa playlist (kèm entry của nó qua CASCADE). Chặn xóa playlist cuối cùng -> trả False."""
    with _connect() as con:
        n = con.execute("SELECT COUNT(*) AS n FROM playlists").fetchone()["n"]
        if n <= 1:
            return False
        con.execute("DELETE FROM playlists WHERE id=?", (int(pl_id),))
        return True


def get_playlist_meta(pl_id):
    with _connect() as con:
        return _ns(con.execute("SELECT * FROM playlists WHERE id=?", (int(pl_id),)).fetchone())


# ---------------------------------------------------------------- products

def add_product(name, image_path=None, link=None):
    """Thêm sản phẩm — chỉ 3 trường: tên, ảnh, link."""
    with _connect() as con:
        cur = con.execute(
            "INSERT INTO products (name, image_path, link) VALUES (?,?,?)",
            (name, str(image_path) if image_path else None, link or None))
        return cur.lastrowid


def update_product(product_id, **fields):
    """Cập nhật động các cột hợp lệ của 1 sản phẩm."""
    cols = {k: v for k, v in fields.items() if k in _PRODUCT_EDITABLE}
    if not cols:
        return
    setexpr = ",".join(f"{k}=?" for k in cols)
    with _connect() as con:
        con.execute(f"UPDATE products SET {setexpr} WHERE id=?", (*cols.values(), product_id))


def find_product_by_item_id(item_id):
    """Tìm sản phẩm theo shopee_item_id (khóa ổn định từ live)."""
    if item_id in (None, ""):
        return None
    with _connect() as con:
        return _ns(con.execute("SELECT * FROM products WHERE shopee_item_id=?",
                               (str(item_id),)).fetchone())


def add_product_from_live(name, shopee_item_id, shop_id=None, image_path=None):
    """Tạo sản phẩm từ 1 item phiên live (tên + item_id + shop_id). Trả product_id."""
    pid = add_product(name, image_path, None)
    update_product(pid, shopee_item_id=str(shopee_item_id),
                   shop_id=str(shop_id) if shop_id else None)
    return pid


def get_or_create_product_from_item(item_id, shop_id=None, name=None, image_path=None):
    """Trả product theo item_id nếu có (cập nhật shop_id/tên nếu thiếu), không thì tạo mới. Trả product_id."""
    p = find_product_by_item_id(item_id)
    if p:
        patch = {}
        if shop_id and not getattr(p, "shop_id", None):
            patch["shop_id"] = str(shop_id)
        if patch:
            update_product(p.id, **patch)
        return p.id
    return add_product_from_live(name or f"SP {item_id}", item_id, shop_id, image_path)


def find_video_by_path(file_path):
    with _connect() as con:
        return _ns(con.execute("SELECT * FROM videos WHERE file_path=?", (str(file_path),)).fetchone())


def list_review_videos():
    """Video chưa gắn SP chắc chắn (cần người duyệt): match_status review|unmatched."""
    with _connect() as con:
        rows = con.execute(
            "SELECT v.*, p.name AS product_name FROM videos v "
            "LEFT JOIN products p ON p.id=v.product_id "
            "WHERE v.match_status IN ('review','unmatched') ORDER BY v.id DESC").fetchall()
    return [_ns(r) for r in rows]


def find_answer_by_path(file_path):
    with _connect() as con:
        return _ns(con.execute("SELECT * FROM answer_videos WHERE file_path=?",
                               (str(file_path),)).fetchone())


def list_review_answers():
    """Answer video chưa gắn SP chắc chắn (review|unmatched) — để khớp lại sau sync."""
    with _connect() as con:
        rows = con.execute("SELECT * FROM answer_videos WHERE match_status IN ('review','unmatched') "
                           "ORDER BY id DESC").fetchall()
    return [_ns(r) for r in rows]


def _match_intent_by_text(text):
    """Khớp 1 ĐOẠN TEXT (hậu tố tên file, vd 'hoi gia' / 'hoi khuyen mai') với intent qua KEYWORD,
    y như cách khớp comment: trả intent.name nếu CÓ keyword xuất hiện trọn vẹn trong text
    (ưu tiên intent priority cao — list_intents đã sort), ngược lại None.
    Dùng match_util (không import intent_matcher -> tránh vòng import)."""
    from match_util import normalize, _phrase_in
    toks = normalize(text).split()
    if not toks:
        return None
    for it in list_intents():                      # đã ORDER BY priority DESC
        for kw in (it.keywords or "").split(","):
            kw = normalize(kw)
            if kw and _phrase_in(kw, toks):         # cụm keyword xuất hiện liền mạch trong hậu tố
                return it.name
    return None


def parse_import_name(stem):
    """Tách tên file '<sản-phẩm>__<INTENT>.mp4' -> (product_part, intent_code|None).
    Quy ước chuẩn dùng '__' ngăn cách: đoạn sau '__' là mã intent nếu (a) TRÙNG 1 intent đã có
    (không phân biệt hoa/thường) HOẶC (b) viết kiểu MÃ (CHỮ HOA, ≥3 ký tự, vd ASK_PRICE) — khi đó
    nếu intent chưa có sẽ được TỰ TẠO ở import_video_by_name. Nếu cả tên là 1 mã -> product rỗng."""
    codes = {i.name.upper() for i in list_intents()}
    s = (stem or "").strip()
    # bỏ đuôi video nếu tên đã lưu kèm phần mở rộng (tránh token 'mp4' phá khớp)
    low = s.lower()
    for ext in (".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"):
        if low.endswith(ext):
            s = s[: -len(ext)].strip()
            break
    # (1) quy ước '__': phần đuôi là mã intent nếu trùng intent đã có, hoặc viết kiểu CHỮ HOA
    if "__" in s:
        head, _, tail = s.rpartition("__")
        t = tail.strip()
        if t.upper() in codes or re.fullmatch(r"[A-Z][A-Z0-9_]{2,}", t):
            return head.strip(), t.upper()
        ic = _match_intent_by_text(t)                 # '__hoi gia' -> ASK_PRICE (theo keyword)
        if ic:
            return head.strip(), ic
    # (1b) mã intent ĐÃ CÓ ở cuối sau MỘT ngăn cách bất kỳ (_ / space / -), kể cả mã chứa '_'
    # như ASK_PRICE (rpartition '_' không tách được vì tách ở '_' cuối). Bỏ qua hoa/thường.
    up = s.upper()
    for code in sorted(codes, key=len, reverse=True):
        if up.endswith(code) and len(up) > len(code) and not up[-len(code) - 1].isalnum():
            return s[: len(s) - len(code)].rstrip(" _-").strip(), code
    # (1c) HẬU TỐ TỰ NHIÊN sau '_' -> khớp intent qua KEYWORD ('_hoi gia' -> ASK_PRICE,
    # '_hoi khuyen mai' -> ASK_VOUCHER). Chỉ '_' (không space/-) để khỏi cắt nhầm tên SP nhiều từ.
    if "_" in s:
        head, _, tail = s.rpartition("_")
        ic = _match_intent_by_text(tail)
        if ic:
            return head.strip().rstrip(" _-"), ic
    if s.upper() in codes:
        return "", s.upper()
    return s, None


def _resolve_product_part(part, thr_auto=0.85, thr_review=0.55, item_only=False):
    """Khớp 'phần sản phẩm' của tên -> (product_id|None, score, status, candidates_json).
    status ∈ item|auto|review|unmatched|shop. Hybrid: dãy ≥8 số khớp item_id -> khóa cứng;
    không thì fuzzy tên SP."""
    part = (part or "").strip()
    if not part:
        return None, 1.0, "shop", None     # answer chung shop (không gắn SP)
    m = re.search(r"\d{8,}", part)          # dãy số dài -> có thể là Shopee item_id
    if m:
        p = find_product_by_item_id(m.group(0))
        if p:
            return p.id, 1.0, "item", None
    if item_only:
        return None, 0.0, "unmatched", None
    # RULE: SO CẢ CỤM — TÊN VIDEO phải xuất hiện như 1 ĐOẠN LIỀN MẠCH, ĐÚNG THỨ TỰ trong TÊN SP
    # (không so lẻ từng chữ rải rác -> tránh khớp nhầm 'mua'≈'rua', từ rời rạc khắp tên).
    # VD 'dia nhua lua mach' nằm liền trong "Đĩa Nhựa Lúa Mạch Đựng..." -> khớp.
    vseq = _token_list(part)
    if not vseq:
        return None, 0.0, "unmatched", None
    prods = get_all_products()
    matches = [p for p in prods if _seq_contains(vseq, _token_list(p.name))]
    if len(matches) == 1:                              # đúng 1 SP chứa CỤM -> khớp
        return matches[0].id, 1.0, "auto", None
    if len(matches) > 1:                               # nhiều SP chứa cụm -> khách tự map lại
        cand = json.dumps([{"product_id": p.id, "name": p.name} for p in matches[:6]],
                          ensure_ascii=False)
        return None, 0.0, "review", cand
    # 0 SP chứa trọn cụm -> gợi ý theo ĐOẠN LIỀN dài nhất (tỉ lệ phủ tên video) để duyệt tay
    scored = sorted(((p, _seq_run(vseq, _token_list(p.name)) / len(vseq)) for p in prods),
                    key=lambda x: -x[1])
    if scored and scored[0][1] >= thr_review:
        cand = json.dumps([{"product_id": p.id, "name": p.name, "score": round(s, 2)}
                           for p, s in scored[:3] if s > 0], ensure_ascii=False)
        return None, round(scored[0][1], 3), "review", cand
    return None, (round(scored[0][1], 3) if scored else 0.0), "unmatched", None


def resolve_product_for_video(file_path, manifest=None, thr_auto=0.85, thr_review=0.55, name_hint=None):
    """Tương thích cũ: resolve product cho 1 video (không tách intent). Ưu tiên manifest item_id,
    rồi item_id/fuzzy trong tên. name_hint = tên hiển thị đã lưu (sạch hậu tố chống trùng).
    Trả (product_id|None, score, status, candidates_json)."""
    if manifest and manifest.get("product_item_id"):
        p = find_product_by_item_id(manifest["product_item_id"])
        if p:
            return p.id, 1.0, "manifest", None
        pid = get_or_create_product_from_item(manifest["product_item_id"],
                                              manifest.get("shop_id"), manifest.get("product_name"))
        return pid, 1.0, "manifest", None
    part, _intent = parse_import_name(name_hint or Path(str(file_path)).stem)
    return _resolve_product_part(part, thr_auto, thr_review)


def import_video_by_name(file_path, duration=0, thr_auto=None, name_hint=None):
    """Nhập 1 video theo TÊN (idempotent theo file_path):
    parse '<sp>__<INTENT>' -> resolve product (item_id/fuzzy) -> answer_videos (nếu có intent)
    hoặc videos (giới thiệu). name_hint = tên gốc để parse (tránh dính hậu tố chống trùng trên đĩa).
    Trả dict {kind, product_id, intent, status, skipped}."""
    fp = str(file_path)
    if find_video_by_path(fp) or find_answer_by_path(fp):
        return {"skipped": True}
    thr = thr_auto if thr_auto is not None else float(get_setting("ai_threshold", 0.85))
    name = (name_hint or Path(fp).stem)
    part, intent_code = parse_import_name(name)
    pid, score, status, cand = _resolve_product_part(part, thr_auto=thr)
    if intent_code:
        it = get_intent_by_name(intent_code)
        if not it:
            # mã intent hợp lệ (quy ước __CODE) nhưng chưa có -> TỰ TẠO để video kịch bản nổi lên tab AI
            it = get_intent(add_intent(name=intent_code))
        if it:
            avid = add_answer_video(it.id, fp, name, duration, pid)
            update_answer_video(avid, match_status=status, match_score=score, match_candidates=cand)
            return {"kind": "answer", "intent": it.name, "product_id": pid, "status": status}
        # không tạo được intent -> coi như video thường
    add_video(fp, pid, duration, name=name, match_status=status, match_score=score, match_candidates=cand)
    return {"kind": "video", "product_id": pid, "status": status}


def get_all_products():
    with _connect() as con:
        return [_ns(r) for r in con.execute("SELECT * FROM products ORDER BY id")]


def get_product(product_id):
    with _connect() as con:
        return _ns(con.execute("SELECT * FROM products WHERE id=?", (product_id,)).fetchone())


def find_product(sku=None, name=None):
    """Tìm product theo name (products đã bỏ sku). Dùng khi link video import."""
    if name:
        with _connect() as con:
            r = con.execute("SELECT * FROM products WHERE name=?", (str(name).strip(),)).fetchone()
            if r:
                return _ns(r)
    return None


def delete_product(product_id):
    with _connect() as con:
        con.execute("DELETE FROM products WHERE id=?", (product_id,))


# ---------------------------------------------------------------- videos

def add_video(file_path, product_id=None, duration=0, **extra):
    """Thêm video. extra: name, group_name, priority, play_limit, is_error."""
    fields = {"file_path": str(file_path), "product_id": product_id,
              "duration": float(duration or 0)}
    fields.setdefault("name", Path(str(file_path)).name)
    for k, v in extra.items():
        if k in _VIDEO_EDITABLE:
            fields[k] = v
    cols = ",".join(fields)
    qs = ",".join("?" * len(fields))
    with _connect() as con:
        cur = con.execute(f"INSERT INTO videos ({cols}) VALUES ({qs})", tuple(fields.values()))
        return cur.lastrowid


def update_video(video_id, **fields):
    cols = {k: v for k, v in fields.items() if k in _VIDEO_EDITABLE}
    if not cols:
        return
    setexpr = ",".join(f"{k}=?" for k in cols)
    with _connect() as con:
        con.execute(f"UPDATE videos SET {setexpr} WHERE id=?", (*cols.values(), video_id))


def bulk_add_videos(rows):
    """Import video từ CSV. Mỗi dòng: product = item_id (chính xác) > tên SP (fuzzy) > parse tên file;
    intent (cột Intent hoặc __INTENT trong tên) -> answer_videos. Idempotent theo file_path.
    Trả dict {added, linked, answer, review, unmatched, errors}."""
    rep = {"added": 0, "linked": 0, "answer": 0, "review": 0, "unmatched": 0, "errors": 0}
    for row in rows:
        try:
            fp = (row.get("file_path") or row.get("name") or "").strip()
            if not fp:
                rep["errors"] += 1
                continue
            if find_video_by_path(fp) or find_answer_by_path(fp):
                continue   # idempotent
            dur = row.get("duration") or 0
            name = row.get("name") or Path(fp).stem
            intent_code = (str(row.get("intent_code") or "").strip().upper()) or None
            pid, score, status, cand = None, 0.0, "unmatched", None
            if row.get("item_id"):
                p = find_product_by_item_id(str(row["item_id"]).strip())
                if p:
                    pid, score, status = p.id, 1.0, "item"
            if pid is None and row.get("product_name"):
                pid, score, status, cand = _resolve_product_part(str(row["product_name"]))
            # không có cột product nào -> suy từ tên file (kèm tách __INTENT)
            if pid is None and not row.get("item_id") and not row.get("product_name"):
                part, ic = parse_import_name(Path(fp).stem)
                intent_code = intent_code or ic
                pid, score, status, cand = _resolve_product_part(part)
            extra = {k: row[k] for k in ("group_name", "category", "priority", "play_limit")
                     if row.get(k) not in (None, "")}
            if intent_code:
                it = get_intent_by_name(intent_code)
                if it:
                    avid = add_answer_video(it.id, fp, name, dur, pid)
                    update_answer_video(avid, match_status=status, match_score=score, match_candidates=cand)
                    rep["added"] += 1
                    rep["answer"] += 1
                    if pid:
                        rep["linked"] += 1
                    continue
            add_video(fp, pid, dur, name=name, match_status=status, match_score=score,
                      match_candidates=cand, **extra)
            rep["added"] += 1
            if pid:
                rep["linked"] += 1
            if status == "review":
                rep["review"] += 1
            elif status == "unmatched":
                rep["unmatched"] += 1
        except Exception:
            rep["errors"] += 1
    return rep


def mark_video_error(video_id, flag=True):
    with _connect() as con:
        con.execute("UPDATE videos SET is_error=? WHERE id=?", (1 if flag else 0, video_id))


def delete_video(video_id):
    with _connect() as con:
        con.execute("DELETE FROM videos WHERE id=?", (video_id,))


def get_all_videos():
    with _connect() as con:
        rows = con.execute(
            """
            SELECT v.*, pr.name AS product_name
              FROM videos v LEFT JOIN products pr ON pr.id = v.product_id
          ORDER BY v.id
            """
        )
        return [_ns(r) for r in rows]


def get_video(video_id):
    with _connect() as con:
        return _ns(con.execute("SELECT * FROM videos WHERE id=?", (video_id,)).fetchone())


# ---------------------------------------------------------------- playlist

def set_playlist(video_ids, pl_id=None):
    """Thay toàn bộ 1 playlist bằng danh sách video_ids (theo đúng thứ tự), reset is_played."""
    with _connect() as con:
        pid = _resolve_pl(con, pl_id)
        con.execute("DELETE FROM playlist WHERE pl_id=?", (pid,))
        con.executemany(
            "INSERT INTO playlist (video_id, pl_id, order_index, is_played) VALUES (?,?,?,0)",
            [(vid, pid, idx) for idx, vid in enumerate(video_ids)],
        )


def add_to_playlist(video_id, pl_id=None):
    """Thêm 1 video vào cuối 1 playlist. Chặn 2 video giống nhau liên tiếp → trả False."""
    with _connect() as con:
        pid = _resolve_pl(con, pl_id)
        last = con.execute(
            "SELECT video_id FROM playlist WHERE pl_id=? ORDER BY order_index DESC, id DESC LIMIT 1",
            (pid,)).fetchone()
        if last and last["video_id"] == int(video_id):
            return False
        nxt = con.execute(
            "SELECT COALESCE(MAX(order_index)+1,0) AS n FROM playlist WHERE pl_id=?", (pid,)
        ).fetchone()["n"]
        con.execute(
            "INSERT INTO playlist (video_id, pl_id, order_index, is_played) VALUES (?,?,?,0)",
            (video_id, pid, nxt),
        )
        return True


def get_playlist(pl_id=None):
    """Trả 1 playlist kèm thông tin video + product để hiển thị UI (theo order_index)."""
    with _connect() as con:
        pid = _resolve_pl(con, pl_id)
        rows = con.execute(
            """
            SELECT p.id AS playlist_id, p.order_index, p.is_played,
                   v.id AS video_id, v.file_path, v.duration,
                   pr.id AS product_id, pr.name AS product_name
              FROM playlist p
              JOIN videos v   ON v.id = p.video_id
         LEFT JOIN products pr ON pr.id = v.product_id
             WHERE p.pl_id = ?
          ORDER BY p.order_index, p.id
            """,
            (pid,),
        )
        return [_ns(r) for r in rows]


_ORDER_BY = {
    "order":      "p.order_index, p.id",
    "priority":   "v.priority DESC, p.order_index, p.id",   # ưu tiên cao phát trước
    "random":     "RANDOM()",                                # ngẫu nhiên trong số chưa phát
    "commission": "pr.commission DESC, p.order_index, p.id",  # hoa hồng cao phát trước
    "sale":       "(pr.sale_price IS NOT NULL AND pr.sale_price>0) DESC, p.order_index, p.id",  # SP sale trước
}


def get_next_unplayed_video(pl_id=None, exclude_ids=None):
    """Video chưa phát kế tiếp trong 1 playlist. Thứ tự theo play_mode của playlist
    (order/priority/random/commission/sale), lọc group_filter (nhóm SP) nếu có,
    bỏ qua exclude_ids (vd video đã hết giới hạn phát/phiên)."""
    with _connect() as con:
        pid = _resolve_pl(con, pl_id)
        meta = con.execute("SELECT play_mode, group_filter FROM playlists WHERE id=?", (pid,)).fetchone()
        mode = (meta["play_mode"] if meta else "order") or "order"
        group = meta["group_filter"] if meta else None
        order_by = _ORDER_BY.get(mode, _ORDER_BY["order"])
        sql = (
            "SELECT v.* FROM playlist p "
            "  JOIN videos v ON v.id = p.video_id "
            " LEFT JOIN products pr ON pr.id = v.product_id "
            " WHERE p.pl_id = ? AND p.is_played = 0 AND v.is_error = 0"
        )
        params = [pid]
        if group:
            sql += " AND v.group_name = ?"
            params.append(group)
        if exclude_ids:
            sql += " AND v.id NOT IN (%s)" % ",".join("?" * len(exclude_ids))
            params.extend(int(x) for x in exclude_ids)
        sql += f" ORDER BY {order_by} LIMIT 1"
        return _ns(con.execute(sql, tuple(params)).fetchone())


def get_product_groups():
    """Danh sách nhóm (group_name của video) khác rỗng — cho dropdown lọc nhóm playlist."""
    with _connect() as con:
        rows = con.execute(
            "SELECT DISTINCT group_name FROM videos "
            "WHERE group_name IS NOT NULL AND TRIM(group_name) <> '' ORDER BY group_name")
        return [r["group_name"] for r in rows]


def mark_played(video_id, pl_id=None):
    """Đánh dấu entry của video_id trong 1 playlist là đã phát + tăng play_count."""
    with _connect() as con:
        pid = _resolve_pl(con, pl_id)
        con.execute("UPDATE playlist SET is_played=1 WHERE video_id=? AND pl_id=?", (video_id, pid))
        con.execute("UPDATE videos SET play_count=play_count+1 WHERE id=?", (video_id,))


def reset_all_played(pl_id=None):
    """Đặt lại 1 playlist về chưa phát (để loop lại)."""
    with _connect() as con:
        pid = _resolve_pl(con, pl_id)
        con.execute("UPDATE playlist SET is_played=0 WHERE pl_id=?", (pid,))


def reset_last_played(pl_id=None):
    """Đặt lại entry đã phát gần nhất trong 1 playlist về chưa phát — cho nút Previous."""
    with _connect() as con:
        pid = _resolve_pl(con, pl_id)
        con.execute(
            "UPDATE playlist SET is_played=0 WHERE id=("
            "SELECT id FROM playlist WHERE pl_id=? AND is_played=1 "
            "ORDER BY order_index DESC, id DESC LIMIT 1)",
            (pid,),
        )


def remove_from_playlist(playlist_id):
    with _connect() as con:
        con.execute("DELETE FROM playlist WHERE id=?", (playlist_id,))


def clear_playlist(pl_id=None):
    """Xóa sạch 1 playlist (nút 'Xóa tất cả')."""
    with _connect() as con:
        pid = _resolve_pl(con, pl_id)
        con.execute("DELETE FROM playlist WHERE pl_id=?", (pid,))


def reorder_playlist(ordered_playlist_ids):
    """Sắp xếp lại playlist theo thứ tự playlist_id cho trước (giữ nguyên is_played)."""
    with _connect() as con:
        for idx, plid in enumerate(ordered_playlist_ids):
            con.execute("UPDATE playlist SET order_index=? WHERE id=?", (idx, plid))


# ---------------------------------------------------------------- sessions (P2)

_SESSION_EDITABLE = {"name", "platform", "rtmp_server", "stream_key", "scene", "start_at",
                     "end_at", "auto_start", "auto_stop", "auto_recover", "pl_id", "profile",
                     "status", "error"}


def add_session(name, **fields):
    fields = {k: v for k, v in fields.items() if k in _SESSION_EDITABLE}
    fields["name"] = name
    cols = ",".join(fields)
    qs = ",".join("?" * len(fields))
    with _connect() as con:
        cur = con.execute(f"INSERT INTO sessions ({cols}) VALUES ({qs})", tuple(fields.values()))
        return cur.lastrowid


def update_session(session_id, **fields):
    cols = {k: v for k, v in fields.items() if k in _SESSION_EDITABLE}
    if not cols:
        return
    setexpr = ",".join(f"{k}=?" for k in cols)
    with _connect() as con:
        con.execute(f"UPDATE sessions SET {setexpr} WHERE id=?", (*cols.values(), session_id))


def set_session_status(session_id, status, started_at=None, ended_at=None, error=None):
    sets, vals = ["status=?"], [status]
    if started_at is not None:
        sets.append("started_at=?"); vals.append(started_at)
    if ended_at is not None:
        sets.append("ended_at=?"); vals.append(ended_at)
    sets.append("error=?"); vals.append(error)
    vals.append(session_id)
    with _connect() as con:
        con.execute(f"UPDATE sessions SET {','.join(sets)} WHERE id=?", tuple(vals))


def delete_session(session_id):
    with _connect() as con:
        con.execute("DELETE FROM sessions WHERE id=?", (session_id,))


def get_session(session_id):
    with _connect() as con:
        return _ns(con.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone())


def list_sessions(limit=200):
    with _connect() as con:
        return [_ns(r) for r in con.execute(
            "SELECT * FROM sessions ORDER BY COALESCE(start_at,created_at) DESC, id DESC LIMIT ?",
            (limit,))]


def get_active_session():
    with _connect() as con:
        return _ns(con.execute("SELECT * FROM sessions WHERE status='live' ORDER BY id LIMIT 1").fetchone())


def get_next_scheduled_session():
    with _connect() as con:
        return _ns(con.execute(
            "SELECT * FROM sessions WHERE status='scheduled' ORDER BY start_at, id LIMIT 1").fetchone())


def get_due_to_start(now):
    """Phiên scheduled tới giờ bắt đầu (start_at<=now<end_at), auto_start=1."""
    with _connect() as con:
        return [_ns(r) for r in con.execute(
            "SELECT * FROM sessions WHERE status='scheduled' AND auto_start=1 "
            "AND start_at IS NOT NULL AND start_at<=? AND (end_at IS NULL OR end_at>?) "
            "ORDER BY start_at, id", (now, now))]


def get_due_to_stop(now):
    """Phiên live đã quá end_at, auto_stop=1."""
    with _connect() as con:
        return [_ns(r) for r in con.execute(
            "SELECT * FROM sessions WHERE status='live' AND auto_stop=1 "
            "AND end_at IS NOT NULL AND end_at<=? ORDER BY id", (now,))]


def get_sessions_by_status(status):
    with _connect() as con:
        return [_ns(r) for r in con.execute("SELECT * FROM sessions WHERE status=? ORDER BY id", (status,))]


def get_expired_scheduled(now):
    """Phiên scheduled đã quá giờ (end_at<=now) mà chưa từng lên live."""
    with _connect() as con:
        return [_ns(r) for r in con.execute(
            "SELECT * FROM sessions WHERE status='scheduled' AND end_at IS NOT NULL AND end_at<=?",
            (now,))]


# ---------------------------------------------------------------- intents (Phần B)

_INTENT_EDITABLE = {"name", "keywords", "trigger_mode", "cooldown_sec", "enabled", "priority"}


def add_intent(name, keywords=None, trigger_mode="enqueue", cooldown_sec=30, enabled=1):
    with _connect() as con:
        cur = con.execute(
            "INSERT INTO intents (name, keywords, trigger_mode, cooldown_sec, enabled) VALUES (?,?,?,?,?)",
            (name, keywords, trigger_mode, int(cooldown_sec or 0), int(bool(enabled))))
        return cur.lastrowid


def update_intent(intent_id, **fields):
    cols = {k: v for k, v in fields.items() if k in _INTENT_EDITABLE}
    if not cols:
        return
    setexpr = ",".join(f"{k}=?" for k in cols)
    with _connect() as con:
        con.execute(f"UPDATE intents SET {setexpr} WHERE id=?", (*cols.values(), intent_id))


def delete_intent(intent_id):
    with _connect() as con:
        con.execute("DELETE FROM intents WHERE id=?", (intent_id,))


def get_intent(intent_id):
    with _connect() as con:
        return _ns(con.execute("SELECT * FROM intents WHERE id=?", (intent_id,)).fetchone())


def list_intents():
    with _connect() as con:
        return [_ns(r) for r in con.execute(
            "SELECT * FROM intents ORDER BY priority DESC, id")]


def get_intent_by_name(name):
    """Khớp tên intent KHÔNG phân biệt hoa/thường (đồng bộ với parse_import_name dùng .upper(),
    bao gồm cả tên tiếng Việt — SQLite COLLATE NOCASE không lo được unicode nên so trong Python)."""
    key = str(name).strip().upper()
    with _connect() as con:
        rows = con.execute("SELECT * FROM intents").fetchall()
    for r in rows:
        if (r["name"] or "").strip().upper() == key:
            return _ns(r)
    return None


# ---------------------------------------------------------------- answer_videos

def add_answer_video(intent_id, file_path, name=None, duration=0, product_id=None):
    with _connect() as con:
        cur = con.execute(
            "INSERT INTO answer_videos (intent_id, file_path, name, duration, product_id) "
            "VALUES (?,?,?,?,?)",
            (intent_id, str(file_path), name or Path(str(file_path)).name, float(duration or 0), product_id))
        return cur.lastrowid


def update_answer_video(av_id, **fields):
    allowed = {"intent_id", "name", "file_path", "duration", "product_id", "enabled"} | _MATCH_EDITABLE
    cols = {k: v for k, v in fields.items() if k in allowed}
    if not cols:
        return
    setexpr = ",".join(f"{k}=?" for k in cols)
    with _connect() as con:
        con.execute(f"UPDATE answer_videos SET {setexpr} WHERE id=?", (*cols.values(), av_id))


def delete_answer_video(av_id):
    with _connect() as con:
        con.execute("DELETE FROM answer_videos WHERE id=?", (av_id,))


def delete_videos_by_path(file_path):
    """Xóa mọi dòng videos theo file_path (dọn chéo khi xóa 1 video -> mất ở cả thư viện)."""
    with _connect() as con:
        con.execute("DELETE FROM videos WHERE file_path=?", (str(file_path),))


def delete_answers_by_path(file_path):
    """Xóa mọi dòng answer_videos theo file_path (dọn chéo -> mất luôn trong Kịch bản AI)."""
    with _connect() as con:
        con.execute("DELETE FROM answer_videos WHERE file_path=?", (str(file_path),))


def get_answer_video(av_id):
    with _connect() as con:
        r = con.execute(
            "SELECT av.*, pr.name AS product_name FROM answer_videos av "
            "LEFT JOIN products pr ON pr.id=av.product_id WHERE av.id=?", (av_id,)).fetchone()
        return _ns(r)


def list_answer_videos(intent_id=None):
    with _connect() as con:
        q = ("SELECT av.*, i.name AS intent_name, pr.name AS product_name FROM answer_videos av "
             "LEFT JOIN intents i ON i.id=av.intent_id "
             "LEFT JOIN products pr ON pr.id=av.product_id")
        args = ()
        if intent_id is not None:
            q += " WHERE av.intent_id=?"; args = (intent_id,)
        q += " ORDER BY av.id"
        return [_ns(r) for r in con.execute(q, args)]


def pick_answer_for_intent(intent_id):
    """Round-robin: chọn video seq nhỏ nhất (ít chọn gần đây nhất) RỒI xoay ngay (tăng seq)
    để lần chọn kế tiếp ra video khác — kể cả khi chưa kịp phát. Không phụ thuộc thời gian."""
    with _connect() as con:
        r = con.execute(
            "SELECT * FROM answer_videos WHERE intent_id=? AND enabled=1 "
            "ORDER BY last_played_seq ASC, id ASC LIMIT 1",
            (intent_id,)).fetchone()
        if not r:
            return None
        nxt = con.execute("SELECT COALESCE(MAX(last_played_seq),0)+1 AS n FROM answer_videos").fetchone()["n"]
        con.execute("UPDATE answer_videos SET last_played_seq=? WHERE id=?", (nxt, r["id"]))
        return _ns(r)


def pick_answer(intent_id, product_id=None):
    """Chọn video trả lời cho (intent [+ product]). Ưu tiên video gắn ĐÚNG product, nếu không có
    thì fallback video chung của intent (product_id IS NULL). Round-robin theo last_played_seq.
    Trả (answer_video | None, scope) với scope ∈ 'product' | 'general'."""
    def _pick(where, args):
        with _connect() as con:
            r = con.execute(
                "SELECT * FROM answer_videos WHERE intent_id=? AND enabled=1 " + where +
                " ORDER BY priority DESC, last_played_seq ASC, id ASC LIMIT 1", (intent_id, *args)).fetchone()
            if not r:
                return None
            nxt = con.execute("SELECT COALESCE(MAX(last_played_seq),0)+1 AS n FROM answer_videos").fetchone()["n"]
            con.execute("UPDATE answer_videos SET last_played_seq=? WHERE id=?", (nxt, r["id"]))
            return _ns(r)
    if product_id is not None:
        r = _pick("AND product_id=?", (int(product_id),))
        if r:
            return r, "product"
    r = _pick("AND product_id IS NULL", ())
    return (r, "general") if r else (None, None)


def mark_answer_played(av_id):
    """Tăng play_count + ghi thời điểm phát (KHÔNG đụng seq — rotation do pick_answer lo)."""
    with _connect() as con:
        con.execute("UPDATE answer_videos SET play_count=play_count+1, "
                    "last_played_at=datetime('now','localtime') WHERE id=?", (av_id,))


# ---------------------------------------------------------------- settings (key-value JSON)

def get_setting(key, default=None):
    with _connect() as con:
        r = con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    if not r:
        return default
    try:
        return json.loads(r["value"])
    except Exception:
        return default


def set_setting(key, value):
    with _connect() as con:
        con.execute(
            "INSERT INTO settings(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value)))


# ---------------------------------------------------------------- shopee cookies (1 dòng / quốc gia)

def save_shopee_cookie(code, cookie, domain=None, user_agent=None):
    """Lưu/ghi đè cookie phiên Shopee theo mã quốc gia (VN/MY/...). Trả về dict đã lưu."""
    code = (code or "").strip().upper()
    if not code or not cookie:
        raise ValueError("cần code và cookie")
    with _connect() as con:
        con.execute(
            "INSERT INTO shopee_cookies(code,domain,cookie,user_agent,updated_at) "
            "VALUES(?,?,?,?,datetime('now','localtime')) "
            "ON CONFLICT(code) DO UPDATE SET domain=excluded.domain, cookie=excluded.cookie, "
            "user_agent=COALESCE(excluded.user_agent, shopee_cookies.user_agent), "
            "updated_at=excluded.updated_at",
            (code, domain, cookie, user_agent))
        r = con.execute("SELECT * FROM shopee_cookies WHERE code=?", (code,)).fetchone()
    return dict(r) if r else None


def get_shopee_cookie(code):
    with _connect() as con:
        r = con.execute("SELECT * FROM shopee_cookies WHERE code=?",
                        ((code or "").strip().upper(),)).fetchone()
    return dict(r) if r else None


def list_shopee_cookies():
    """Danh sách cookie đã lưu (KHÔNG kèm chuỗi cookie đầy đủ — chỉ metadata + độ dài)."""
    with _connect() as con:
        rows = con.execute("SELECT code, domain, user_agent, updated_at, "
                           "length(cookie) AS cookie_len FROM shopee_cookies "
                           "ORDER BY updated_at DESC").fetchall()
    return [dict(r) for r in rows]


def delete_shopee_cookie(code):
    with _connect() as con:
        con.execute("DELETE FROM shopee_cookies WHERE code=?", ((code or "").strip().upper(),))


# ---------------------------------------------------------------- product triggers (keyword -> SP)

def add_trigger(product_id, keyword):
    kw = (keyword or "").strip()
    if not kw:
        raise ValueError("keyword rỗng")
    with _connect() as con:
        con.execute("INSERT INTO product_triggers(product_id, keyword) VALUES(?,?)",
                    (int(product_id), kw))


def delete_trigger(trigger_id):
    with _connect() as con:
        con.execute("DELETE FROM product_triggers WHERE id=?", (int(trigger_id),))


def get_all_triggers():
    """Tất cả trigger (kèm tên SP) — cho keyword matcher + UI."""
    with _connect() as con:
        rows = con.execute(
            "SELECT t.id, t.product_id, t.keyword, p.name AS product_name "
            "FROM product_triggers t JOIN products p ON p.id=t.product_id "
            "ORDER BY t.product_id, t.id").fetchall()
    return [_ns(r) for r in rows]


def get_triggers_by_product(product_id):
    with _connect() as con:
        rows = con.execute("SELECT * FROM product_triggers WHERE product_id=? ORDER BY id",
                           (int(product_id),)).fetchall()
    return [_ns(r) for r in rows]


# ---------------------------------------------------------------- comment logs

def log_comment(comment_id=None, user_id="", content="", matched_product_id=None,
                confidence=0, match_method="no_match", triggered=False):
    with _connect() as con:
        con.execute(
            "INSERT INTO comment_logs(comment_id,user_id,content,matched_product_id,"
            "confidence,match_method,triggered) VALUES(?,?,?,?,?,?,?)",
            (str(comment_id) if comment_id is not None else None, user_id or "", content or "",
             matched_product_id, float(confidence or 0), match_method, int(bool(triggered))))


def list_comment_logs(limit=200):
    with _connect() as con:
        rows = con.execute(
            "SELECT c.*, p.name AS product_name FROM comment_logs c "
            "LEFT JOIN products p ON p.id=c.matched_product_id "
            "ORDER BY c.id DESC LIMIT ?", (int(limit),)).fetchall()
    return [_ns(r) for r in rows]


def get_video_for_product(product_id, pick="rotate"):
    """Chọn 1 video gắn SP để trigger phát (bỏ video lỗi).
    pick='rotate' (mặc định): xoay vòng — video LÂU chưa phát nhất (NULL = chưa phát bao giờ).
    pick='random': ngẫu nhiên.  Cả hai tránh lặp lại 1 video khi SP có nhiều video."""
    with _connect() as con:
        if pick == "random":
            order = "RANDOM()"
        else:  # rotate: chưa phát (NULL) trước, rồi tới cái phát lâu nhất
            order = "last_played_at IS NULL DESC, last_played_at ASC, id ASC"
        r = con.execute(
            f"SELECT * FROM videos WHERE product_id=? AND COALESCE(is_error,0)=0 "
            f"ORDER BY {order} LIMIT 1", (int(product_id),)).fetchone()
    return _ns(r) if r else None


def get_intro_video(product_id, pick="rotate"):
    """Video giới thiệu SP: ưu tiên products.intro_video_id (gán tay), nếu không có thì
    chọn trong các video gắn product_id (xoay vòng/random)."""
    p = get_product(product_id)
    iv = getattr(p, "intro_video_id", None) if p else None
    if iv:
        v = get_video(int(iv))
        if v:
            return v
    return get_video_for_product(product_id, pick=pick)


def list_videos_for_product(product_id):
    with _connect() as con:
        rows = con.execute("SELECT * FROM videos WHERE product_id=? AND COALESCE(is_error,0)=0 "
                           "ORDER BY id", (int(product_id),)).fetchall()
    return [_ns(r) for r in rows]


def mark_video_triggered(video_id):
    """Đánh dấu video vừa được trigger phát (cập nhật last_played_at + tăng play_count) — để xoay vòng."""
    with _connect() as con:
        con.execute("UPDATE videos SET last_played_at=strftime('%Y-%m-%d %H:%M:%f','now','localtime'), "
                    "play_count=play_count+1 WHERE id=?", (int(video_id),))


# ---------------------------------------------------------------- video chọn theo rule

def get_videos_by_category(category):
    """Video theo category (vd 'follow'/'voucher') — bỏ video lỗi. Cho rule chèn tự động."""
    if not category:
        return []
    with _connect() as con:
        rows = con.execute(
            "SELECT * FROM videos WHERE category=? AND is_error=0 ORDER BY id", (str(category),))
        return [_ns(r) for r in rows]


def add_scene_asset(kind, file_path, name=None):
    with _connect() as con:
        cur = con.execute(
            "INSERT INTO scene_assets (kind, name, file_path) VALUES (?,?,?)",
            (kind, name or Path(str(file_path)).name, str(file_path)))
        return cur.lastrowid


def list_scene_assets(kind=None):
    with _connect() as con:
        if kind:
            rows = con.execute("SELECT * FROM scene_assets WHERE kind=? ORDER BY id", (kind,))
        else:
            rows = con.execute("SELECT * FROM scene_assets ORDER BY kind, id")
        return [_ns(r) for r in rows]


def get_scene_asset(asset_id):
    with _connect() as con:
        return _ns(con.execute("SELECT * FROM scene_assets WHERE id=?", (int(asset_id),)).fetchone())


def toggle_scene_asset(asset_id, enabled):
    with _connect() as con:
        con.execute("UPDATE scene_assets SET enabled=? WHERE id=?", (1 if enabled else 0, int(asset_id)))


def delete_scene_asset(asset_id):
    with _connect() as con:
        con.execute("DELETE FROM scene_assets WHERE id=?", (int(asset_id),))


def get_top_commission_videos(n=3):
    """Top video theo hoa hồng sản phẩm (cho rule 'phát lại top sau X phút')."""
    with _connect() as con:
        rows = con.execute(
            "SELECT v.* FROM videos v JOIN products pr ON pr.id=v.product_id "
            "WHERE v.is_error=0 ORDER BY pr.commission DESC, v.id LIMIT ?", (int(n or 3),))
        return [_ns(r) for r in rows]


if __name__ == "__main__":
    init_db()
    print(f"Initialized {DB_PATH}")
