import { Fragment, useCallback, useMemo, useState } from 'react'
import { CheckCircle2, Clock, FileSpreadsheet, FileText, Loader2, RefreshCcw, RotateCcw, XCircle, type LucideIcon } from 'lucide-react'
import { Card } from '../../components/Card'
import { CardHeader, LinkAction } from '../../components/CardHeader'
import { Badge } from '../../components/Badge'
import { groupByExcelFile } from '../../utils/groupByExcelFile'

type Tone = 'neutral' | 'success' | 'warning' | 'error' | 'brand'

/** Backend labels carry their own emoji (e.g. "✅ xong") — strip it and map to a themed icon instead. */
function statusMeta(label: string): { text: string; tone: Tone; icon: LucideIcon } {
  const text = label.replace(/^\S+\s*/u, '')
  if (label.includes('lỗi cứng')) return { text, tone: 'error', icon: XCircle }
  if (label.includes('xong')) return { text, tone: 'success', icon: CheckCircle2 }
  if (label.includes('dead-letter')) return { text, tone: 'warning', icon: RefreshCcw }
  if (label.includes('đang chạy')) return { text, tone: 'brand', icon: Loader2 }
  if (label.includes('chờ retry')) return { text, tone: 'warning', icon: RotateCcw }
  return { text, tone: 'neutral', icon: Clock }
}
import { Spinner } from '../../components/Spinner'
import { useNotify } from '../../components/notifications/NotificationProvider'
import { usePolling } from '../../hooks/usePolling'
import { requeueByIds, requeueDeadLetter, ttsStatus } from '../../api/tts'
import { ApiError } from '../../api/client'
import { TtsRetryModal } from './TtsRetryModal'
import type { TtsJobRow, TtsStatusResponse } from '../../types/api'

const isFailed = (j: TtsJobRow) => j.status === 'failed_retryable' || j.status === 'failed_permanent'

