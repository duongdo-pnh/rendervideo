import type { ComponentProps, ReactNode } from 'react'
import type { LucideIcon } from 'lucide-react'

export function CardHeader({ icon: Icon, title, actions }: { icon: LucideIcon; title: string; actions?: ReactNode }) {
  return (
    <div className="flex shrink-0 flex-wrap items-center justify-between gap-3 border-b border-slate-100 dark:border-slate-800 px-4 py-3.5">
      <div className="flex items-center gap-2.5">
        <span className="flex h-7 w-7 shrink-0 items-center justify-center rounded-lg bg-brand-50 dark:bg-brand-500/10 text-brand-600 dark:text-brand-400">
          <Icon className="h-4 w-4" strokeWidth={1.75} />
        </span>
        <h2 className="text-[15px] font-semibold text-slate-800 dark:text-slate-100">{title}</h2>
      </div>
      {actions && <div className="flex flex-wrap items-center gap-2.5 sm:gap-3 text-sm">{actions}</div>}
    </div>
  )
}

export function LinkAction({ children, className, ...rest }: ComponentProps<'button'>) {
  return (
    <button
      type="button"
      className={`inline-flex items-center gap-1 text-sm font-medium text-brand-600 hover:text-brand-700 dark:text-brand-400 dark:hover:text-brand-300 disabled:opacity-50 ${className ?? ''}`}
      {...rest}
    >
      {children}
    </button>
  )
}

export function IconAction({ children, className, ...rest }: ComponentProps<'button'>) {
  return (
    <button
      type="button"
      className={`flex h-7 w-7 items-center justify-center rounded-lg text-slate-500 hover:bg-slate-100 dark:text-slate-400 dark:hover:bg-slate-800 disabled:opacity-50 ${className ?? ''}`}
      {...rest}
    >
      {children}
    </button>
  )
}
