import { useAppSelector } from '../../store/hooks'

export function WsStatusIndicator() {
  const status = useAppSelector((s) => s.jobs.wsStatus)
  const label = status === 'connected' ? 'Đã kết nối' : status === 'connecting' ? 'Đang kết nối…' : 'Mất kết nối';
  const dot =
    status === 'connected'
      ? 'bg-emerald-500'
      : status === 'connecting'
        ? 'bg-amber-500 animate-pulse'
        : 'bg-rose-500'

  return (
    <span className="inline-flex items-center gap-1.5 rounded-lg bg-slate-100 dark:bg-slate-800 px-2.5 py-1 text-xs font-medium text-slate-600 dark:text-slate-300">
      <span className={`h-1.5 w-1.5 rounded-full ${dot}`} />
      {label}
    </span>
  )
}