export function ExcelTtsStatusTab() {
  const notify = useNotify()
  const [status, setStatus] = useState<TtsStatusResponse | null>(null)
  const [requeuing, setRequeuing] = useState(false)
  const [busyKey, setBusyKey] = useState<string | null>(null)
  const [retryModalOpen, setRetryModalOpen] = useState(false)
  const [retrySubmitting, setRetrySubmitting] = useState(false)

  const load = useCallback(async () => {
    try {
      setStatus(await ttsStatus())
    } catch {
      // silent — this polls quietly in the background
    }
  }, [])

  usePolling(load, 6000)

  const deadLetterCount = status?.dead_letter.length ?? 0
  const groups = useMemo(() => groupByExcelFile(status?.jobs ?? []), [status])
  const showGroupHeaders = groups.length > 0
  const failedJobs = useMemo(() => (status?.jobs ?? []).filter(isFailed), [status])

  const handleRequeue = async () => {
    setRequeuing(true)
    try {
      const res = await requeueDeadLetter()
      notify.success(res.message)
      await load()
    } catch (err) {
      notify.error(err instanceof ApiError ? err.message : 'Không requeue được.')
    } finally {
      setRequeuing(false)
    }
  }

  const handleRetryFile = async (group: { key: string; label: string; items: TtsJobRow[] }) => {
    const ids = group.items.filter(isFailed).map((j) => j.id)
    setBusyKey(group.key)
    try {
      const res = await requeueByIds(ids)
      notify.success(res.message)
      await load()
    } catch (err) {
      notify.error(err instanceof ApiError ? err.message : `Không retry được file "${group.label}".`)
    } finally {
      setBusyKey(null)
    }
  }

  const handleRetrySelected = async (ids: number[]) => {
    setRetrySubmitting(true)
    try {
      const res = await requeueByIds(ids)
      notify.success(res.message)
      await load()
      setRetryModalOpen(false)
    } catch (err) {
      notify.error(err instanceof ApiError ? err.message : 'Không retry được các dòng đã chọn.')
    } finally {
      setRetrySubmitting(false)
    }
  }

  return (
    <Card className="!p-0 flex h-full flex-col overflow-hidden">
      <CardHeader
        icon={FileText}
        title="Trạng thái TTS queue"
        actions={
          <>
            {failedJobs.length > 0 && (
              <LinkAction onClick={() => setRetryModalOpen(true)}>
                <RotateCcw className="h-3.5 w-3.5" /> Thử lại nhiều dòng ({failedJobs.length})
              </LinkAction>
            )}
            {deadLetterCount > 0 && (
              <LinkAction onClick={handleRequeue} disabled={requeuing}>
                <RotateCcw className="h-3.5 w-3.5" /> Retry lỗi cứng ({deadLetterCount})
              </LinkAction>
            )}
          </>
        }
      />
      <div className="flex min-h-0 flex-1 flex-col p-5">
        {!status && (
          <div className="flex justify-center py-8">
            <Spinner />
          </div>
        )}
        {status && (
          <>
            <div className="mb-4 flex shrink-0 flex-wrap gap-2">
              {Object.entries(status.counts).map(([label, count]) => {
                const meta = statusMeta(label)
                return (
                  <Badge key={label} tone={meta.tone} icon={meta.icon} spin={meta.icon === Loader2}>
                    {meta.text}: {count}
                  </Badge>
                )
              })}
            </div>
            <div className="min-h-0 flex-1 overflow-y-auto overflow-x-auto">
              <table className="w-full text-left text-sm border-separate border-spacing-0">
                <thead>
                  <tr>
                    {[
                      { label: 'Dòng' },
                      { label: 'Sản phẩm', hideBelowSm: true },
                      { label: 'Nội dung' },
                      { label: 'Trạng thái' },
                      { label: 'Lỗi', hideBelowSm: true },
                    ].map(({ label, hideBelowSm }) => (
                      <th
                        key={label}
                        className={`sticky top-0 z-10 whitespace-nowrap border-b border-slate-200 dark:border-slate-800 bg-white dark:bg-slate-900 py-2 pr-4 text-xs font-normal uppercase tracking-wide text-slate-500 ${hideBelowSm ? 'hidden sm:table-cell' : ''}`}
                      >
                        {label}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-100 dark:divide-slate-800">
                  {status.jobs.length === 0 && (
                    <tr>
                      <td colSpan={5} className="py-8 text-center text-slate-400">
                        Chưa có job TTS nào
                      </td>
                    </tr>
                  )}
                  {groups.map((group) => {
                    const fileFailedCount = group.items.filter(isFailed).length
                    return (
                    <Fragment key={group.key}>
                      {showGroupHeaders && (
                        <tr className="bg-slate-50 dark:bg-slate-900/80">
                          <td colSpan={5} className="py-1.5 pr-4">
                            <div className="flex flex-col items-start gap-1 sm:flex-row sm:items-center sm:justify-between">
                              <span className="inline-flex items-center gap-1.5 text-xs font-medium text-slate-500">
                                <FileSpreadsheet className="h-3.5 w-3.5 shrink-0" /> {group.label} ({group.items.length})
                              </span>
                              {fileFailedCount > 0 && (
                                <button
                                  disabled={busyKey === group.key}
                                  onClick={() => handleRetryFile(group)}
                                  title={`Thử lại cả file (${fileFailedCount})`}
                                  className="rounded-lg p-1.5 text-emerald-600 hover:bg-emerald-50 dark:text-emerald-400 dark:hover:bg-emerald-950"
                                >
                                  <RotateCcw className="h-3.5 w-3.5" />
                                </button>
                              )}
                            </div>
                          </td>
                        </tr>
                      )}
                      {group.items.map((j) => (
                        <tr key={j.id}>
                          <td className="py-1.5 pr-4 text-xs text-slate-500">#{j.row}</td>
                          <td className="hidden max-w-[160px] truncate py-1.5 pr-4 sm:table-cell">{j.product || '(chung)'}</td>
                          <td className="max-w-[140px] truncate py-1.5 pr-4 text-slate-500 sm:max-w-[280px]">{j.text}</td>
                          <td className="py-1.5 pr-4 text-xs">
                            {(() => {
                              const meta = statusMeta(j.status_label)
                              return (
                                <Badge tone={meta.tone} icon={meta.icon} spin={meta.icon === Loader2}>
                                  {meta.text}
                                </Badge>
                              )
                            })()}
                          </td>
                          <td className="hidden max-w-[160px] truncate py-1.5 pr-4 text-xs text-rose-600 dark:text-rose-400 sm:table-cell">
                            {j.error ?? '—'}
                          </td>
                        </tr>
                      ))}
                    </Fragment>
                    )
                  })}
                </tbody>
              </table>
            </div>
          </>
        )}
      </div>

      {retryModalOpen && (
        <TtsRetryModal
          jobs={failedJobs}
          submitting={retrySubmitting}
          onClose={() => setRetryModalOpen(false)}
          onConfirm={handleRetrySelected}
        />
      )}
    </Card>
  )
}
