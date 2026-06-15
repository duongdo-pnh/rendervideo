"""Helper khớp chuỗi/tên DÙNG CHUNG — THUẦN, không import module app nào (tránh vòng import
giữa live_database ↔ shopee_api ↔ intent_matcher). Chứa: chuẩn hóa bỏ dấu, fuzzy token, độ giống tên.
"""
import re
import unicodedata
from difflib import SequenceMatcher


def normalize(s):
    """lower + bỏ dấu tiếng Việt + đ→d + gom khoảng trắng."""
    s = (s or "").lower().strip()
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")  # bỏ dấu thanh/mũ/móc
    s = s.replace("đ", "d")
    return re.sub(r"\s+", " ", s)


def _tokens(s):
    # [^\W_] = ký tự chữ/số nhưng KHÔNG gồm '_' -> tách 'bat_che_mua' thành {bat,che,mua}
    return set(re.findall(r"[^\W_]+", normalize(s), re.UNICODE))


def _edit_dist1(a, b):
    """True nếu a,b giống nhau hoặc lệch đúng 1 thao tác (thêm/bớt/đổi 1 ký tự)."""
    if a == b:
        return True
    la, lb = len(a), len(b)
    if abs(la - lb) > 1:
        return False
    if la == lb:
        return sum(x != y for x, y in zip(a, b)) == 1
    if la > lb:
        a, b, la, lb = b, a, lb, la
    i = j = 0
    skipped = False
    while i < la and j < lb:
        if a[i] != b[j]:
            if skipped:
                return False
            skipped = True
            j += 1
        else:
            i += 1
            j += 1
    return True


def _phrase_in(phrase, ctok_list):
    """Cụm keyword (đã chuẩn hóa) xuất hiện như CHUỖI TỪ NGUYÊN VẸN trong comment.
    Tránh khớp nhầm chuỗi con: 'co k' KHÔNG khớp trong 'co khuyen'."""
    pt = phrase.split()
    n = len(pt)
    if not n:
        return False
    for i in range(len(ctok_list) - n + 1):
        if ctok_list[i:i + n] == pt:
            return True
    return False


def _longest_token_run(a_list, b_list):
    """Độ dài đoạn TỪ liên tiếp dài nhất chung giữa 2 list (khớp mờ lệch 1 ký tự với từ ≥3)."""
    best = 0
    for i in range(len(a_list)):
        for j in range(len(b_list)):
            k = 0
            while i + k < len(a_list) and j + k < len(b_list):
                x, y = a_list[i + k], b_list[j + k]
                if x == y or (len(x) >= 3 and len(y) >= 3 and _edit_dist1(x, y)):
                    k += 1
                else:
                    break
            if k > best:
                best = k
    return best


def _token_list(s):
    """Như _tokens nhưng GIỮ THỨ TỰ (list) — để so cụm liền mạch."""
    return re.findall(r"[^\W_]+", normalize(s), re.UNICODE)


def _word_eq(a, b):
    """Bằng nhau, hoặc lệch ≤1 ký tự CHỈ khi cả 2 từ ≥5 ký tự (tránh mua≈rua, che≈chen)."""
    return a == b or (len(a) >= 5 and len(b) >= 5 and _edit_dist1(a, b))


def _seq_contains(needle, hay):
    """needle (list từ tên video) xuất hiện như 1 ĐOẠN LIỀN MẠCH, ĐÚNG THỨ TỰ trong hay (list từ tên SP).
    So CẢ CỤM, không so lẻ rải rác. VD ['dia','nhua','lua','mach'] nằm liền trong tên SP -> True."""
    n = len(needle)
    if n == 0 or n > len(hay):
        return False
    for i in range(len(hay) - n + 1):
        if all(_word_eq(needle[j], hay[i + j]) for j in range(n)):
            return True
    return False


def _seq_run(needle, hay):
    """Độ dài đoạn LIỀN MẠCH dài nhất của needle khớp trong hay (để chấm điểm gợi ý duyệt tay)."""
    best = 0
    for i in range(len(needle)):
        for j in range(len(hay)):
            k = 0
            while i + k < len(needle) and j + k < len(hay) and _word_eq(needle[i + k], hay[j + k]):
                k += 1
            best = max(best, k)
    return best


def _token_fuzzy_subset(name_toks, comment_toks):
    """Mọi token đều có 1 token khớp (bằng nhau, hoặc lệch 1 ký tự CHỈ khi dài ≥5).
    Token tiếng Việt 3-4 ký tự (mua/che/bat/mach/treo…) rất dễ lệch-1 trùng nhầm
    (mua≈rua, che≈chen) -> chỉ cho fuzzy với token ≥5 ký tự, ngắn hơn phải khớp ĐÚNG."""
    for nt in name_toks:
        ok = False
        for ct in comment_toks:
            if nt == ct or (len(nt) >= 5 and len(ct) >= 5 and _edit_dist1(nt, ct)):
                ok = True
                break
        if not ok:
            return False
    return True


def _name_sim(prod_name, item_name):
    """Độ giống tên SP local (ngắn) với tên SP live (dài), 0..1: max(containment, ratio)."""
    a, b = normalize(prod_name), normalize(item_name)
    if not a or not b:
        return 0.0
    at = [t for t in a.split() if t]
    bt = set(b.split())
    contain = 0.0
    if at:
        found = sum(1 for t in at
                    if any(t == u or (len(t) >= 3 and len(u) >= 3 and _edit_dist1(t, u)) for u in bt))
        contain = found / len(at)
    return max(contain, SequenceMatcher(None, a, b).ratio())
