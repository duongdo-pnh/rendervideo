import { useState } from 'react'
import { AlertTriangle, Download, Info, RotateCcw } from 'lucide-react'
import { Card } from '../../components/Card'
import { CardHeader } from '../../components/CardHeader'
import { Button } from '../../components/Button'
import { Spinner } from '../../components/Spinner'
import { useNotify } from '../../components/notifications/NotificationProvider'
import { startFun5bInstall } from '../../api/engines'
import { ApiError } from '../../api/client'
import type { Fun5bInfo } from '../../types/api'

/** The model download (step total-1) is nearly all of the install's time: weigh it 85%, the
 * environment steps before it 10%, the final check 5%. */
function installPercent(step: number, total: number, downloadedGb: number | null | undefined, modelsGb: number) {
  const download = total - 1
  if (step < download) return Math.round((Math.max(step - 1, 0) / (download - 1)) * 10)
  if (step === download) return Math.round(10 + Math.min(1, (downloadedGb ?? 0) / (modelsGb || 1)) * 85)
  return 95
}

/**
 * Shown instead of the form while Fun-5B is not usable on this machine. After an update the app
 * installs it by itself in the background (fun5b_setup.auto_start); this card follows that install
 * (AnimatePage polls) and offers a retry when it failed.
 */
export function InstallCard({ info, onChanged }: { info: Fun5bInfo; onChanged: () => void }) {
  const notify = useNotify()
  const [starting, setStarting] = useState(false)
  const s = info.install
  const running = s.state === 'running'
  const percent = installPercent(s.step, s.total, s.downloaded_gb, s.models_gb)

  const retry = async () => {
    setStarting(true)
    try {
      await startFun5bInstall()
      onChanged()
    } catch (err) {
      notify.error(err instanceof ApiError ? err.message : 'Không bắt đầu cài được.')
    } finally {
      setStarting(false)
    }
  }

  return (
    <Card className="!p-0">
      <CardHeader icon={Download} title="Cài đặt Fun-5B" />
      <div className="space-y-4 p-5 text-sm">
        {running && (
          <>
            <p className="text-slate-600 dark:text-slate-300">
              Máy đang tự cài Fun-5B ở chế độ nền (môi trường Python và ~{Math.round(s.models_gb)} GB model). Vẫn dùng
              các chức năng khác bình thường; trang này tự mở form khi cài xong.
            </p>
            <div>
              <div className="mb-1 flex items-center justify-between gap-3 text-xs">
                <span className="flex min-w-0 items-center gap-1.5 font-medium text-slate-700 dark:text-slate-200">
                  <Spinner className="h-3.5 w-3.5 shrink-0" />
                  <span className="truncate">Bước {s.step}/{s.total}: {s.label}</span>
                </span>
                <span className="shrink-0 font-mono text-brand-600 dark:text-brand-400">{percent}%</span>
              </div>
              <div className="h-2 overflow-hidden rounded-full bg-slate-100 dark:bg-slate-800">
                <div className="h-full rounded-full bg-brand-500 transition-[width] duration-700" style={{ width: `${percent}%` }} />
              </div>
              {s.downloaded_gb != null && (
                <p className="mt-1.5 text-xs text-slate-500">
                  Đã tải {s.downloaded_gb.toFixed(1)} / {s.models_gb.toFixed(0)} GB model
                </p>
              )}
            </div>
          </>
        )}

        {s.state === 'skipped' && (
          <p className="flex items-start gap-2 text-slate-600 dark:text-slate-300">
            <Info className="mt-0.5 h-4 w-4 shrink-0 text-slate-400" /> {s.message ?? info.reason}
          </p>
        )}

        {(s.state === 'failed' || s.state === 'not_installed') && (
          <>
            <p className="flex items-start gap-2 text-amber-700 dark:text-amber-400">
              <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
              {s.state === 'failed' ? s.message ?? 'Cài đặt bị lỗi.' : info.reason ?? 'Chưa cài Fun-5B.'}
            </p>
            {s.state === 'failed' && (
              <p className="text-xs text-slate-500">
                Phần đã tải được giữ lại — cài lại sẽ chạy tiếp từ chỗ dừng.
              </p>
            )}
            <Button variant="primary" loading={starting} onClick={retry}>
              <RotateCcw className="h-3.5 w-3.5" /> {s.state === 'failed' ? 'Cài lại' : 'Cài đặt ngay'}
            </Button>
          </>
        )}

        {s.state === 'installed' && !info.available && (
          <p className="flex items-start gap-2 text-amber-700 dark:text-amber-400">
            <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" /> {info.reason}
          </p>
        )}
      </div>
    </Card>
  )
}
