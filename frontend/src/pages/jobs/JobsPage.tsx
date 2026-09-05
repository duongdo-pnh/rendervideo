import { useSearchParams } from 'react-router-dom'
import { Film, ListChecks } from 'lucide-react'
import { CreateJobTab } from './CreateJobTab'
import { QueueStatusTab } from './QueueStatusTab'

const TABS = [
  { key: 'create', label: 'Tạo job render', icon: Film },
  { key: 'queue', label: 'Trạng thái queue', icon: ListChecks },
] as const

export default function JobsPage() {
  const [params, setParams] = useSearchParams()
  const tab = params.get('tab') === 'queue' ? 'queue' : 'create'

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
      <div className="min-h-0 flex-1">{tab === 'create' ? <CreateJobTab /> : <QueueStatusTab />}</div>
    </div>
  )
}
