import type { SelectHTMLAttributes } from 'react'

interface Option {
  label: string
  value: string
}

interface Props extends Omit<SelectHTMLAttributes<HTMLSelectElement>, 'onChange'> {
  label?: string
  options: Option[]
  placeholder?: string
  onChange: (value: string) => void
}

export function SelectField({ label, options, placeholder, onChange, className, id, ...rest }: Props) {
  return (
    <label className="block text-sm" htmlFor={id}>
      {label && <span className="mb-1 block font-medium text-slate-700 dark:text-slate-300">{label}</span>}
      <select
        id={id}
        onChange={(e) => onChange(e.target.value)}
        className={`w-full rounded-lg border border-slate-300 dark:border-slate-700 bg-white dark:bg-slate-900 px-3 py-1.5 text-sm text-slate-900 dark:text-slate-100 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500 ${className ?? ''}`}
        {...rest}
      >
        {placeholder && <option value="">{placeholder}</option>}
        {options.map((opt) => (
          <option key={opt.value} value={opt.value}>
            {opt.label}
          </option>
        ))}
      </select>
    </label>
  )
}
