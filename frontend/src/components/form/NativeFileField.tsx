interface Props {
  label: string
  accept: string
  multiple?: boolean
  files: File[]
  onChange: (files: File[]) => void
}

export function NativeFileField({ label, accept, multiple, files, onChange }: Props) {
  const summary =
    files.length === 0 ? 'No file chosen' : files.length === 1 ? files[0].name : `${files.length} files`

  return (
    <label className="block text-sm">
      <span className="mb-1 block font-medium text-slate-700 dark:text-slate-300">{label}</span>
      <span className="flex items-center gap-2">
        <span className="relative inline-flex cursor-pointer items-center rounded-lg border border-slate-300 dark:border-slate-700 bg-slate-100 dark:bg-slate-800 px-3 py-1.5 text-sm font-medium text-slate-700 dark:text-slate-200 hover:bg-slate-200 dark:hover:bg-slate-700">
          Choose File{multiple ? 's' : ''}
          <input
            type="file"
            accept={accept}
            multiple={multiple}
            className="absolute inset-0 h-full w-full cursor-pointer opacity-0"
            onChange={(e) => onChange(Array.from(e.target.files ?? []))}
          />
        </span>
        <span className="truncate text-sm text-slate-500">{summary}</span>
      </span>
    </label>
  )
}
