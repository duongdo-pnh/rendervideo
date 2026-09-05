import type { InputHTMLAttributes } from 'react'

interface Props extends InputHTMLAttributes<HTMLInputElement> {
  label?: string
  hint?: string
}

export function TextField({ label, hint, className, id, ...rest }: Props) {
  return (
    <label className="block text-sm" htmlFor={id}>
      {label && <span className="mb-1 block font-medium text-slate-700 dark:text-slate-300">{label}</span>}
      <input
        id={id}
        className={`w-full rounded-lg border border-slate-300 dark:border-slate-700 bg-white dark:bg-slate-900 px-3 py-1.5 text-sm text-slate-900 dark:text-slate-100 placeholder:text-slate-400 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500 ${className ?? ''}`}
        {...rest}
      />
      {hint && <span className="mt-1 block text-xs text-slate-500">{hint}</span>}
    </label>
  )
}
