interface HasBatch {
  batch_id?: string | null
  excel_file?: string | null
}

export interface ExcelFileGroup<T> {
  key: string
  label: string
  items: T[]
}

export const NO_SOURCE_KEY = '__no_source__'

/** Groups jobs by their originating Excel batch, preserving each item's relative
 * order and the order groups first appear in — items without a batch_id (created
 * manually, not via Excel import) land in one "no source" bucket at the end. */
export function groupByExcelFile<T extends HasBatch>(items: T[]): ExcelFileGroup<T>[] {
  const order: string[] = []
  const buckets = new Map<string, T[]>()
  for (const item of items) {
    const key = item.batch_id ?? NO_SOURCE_KEY
    if (!buckets.has(key)) {
      order.push(key)
      buckets.set(key, [])
    }
    buckets.get(key)!.push(item)
  }
  order.sort((a, b) => (a === NO_SOURCE_KEY ? 1 : b === NO_SOURCE_KEY ? -1 : 0))
  return order.map((key) => {
    const items = buckets.get(key)!
    const label = key === NO_SOURCE_KEY ? 'Tạo thủ công / không rõ nguồn' : items[0].excel_file || key
    return { key, label, items }
  })
}
