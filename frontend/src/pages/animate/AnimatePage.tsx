import { useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { Images, Wand2 } from 'lucide-react'
import { Card } from '../../components/Card'
import { Spinner } from '../../components/Spinner'
import { getFun5bInfo } from '../../api/engines'
import { ApiError } from '../../api/client'
import { usePolling } from '../../hooks/usePolling'
import type { Fun5bInfo } from '../../types/api'
import { CreateAnimateTab } from './CreateAnimateTab'
import { InstallCard } from './InstallCard'
import { PlatesTab } from './PlatesTab'

const TABS = [
  { key: 'create', label: 'Tạo video chuyển động', icon: Wand2 },
  { key: 'plates', label: 'Nền đã lưu', icon: Images },
] as const

/** Fun-5B character animation: its own page next to the lip-sync jobs; jobs share the same queue. */
export default function AnimatePage() {
  const [params, setParams] = useSearchParams()
  const tab = params.get('tab') === 'plates' ? 'plates' : 'create'
  const [info, setInfo] = useState<Fun5bInfo | null>(null)
  const [error, setError] = useState<string | null>(null)

  const load = () =>
    getFun5bInfo()
      .then((i) => {
        setInfo(i)
        setError(null)
      })
      .catch((err) => setError(err instanceof ApiError ? err.message : 'Không tải được thông tin Fun-5B.'))
  // Once usable the form takes over; until then follow the background install every 5s.
  usePolling(load, 5_000, !info?.available)

  return (
    <div className="flex h-full flex-col">
      <div className="mb-4 flex shrink-0 gap-1 overflow-x-auto border-b border-slate-200 dark:border-slate-800">
        {TABS.map((t) => (
          <button
            key={t.key}
            type="button"
            title={t.label}
            onClick={() => setParams(t.key === 'create' ? {} : { tab: t.key })}
            className={`-mb-px flex shrink-0 items-center gap-1.5 whitespace-nowrap border-b-2 px-3 sm:px-4 py-2.5 text-sm font-medium transition-colors ${
              tab === t.key
                ? 'border-brand-500 text-brand-600 dark:text-brand-400'
                : 'border-transparent text-slate-500 hover:text-slate-700 dark:hover:text-slate-300'
            }`}
          >
            <t.icon className="h-4 w-4 shrink-0" />
            <span className="hidden sm:inline">{t.label}</span>
          </button>
        ))}
      </div>
      <div className="min-h-0 flex-1">
        {tab === 'plates' ? (
          <PlatesTab />
        ) : error ? (
          <Card><p className="text-sm text-rose-600">{error}</p></Card>
        ) : !info ? (
          <Spinner className="mx-auto my-24 h-6 w-6 text-slate-400" />
        ) : !info.available ? (
          <InstallCard info={info} onChanged={() => void load()} />
        ) : (
          <CreateAnimateTab info={info} />
        )}
      </div>
    </div>
  )
}
