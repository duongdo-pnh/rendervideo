import type { ReactNode } from 'react'
import type { LucideIcon } from 'lucide-react'

type Tone = 'neutral' | 'success' | 'warning' | 'error' | 'brand'

const TONES: Record<Tone, string> = {
  neutral: 'bg-slate-100 text-slate-700 dark:bg-slate-800 dark:text-slate-300',
  success: 'bg-emerald-50 text-emerald-700 dark:bg-emerald-950 dark:text-emerald-400',
  warning: 'bg-amber-50 text-amber-700 dark:bg-amber-950 dark:text-amber-400',
  error: 'bg-rose-50 text-rose-700 dark:bg-rose-950 dark:text-rose-400',
  brand: 'bg-brand-50 text-brand-700 dark:bg-brand-500/10 dark:text-brand-300',
}

export function Badge({
  tone = 'neutral',
  icon: Icon,
  spin,
  children,
}: {
  tone?: Tone
  icon?: LucideIcon
  spin?: boolean
  children: ReactNode
}) {
  return (
    <span className={`inline-flex items-center gap-1 whitespace-nowrap rounded-lg px-2 py-0.5 text-xs font-medium tracking-wide ${TONES[tone]}`}>
      {Icon && <Icon className={`h-3 w-3 shrink-0 ${spin ? 'animate-spin' : ''}`} strokeWidth={2} />}
      {children}
    </span>
  )
}
