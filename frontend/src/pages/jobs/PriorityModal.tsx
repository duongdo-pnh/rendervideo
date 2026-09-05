import { useState } from 'react'
import { ListOrdered, X } from 'lucide-react'
import { Button } from '../../components/Button'
import type { PriorityChoice } from '../../types/api'

export function PriorityModal({
  choices,
  onClose,
  onApply,
}: {
  choices: PriorityChoice[]
  onClose: () => void
  onApply: (orderedIds: number[]) => void
}) {
  const [order, setOrder] = useState<number[]>([])

  const toggle = (id: number) => {
    setOrder((prev) => (prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id]))
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4">
      <div className="w-full max-w-2xl rounded-2xl bg-white dark:bg-slate-900 shadow-card border border-slate-200 dark:border-slate-800">
        <div className="flex items-start justify-between border-b border-slate-100 dark:border-slate-800 px-5 py-4">
          <div>
            <h2 className="flex items-center gap-1.5 text-sm font-semibold text-slate-800 dark:text-slate-100">
              <ListOrdered className="h-4 w-4 text-brand-600 dark:text-brand-400" /> Tạo danh sách ưu tiên
            </h2>
            <p className="mt-0.5 text-xs text-slate-500">
              Chọn job theo thứ tự muốn chạy — job chọn đầu tiên sẽ chạy trước.
            </p>
          </div>
          <button onClick={onClose} className="text-slate-400 hover:text-slate-600">
            <X className="h-4 w-4" />
          </button>
        </div>
        <div className="max-h-[50vh] overflow-y-auto p-5">
          <div className="flex flex-wrap gap-1.5">
            {choices.length === 0 && <span className="text-xs text-slate-400">Không có job đang chờ.</span>}
            {choices.map((c) => {
              const picked = order.includes(c.id)
              return (
                <button
                  key={c.id}
                  type="button"
                  onClick={() => toggle(c.id)}
                  className={`rounded-lg border px-2.5 py-1.5 text-sm transition-colors ${
                    picked
                      ? 'border-brand-500 bg-brand-50 text-brand-700 dark:bg-brand-500/10 dark:text-brand-300'
                      : 'border-slate-200 dark:border-slate-700 text-slate-600 dark:text-slate-300 hover:bg-slate-50 dark:hover:bg-slate-800'
                  }`}
                >
                  {picked ? `${order.indexOf(c.id) + 1}. ` : '+ '}
                  {c.label}
                </button>
              )
            })}
          </div>
        </div>
        <div className="flex justify-end gap-2 border-t border-slate-100 dark:border-slate-800 px-5 py-3">
          <Button onClick={onClose}>Hủy</Button>
          <Button variant="primary" disabled={order.length === 0} onClick={() => onApply(order)}>
            Áp dụng danh sách ({order.length})
          </Button>
        </div>
      </div>
    </div>
  )
}
