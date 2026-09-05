// Mirrors job_naming.py exactly — the output filename ("<product>__<INTENT>" or
// "<product>") is what the live system uses to auto-match video<->product, so the
// client-side preview must compute the identical string the backend will use.

export const KIND_INTRO = 'Giới thiệu sản phẩm'
export const KIND_ANSWER = 'Trả lời câu hỏi'

export const INTENT_CHOICES: [string, string][] = [
  ['Hỏi giá', 'ASK_PRICE'],
  ['Hỏi chất lượng / review', 'ASK_QUALITY'],
  ['Hỏi cách dùng', 'ASK_USAGE'],
  ['Còn hàng / còn size-màu', 'ASK_STOCK'],
  ['Cách mua / chốt đơn', 'ASK_BUY'],
  ['Phí ship / giao hàng', 'ASK_SHIPPING'],
  ['Voucher / giảm giá', 'ASK_VOUCHER'],
  ['Đổi trả / bảo hành', 'ASK_RETURN'],
  ['Xem sản phẩm', 'ASK_PRODUCT'],
  ['Chất liệu / lớp / quai đeo', 'ASK_MATERIAL'],
  ['Size / form / cân nặng', 'ASK_SIZE_FIT'],
  ['Hạn sử dụng', 'ASK_EXPIRY'],
  ['Dành cho trẻ em', 'ASK_CHILDREN'],
  ['Màu sắc / mix màu', 'ASK_COLOR'],
  ['Kiểm hàng', 'ASK_CHECK'],
  ['Khác (other_key)', 'ASK_OTHER'],
]

function toAscii(s: string): string {
  return s
    .replace(/đ/g, 'd')
    .replace(/Đ/g, 'D')
    .normalize('NFKD')
    .replace(/[̀-ͯ]/g, '')
    .trim()
    .toLowerCase()
}

export function slugKey(value: string | null | undefined): string | null {
  let key = toAscii(value || '')
    .replace(/ /g, '_')
    .replace(/-/g, '_')
  key = key.replace(/[^a-z0-9_]+/g, '_')
  key = key.replace(/_+/g, '_').replace(/^_|_$/g, '')
  return key || null
}

function manualIntent(intent: string, otherKey?: string | null): string {
  if (intent === 'ASK_OTHER') {
    const key = slugKey(otherKey)
    return key ? `ASK_OTHER_${key}` : 'ASK_OTHER'
  }
  return intent
}

export function buildName(
  product: string,
  kind: string,
  intent: string | null,
  otherKey?: string | null,
): string {
  const p = (product || '').trim()
  if (kind === KIND_ANSWER && intent) {
    const resolved = manualIntent(intent, otherKey)
    return p ? `${p}__${resolved}` : `__${resolved}`
  }
  return p || 'video'
}
