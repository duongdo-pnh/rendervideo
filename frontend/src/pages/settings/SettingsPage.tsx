import { useSearchParams } from 'react-router-dom'
import { AudioLines, LayoutDashboard, PlugZap } from 'lucide-react'
import { TtsConfigTab } from './TtsConfigTab'
import { DashboardFleetTab } from './DashboardFleetTab'
import { AkaliTab } from './AkaliTab'

const TABS = [
  { key: 'tts', label: 'Cấu hình TTS', icon: AudioLines },
  { key: 'dashboard', label: 'Dashboard Fleet', icon: LayoutDashboard },
  { key: 'akali', label: 'Kết nối akali', icon: PlugZap },
] as const

type TabKey = (typeof TABS)[number]['key']

export default function SettingsPage() {
  const [params, setParams] = useSearchParams()
  const requested = params.get('tab')
  const tab: TabKey = TABS.some((t) => t.key === requested) ? (requested as TabKey) : 'tts'

  return (
    <div className="flex h-full flex-col">
      <div className="mb-4 flex shrink-0 gap-1 overflow-x-auto border-b border-slate-200 dark:border-slate-800">
        {TABS.map((t) => (
          <button
            key={t.key}
            type="button"
            title={t.label}
            onClick={() => setParams(t.key === 'tts' ? {} : { tab: t.key })}
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
        {tab === 'tts' && <TtsConfigTab />}
        {tab === 'dashboard' && <DashboardFleetTab />}
        {tab === 'akali' && <AkaliTab />}
      </div>
    </div>
  )
}
