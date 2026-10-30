import { useState } from 'react'
import { NavLink, Outlet, useLocation } from 'react-router-dom'
import {
  Clapperboard,
  PersonStanding,
  Video,
  FileSpreadsheet,
  UserRound,
  Radio,
  Cloud,
  Settings,
  Moon,
  Sun,
  Menu,
  X,
} from 'lucide-react'
import { useAppDispatch, useAppSelector } from '../store/hooks'
import { toggleTheme } from '../store/slices/uiSlice'

interface NavItem {
  to: string
  label: string
  icon: typeof Clapperboard
  end?: boolean
}

export const NAV: NavItem[] = [
  { to: '/', label: 'Render / Jobs', icon: Clapperboard, end: true },
  { to: '/animate', label: 'Chuyển động nhân vật', icon: PersonStanding },
  { to: '/videos', label: 'Video đã render', icon: Video },
  { to: '/excel', label: 'Import Excel', icon: FileSpreadsheet },
  { to: '/avatar-cache', label: 'Avatar Cache', icon: UserRound },
  { to: '/livestream', label: 'Livestream', icon: Radio },
  { to: '/drive', label: 'Google Drive', icon: Cloud },
  { to: '/settings', label: 'Cài đặt', icon: Settings },
]

function SidebarContent({ onNavigate }: { onNavigate?: () => void }) {
  const theme = useAppSelector((s) => s.ui.theme)
  const dispatch = useAppDispatch()

  return (
    <>
      <div className="flex items-center gap-2 px-4 py-4">
        <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-xl bg-gradient-to-br from-brand-500 to-brand-700 text-sm font-bold text-white">
          BR
        </div>
        <div>
          <div className="text-sm font-semibold leading-tight">Byscom Render</div>
          <div className="text-xs text-slate-500">Lip-sync pipeline · v{__APP_VERSION__}</div>
        </div>
      </div>
      <nav className="flex-1 space-y-0.5 px-2 py-2">
        {NAV.map(({ to, label, icon: Icon, end }) => (
          <NavLink
            key={to}
            to={to}
            end={end}
            onClick={onNavigate}
            className={({ isActive }) =>
              `relative flex items-center gap-2.5 rounded-lg px-3 py-2 text-sm font-medium transition-colors ${
                isActive
                  ? 'bg-brand-50 text-brand-700 dark:bg-brand-500/10 dark:text-brand-300'
                  : 'text-slate-600 dark:text-slate-400 hover:bg-slate-100 dark:hover:bg-slate-800'
              }`
            }
          >
            {({ isActive }) => (
              <>
                {isActive && <span className="absolute left-0 top-1.5 bottom-1.5 w-0.5 rounded-full bg-brand-500" />}
                <Icon className="h-[18px] w-[18px] shrink-0" strokeWidth={1.75} />
                <span className="truncate">{label}</span>
              </>
            )}
          </NavLink>
        ))}
      </nav>
      <div className="border-t border-slate-200 dark:border-slate-800 p-2">
        <button
          type="button"
          onClick={() => dispatch(toggleTheme())}
          className="flex w-full items-center gap-2.5 rounded-lg px-3 py-2 text-sm font-medium text-slate-600 dark:text-slate-400 hover:bg-slate-100 dark:hover:bg-slate-800"
        >
          {theme === 'dark' ? <Sun className="h-[18px] w-[18px]" strokeWidth={1.75} /> : <Moon className="h-[18px] w-[18px]" strokeWidth={1.75} />}
          {theme === 'dark' ? 'Giao diện sáng' : 'Giao diện tối'}
        </button>
      </div>
    </>
  )
}

export function AppLayout() {
  const location = useLocation()
  const current = NAV.find((n) => (n.end ? location.pathname === n.to : location.pathname.startsWith(n.to))) ?? NAV[0]
  const [mobileOpen, setMobileOpen] = useState(false)

  return (
    <div className="flex h-screen bg-slate-50 dark:bg-slate-950 text-slate-900 dark:text-slate-100">
      {/* Desktop sidebar */}
      <aside className="hidden lg:flex w-60 shrink-0 flex-col border-r border-slate-200 dark:border-slate-800 bg-white dark:bg-slate-900">
        <SidebarContent />
      </aside>

      {/* Mobile sidebar overlay */}
      {mobileOpen && (
        <div className="fixed inset-0 z-40 flex lg:hidden">
          <div className="fixed inset-0 bg-black/40" onClick={() => setMobileOpen(false)} />
          <aside className="relative flex w-64 flex-col border-r border-slate-200 dark:border-slate-800 bg-white dark:bg-slate-900">
            <button
              onClick={() => setMobileOpen(false)}
              className="absolute right-3 top-4 rounded-lg p-1.5 text-slate-400 hover:bg-slate-100 dark:hover:bg-slate-800"
            >
              <X className="h-4 w-4" />
            </button>
            <SidebarContent onNavigate={() => setMobileOpen(false)} />
          </aside>
        </div>
      )}

      <main className="flex flex-1 flex-col overflow-hidden min-w-0">
        <div className="flex shrink-0 items-center gap-1.5 border-b border-slate-200 dark:border-slate-800 bg-white dark:bg-slate-900 px-4 sm:px-6 py-3 text-sm text-slate-500">
          <button
            onClick={() => setMobileOpen(true)}
            aria-label="Mở menu điều hướng"
            className="mr-1 rounded-lg p-1 text-slate-500 hover:bg-slate-100 dark:hover:bg-slate-800 lg:hidden"
          >
            <Menu className="h-4 w-4" />
          </button>
          <current.icon className="h-3.5 w-3.5 shrink-0" />
          <span className="truncate">{current.label}</span>
        </div>
        <div className="flex-1 overflow-y-auto p-4 sm:p-6 min-h-0">
          <div className="h-full">
            <Outlet />
          </div>
        </div>
      </main>
    </div>
  )
}
