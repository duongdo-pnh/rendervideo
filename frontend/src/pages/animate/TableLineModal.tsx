import { useEffect, useMemo, useRef, useState, type PointerEvent } from 'react'
import { Ruler, X } from 'lucide-react'
import { Button } from '../../components/Button'
import { SliderField } from '../../components/form/SliderField'

/**
 * Required step after choosing a character image: set where the table edge is. The frame is the
 * image as the model sees it (center-cropped to 640×1152, like fun5b's ImageOps.fit); everything
 * below the red line is kept exactly as in the image. Click or drag on the picture to move it.
 * 0 = automatic (fun5b.matting finds the table itself).
 */
export function TableLineModal({
  image,
  initialPercent,
  onConfirm,
  onCancel,
  cancelLabel = 'Huỷ',
}: {
  image: File
  initialPercent: number
  onConfirm: (percent: number) => void
  onCancel: () => void
  cancelLabel?: string
}) {
  const [percent, setPercent] = useState(initialPercent)
  const [dragging, setDragging] = useState(false)
  const frameRef = useRef<HTMLDivElement>(null)
  const url = useMemo(() => URL.createObjectURL(image), [image])
  useEffect(() => () => URL.revokeObjectURL(url), [url])

  const setFromPointer = (e: PointerEvent<HTMLDivElement>) => {
    const box = frameRef.current?.getBoundingClientRect()
    if (!box) return
    const p = Math.round(((e.clientY - box.top) / box.height) * 100)
    setPercent(Math.min(99, Math.max(1, p)))
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-2 sm:p-4">
      <div className="flex max-h-[95vh] w-full max-w-xl flex-col rounded-2xl border border-slate-200 bg-white shadow-card dark:border-slate-800 dark:bg-slate-900">
        <div className="flex items-start justify-between gap-3 border-b border-slate-100 px-4 py-3 sm:px-5 sm:py-4 dark:border-slate-800">
          <div>
            <h2 className="flex items-center gap-1.5 text-sm font-semibold text-slate-800 dark:text-slate-100">
              <Ruler className="h-4 w-4 text-brand-600 dark:text-brand-400" /> Chỉnh mép bàn
            </h2>
            <p className="mt-0.5 text-xs text-slate-500">
              Bấm hoặc kéo trên ảnh để đặt vạch đỏ. Phần dưới vạch (bàn, sản phẩm) giữ nguyên như ảnh gốc; tay đưa xuống
              dưới vạch sẽ bị bàn che. Không có bàn thì chọn <strong>Tự động</strong>.
            </p>
          </div>
          <button onClick={onCancel} className="text-slate-400 hover:text-slate-600" aria-label="Đóng">
            <X className="h-4 w-4" />
          </button>
        </div>

        <div className="min-h-0 flex-1 overflow-y-auto p-4 sm:p-5">
          <div
            ref={frameRef}
            onPointerDown={(e) => {
              e.currentTarget.setPointerCapture(e.pointerId)
              setDragging(true)
              setFromPointer(e)
            }}
            onPointerMove={(e) => dragging && setFromPointer(e)}
            onPointerUp={() => setDragging(false)}
            className="relative mx-auto aspect-[640/1152] h-[55vh] max-w-full cursor-row-resize touch-none select-none overflow-hidden rounded-lg bg-slate-100 dark:bg-slate-800"
          >
            <img src={url} alt="" draggable={false} className="h-full w-full object-cover" />
            {percent > 0 && (
              <div className="pointer-events-none absolute inset-x-0 bottom-0 border-t-2 border-rose-500 bg-rose-500/30" style={{ top: `${percent}%` }}>
                <span className="absolute -top-6 right-1 rounded bg-rose-500 px-1.5 py-0.5 text-[11px] font-medium text-white">{percent}%</span>
              </div>
            )}
            {percent === 0 && (
              <span className="pointer-events-none absolute inset-x-0 bottom-2 mx-auto w-fit rounded bg-black/60 px-2 py-1 text-xs text-white">
                Tự động — bấm vào ảnh để đặt mép bàn
              </span>
            )}
          </div>
          <div className="mx-auto mt-4 max-w-sm">
            <SliderField
              label="Mép bàn (% chiều cao)"
              value={percent}
              min={0}
              max={99}
              onChange={setPercent}
              format={(v) => (v === 0 ? 'Tự động' : `${v}%`)}
            />
          </div>
        </div>

        <div className="flex flex-wrap items-center justify-between gap-2 border-t border-slate-100 px-4 py-3 sm:px-5 dark:border-slate-800">
          <Button variant="ghost" onClick={() => setPercent(0)} disabled={percent === 0}>
            Tự động
          </Button>
          <div className="ml-auto flex gap-2">
            <Button onClick={onCancel}>{cancelLabel}</Button>
            <Button variant="primary" onClick={() => onConfirm(percent)}>
              Xác nhận{percent > 0 ? ` (${percent}%)` : ' (tự động)'}
            </Button>
          </div>
        </div>
      </div>
    </div>
  )
}
