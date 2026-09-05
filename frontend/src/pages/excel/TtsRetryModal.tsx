import { useState } from 'react'
import { CheckSquare, RotateCcw, Square, X } from 'lucide-react'
import { Button } from '../../components/Button'
import type { TtsJobRow } from '../../types/api'

export function TtsRetryModal({
  jobs,
  submitting,
  onClose,
  onConfirm,
}: {
  jobs: TtsJobRow[]
  submitting: boolean
  onClose: () => void
  onConfirm: (ids: number[]) => void
}) {
  const [selected, setSelected] = useState<number[]>([])

  const toggle = (id: number) => {
    setSelected((prev) => (prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id]))
  }

  const allSelected = jobs.length > 0 && selected.length === jobs.length
  const toggleAll = () => setSelected(allSelected ? [] : jobs.map((j) => j.id))

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4">
      <div className="flex max-h-[85vh] w-full max-w-2xl flex-col rounded-2xl border border-slate-200 bg-white shadow-card dark:border-slate-800 dark:bg-slate-900">
        <div className="flex items-start justify-between border-b border-slate-100 px-5 py-4 dark:border-slate-800">
          <div>
            <h2 className="flex items-center gap-1.5 text-sm font-semibold text-slate-800 dark:text-slate-100">
              <RotateCcw className="h-4 w-4 text-brand-600 dark:text-brand-400" /> Thử lại nhiều dòng
            </h2>
            <p className="mt-0.5 text-xs text-slate-500">Chọn các dòng lỗi muốn đưa lại vào TTS queue.</p>
          </div>
          <button onClick={onClose} className="text-slate-400 hover:text-slate-600">
            <X className="h-4 w-4" />
          </button>
        </div>

        <div className="flex shrink-0 items-center justify-between border-b border-slate-100 px-5 py-2.5 dark:border-slate-800">
          <button
            type="button"
            onClick={toggleAll}
            className="inline-flex items-center gap-1.5 text-xs font-medium text-brand-600 hover:text-brand-700 dark:text-brand-400 dark:hover:text-brand-300"
          >
            {allSelected ? <CheckSquare className="h-3.5 w-3.5" /> : <Square className="h-3.5 w-3.5" />}
            Chọn tất cả ({jobs.length})
          </button>
          <span className="text-xs text-slate-400">Đã chọn: {selected.length}</span>
        </div>

        <div className="min-h-0 flex-1 overflow-y-auto p-2">
          {jobs.length === 0 && <p className="p-3 text-sm text-slate-400">Không có dòng lỗi nào.</p>}
          {jobs.map((j) => {
            const picked = selected.includes(j.id)
            return (
              <label
                key={j.id}
                className={`flex cursor-pointer items-start gap-2.5 rounded-lg px-3 py-2 text-sm transition-colors ${
                  picked ? 'bg-brand-50 dark:bg-brand-500/10' : 'hover:bg-slate-50 dark:hover:bg-slate-800'
                }`}
              >
                <input
                  type="checkbox"
                  checked={picked}
                  onChange={() => toggle(j.id)}
                  className="mt-0.5 h-4 w-4 shrink-0 rounded border-slate-300 accent-brand-500 dark:border-slate-700"
                />
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-1.5 text-xs text-slate-500">
                    <span className="font-mono">#{j.row}</span>
                    <span className="truncate font-medium text-slate-700 dark:text-slate-300">
                      {j.product || '(chung)'}
                    </span>
                  </div>
                  <p className="truncate text-xs text-slate-500">{j.text}</p>
                  {j.error && <p className="truncate text-xs text-rose-600 dark:text-rose-400">{j.error}</p>}
                </div>
              </label>
            )
          })}
        </div>

        <div className="flex justify-end gap-2 border-t border-slate-100 px-5 py-3 dark:border-slate-800">
          <Button onClick={onClose}>Hủy</Button>
          <Button
            variant="primary"
            loading={submitting}
            disabled={selected.length === 0}
            onClick={() => onConfirm(selected)}
          >
            <RotateCcw className="h-3.5 w-3.5" /> Thử lại {selected.length} dòng
          </Button>
        </div>
      </div>
    </div>
  )
}
