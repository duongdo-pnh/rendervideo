import { useCallback, useEffect, useMemo, useRef, useState, type DragEvent } from 'react'
import { FileCheck, UploadCloud, X } from 'lucide-react'

interface Props {
  label: string
  accept: string
  kind: 'video' | 'audio' | 'image'
  file: File | null
  onChange: (file: File | null) => void
  /** false: show only the chosen file's name (no player / image) */
  preview?: boolean
}

export function FileDropzone({ label, accept, kind, file, onChange, preview = true }: Props) {
  const [dragOver, setDragOver] = useState(false)
  const inputRef = useRef<HTMLInputElement>(null)
  const url = useMemo(() => (file && preview ? URL.createObjectURL(file) : null), [file, preview])
  useEffect(() => () => {
    if (url) URL.revokeObjectURL(url)
  }, [url])

  const handleDrop = useCallback(
    (e: DragEvent<HTMLDivElement>) => {
      e.preventDefault()
      setDragOver(false)
      const dropped = e.dataTransfer.files?.[0]
      if (dropped) onChange(dropped)
    },
    [onChange],
  )

  return (
    <div className="text-sm">
      <span className="mb-1 block font-medium text-slate-700 dark:text-slate-300">{label}</span>
      <div
        onDragOver={(e) => {
          e.preventDefault()
          setDragOver(true)
        }}
        onDragLeave={() => setDragOver(false)}
        onDrop={handleDrop}
        onClick={() => inputRef.current?.click()}
        className={`cursor-pointer rounded-xl border-2 border-dashed p-3 transition-colors ${
          dragOver
            ? 'border-brand-500 bg-brand-50 dark:bg-brand-500/10'
            : 'border-slate-300 dark:border-slate-700 hover:border-brand-400'
        }`}
      >
        <input
          ref={inputRef}
          type="file"
          accept={accept}
          className="hidden"
          onChange={(e) => onChange(e.target.files?.[0] ?? null)}
        />
        {!file && (
          <div className="flex flex-col items-center gap-1 py-6 text-slate-500">
            <UploadCloud className="h-6 w-6" />
            <span className="text-xs">Kéo thả hoặc bấm để chọn file</span>
          </div>
        )}
        {file && !preview && (
          <div onClick={(e) => e.stopPropagation()} className="flex items-center justify-between gap-2 py-3">
            <span className="flex min-w-0 items-center gap-2 text-sm">
              <FileCheck className="h-4 w-4 shrink-0 text-emerald-500" />
              <span className="truncate font-medium text-slate-700 dark:text-slate-200">{file.name}</span>
              <span className="shrink-0 text-xs text-slate-400">{(file.size / 1024 / 1024).toFixed(1)} MB</span>
            </span>
            <button
              type="button"
              onClick={() => onChange(null)}
              className="shrink-0 rounded-full p-1 text-slate-400 hover:bg-slate-100 dark:hover:bg-slate-800 hover:text-rose-600"
              aria-label="Xóa file"
            >
              <X className="h-3.5 w-3.5" />
            </button>
          </div>
        )}
        {file && url && (
          <div onClick={(e) => e.stopPropagation()}>
            {kind === 'video' ? (
              <video src={url} controls className="max-h-56 w-full rounded-lg bg-black" />
            ) : kind === 'image' ? (
              <img src={url} alt={file.name} className="mx-auto max-h-56 rounded-lg object-contain" />
            ) : (
              <audio src={url} controls className="w-full" />
            )}
            <div className="mt-2 flex items-center justify-between">
              <span className="truncate text-xs text-slate-500">{file.name}</span>
              <button
                type="button"
                onClick={() => onChange(null)}
                className="ml-2 shrink-0 rounded-full p-1 text-slate-400 hover:bg-slate-100 dark:hover:bg-slate-800 hover:text-rose-600"
                aria-label="Xóa file"
              >
                <X className="h-3.5 w-3.5" />
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  )
}
