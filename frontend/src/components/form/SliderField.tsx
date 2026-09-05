interface Props {
  label: string
  value: number
  min: number
  max: number
  step?: number
  onChange: (value: number) => void
  format?: (value: number) => string
}

export function SliderField({ label, value, min, max, step = 1, onChange, format }: Props) {
  return (
    <label className="block text-sm">
      <span className="mb-1 flex items-center justify-between font-medium text-slate-700 dark:text-slate-300">
        <span>{label}</span>
        <span className="font-mono text-xs text-brand-600 dark:text-brand-400">{format ? format(value) : value}</span>
      </span>
      <input
        type="range"
        min={min}
        max={max}
        step={step}
        value={value}
        onChange={(e) => onChange(Number(e.target.value))}
        className="w-full accent-brand-500"
      />
    </label>
  )
}
