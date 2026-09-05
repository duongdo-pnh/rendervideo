import { createContext, useCallback, useContext, useRef, useState, type ReactNode } from 'react'
import { CheckCircle2, AlertTriangle, XCircle, X } from 'lucide-react'

type Severity = 'success' | 'warning' | 'error'

interface Toast {
  id: number
  severity: Severity
  message: string
}

interface NotifyApi {
  success: (message: string) => void
  warning: (message: string) => void
  error: (message: string) => void
}

const NotifyContext = createContext<NotifyApi | null>(null)

const STYLES: Record<Severity, { bg: string; border: string; text: string; icon: typeof CheckCircle2 }> = {
  success: {
    bg: 'bg-emerald-50 dark:bg-emerald-950',
    border: 'border-emerald-500',
    text: 'text-emerald-700 dark:text-emerald-400',
    icon: CheckCircle2,
  },
  warning: {
    bg: 'bg-amber-50 dark:bg-amber-950',
    border: 'border-amber-500',
    text: 'text-amber-700 dark:text-amber-400',
    icon: AlertTriangle,
  },
  error: {
    bg: 'bg-rose-50 dark:bg-rose-950',
    border: 'border-rose-500',
    text: 'text-rose-700 dark:text-rose-400',
    icon: XCircle,
  },
}

export function NotificationProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([])
  const idRef = useRef(0)

  const dismiss = useCallback((id: number) => {
    setToasts((prev) => prev.filter((t) => t.id !== id))
  }, [])

  const push = useCallback(
    (severity: Severity, message: string) => {
      const id = ++idRef.current
      setToasts((prev) => [...prev, { id, severity, message }])
      const timeout = severity === 'error' ? 8000 : 4000
      setTimeout(() => dismiss(id), timeout)
    },
    [dismiss],
  )

  const api: NotifyApi = {
    success: (message) => push('success', message),
    warning: (message) => push('warning', message),
    error: (message) => push('error', message),
  }

  return (
    <NotifyContext.Provider value={api}>
      {children}
      <div className="fixed bottom-4 right-4 z-50 flex flex-col gap-2 w-full max-w-sm">
        {toasts.map((t) => {
          const s = STYLES[t.severity]
          const Icon = s.icon
          return (
            <div
              key={t.id}
              role="status"
              className={`flex items-start gap-2 rounded-xl border-l-4 ${s.border} ${s.bg} px-4 py-3 shadow-card`}
            >
              <Icon className={`mt-0.5 h-4 w-4 shrink-0 ${s.text}`} />
              <p className={`flex-1 text-sm ${s.text}`}>{t.message}</p>
              <button
                type="button"
                onClick={() => dismiss(t.id)}
                className={`shrink-0 ${s.text} opacity-60 hover:opacity-100`}
                aria-label="Đóng"
              >
                <X className="h-3.5 w-3.5" />
              </button>
            </div>
          )
        })}
      </div>
    </NotifyContext.Provider>
  )
}

export function useNotify(): NotifyApi {
  const ctx = useContext(NotifyContext)
  if (!ctx) throw new Error('useNotify must be used within NotificationProvider')
  return ctx
}
