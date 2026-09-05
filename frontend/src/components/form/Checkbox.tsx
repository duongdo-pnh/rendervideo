interface Props {
  label: string
  checked: boolean
  onChange: (checked: boolean) => void
}

export function Checkbox({ label, checked, onChange }: Props) {
  return (
    <label className="flex items-center gap-2 text-sm text-slate-700 dark:text-slate-300">
      <input
        type="checkbox"
        checked={checked}
        onChange={(e) => onChange(e.target.checked)}
        className="h-4 w-4 rounded border-slate-300 dark:border-slate-700 accent-brand-500"
      />
      {label}
    </label>
  )
}
