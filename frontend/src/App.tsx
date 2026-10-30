import { Suspense, lazy } from 'react'
import { Routes, Route } from 'react-router-dom'
import { AppLayout } from './layout/AppLayout'
import { NodeKeyGate } from './layout/NodeKeyGate'
import { Spinner } from './components/Spinner'

const JobsPage = lazy(() => import('./pages/jobs/JobsPage'))
const AnimatePage = lazy(() => import('./pages/animate/AnimatePage'))
const VideosPage = lazy(() => import('./pages/videos/VideosPage'))
const ExcelPage = lazy(() => import('./pages/excel/ExcelPage'))
const AvatarCachePage = lazy(() => import('./pages/avatar-cache/AvatarCachePage'))
const LivestreamPage = lazy(() => import('./pages/livestream/LivestreamPage'))
const DrivePage = lazy(() => import('./pages/drive/DrivePage'))
const SettingsPage = lazy(() => import('./pages/settings/SettingsPage'))

function PageFallback() {
  return (
    <div className="flex items-center justify-center py-24 text-slate-400">
      <Spinner className="h-6 w-6" />
    </div>
  )
}

export function App() {
  return (
    <NodeKeyGate>
      <Suspense fallback={<PageFallback />}>
        <Routes>
          <Route element={<AppLayout />}>
            <Route index element={<JobsPage />} />
            <Route path="animate" element={<AnimatePage />} />
            <Route path="videos" element={<VideosPage />} />
            <Route path="excel" element={<ExcelPage />} />
            <Route path="avatar-cache" element={<AvatarCachePage />} />
            <Route path="livestream" element={<LivestreamPage />} />
            <Route path="drive" element={<DrivePage />} />
            <Route path="settings" element={<SettingsPage />} />
          </Route>
        </Routes>
      </Suspense>
    </NodeKeyGate>
  )
}
