interface Option<T extends string> {
  label: string
  value: T
}

interface Props<T extends string> {
  label?: string
  options: Option<T>[]
  value: T
  onChange: (value: T) => void
}

export function RadioGroup<T extends string>({ label, options, value, onChange }: Props<T>) {
  return (
    <div className="text-sm">
      {label && <span className="mb-1 block font-medium text-slate-700 dark:text-slate-300">{label}</span>}
      <div className="flex flex-wrap gap-1.5">
        {options.map((opt) => (
          <button
            key={opt.value}
            type="button"
            onClick={() => onChange(opt.value)}
            className={`rounded-lg border px-3 py-1.5 text-sm transition-colors ${
              value === opt.value
                ? 'border-brand-500 bg-brand-50 text-brand-700 dark:bg-brand-500/10 dark:text-brand-300'
                : 'border-slate-300 dark:border-slate-700 text-slate-600 dark:text-slate-300 hover:bg-slate-50 dark:hover:bg-slate-800'
            }`}
          >
            {opt.label}
          </button>
        ))}
      </div>
    </div>
  )
}
