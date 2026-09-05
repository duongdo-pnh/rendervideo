import { Fragment, useEffect, useMemo, useState } from 'react'
import { Ban, Cloud, CloudOff, FileSpreadsheet, ListChecks, RotateCcw, Trash2, X, Zap } from 'lucide-react'
import { Card } from '../../components/Card'
import { CardHeader, LinkAction } from '../../components/CardHeader'
import { useAppDispatch, useAppSelector } from '../../store/hooks'
import { snapshotReceived } from '../../store/slices/jobsSlice'
import { useNotify } from '../../components/notifications/NotificationProvider'
import { useConfirm } from '../../components/ConfirmDialog'
import {
  bumpPriority,
  cancelAll,
  cancelJob,
  clearJobsByStatus,
  deleteJob,
  listJobs,
  priorityChoices as fetchPriorityChoices,
  reorderPriority,
  retryJob,
} from '../../api/jobs'
import { ApiError } from '../../api/client'
import { streamUrl, downloadUrl } from '../../api/videos'
import { formatDateTimeParts, formatDuration } from '../../utils/format'
import { STATUS_ICON, STATUS_LABEL, STATUS_SPIN, STATUS_TONE } from './statusLabels'
import { PriorityModal } from './PriorityModal'
import { Badge } from '../../components/Badge'
import { groupByExcelFile, type ExcelFileGroup } from '../../utils/groupByExcelFile'
import type { JobOut } from '../../types/api'

function renderSeconds(job: JobOut): number | null {
  if (!job.started_at) return null
  const end = job.finished_at ? new Date(job.finished_at).getTime() : Date.now()
  const start = new Date(job.started_at).getTime()
  if (Number.isNaN(end) || Number.isNaN(start)) return null
  return (end - start) / 1000
}

export function QueueStatusTab() {
  const dispatch = useAppDispatch()
  const notify = useNotify()
  const confirm = useConfirm()
  const jobs = useAppSelector((s) => s.jobs.jobs)
  const wsStatus = useAppSelector((s) => s.jobs.wsStatus)
  const priorityChoicesList = useAppSelector((s) => s.jobs.priorityChoices)
  const [previewJob, setPreviewJob] = useState<JobOut | null>(null)
  const [busyId, setBusyId] = useState<number | null>(null)
  const [busyGroupKey, setBusyGroupKey] = useState<string | null>(null)
  const [priorityOpen, setPriorityOpen] = useState(false)

  useEffect(() => {
    listJobs()
      .then((jobsList) =>
        fetchPriorityChoices().then((priority_choices) =>
          dispatch(snapshotReceived({ jobs: jobsList, priority_choices })),
        ),
      )
      .catch(() => {
        // websocket will populate shortly regardless
      })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const activeCount = useMemo(
    () => jobs.filter((j) => j.status === 'queued' || j.status === 'rendering').length,
    [jobs],
  )
  const groups = useMemo(() => groupByExcelFile(jobs), [jobs])
  const showGroupHeaders = groups.length > 0

  const withBusy = async (id: number, fn: () => Promise<void>) => {
    setBusyId(id)
    try {
      await fn()
    } catch (err) {
      notify.error(err instanceof ApiError ? err.message : 'Thao tác thất bại.')
    } finally {
      setBusyId(null)
    }
  }

  const handleBump = (id: number) =>
    withBusy(id, async () => {
      await bumpPriority(id)
      notify.success(`Đã ưu tiên job #${id}`)
    })

  const handleCancel = (job: JobOut) =>
    withBusy(job.id, async () => {
      const res = await cancelJob(job.id)
      notify.success(res.message)
    })

  const handleRetry = (id: number) =>
    withBusy(id, async () => {
      await retryJob(id)
      notify.success(`Đã đưa lại job #${id} vào hàng đợi.`)
    })

  const handleDelete = (job: JobOut) =>
    withBusy(job.id, async () => {
      const ok = await confirm({ message: `Xóa job #${job.id} · ${job.name}? Không thể hoàn tác.` })
      if (!ok) return
      const res = await deleteJob(job.id)
      notify.success(res.message)
      if (previewJob?.id === job.id) setPreviewJob(null)
    })

  const withGroupBusy = async (key: string, fn: () => Promise<void>) => {
    setBusyGroupKey(key)
    try {
      await fn()
    } catch (err) {
      notify.error(err instanceof ApiError ? err.message : 'Thao tác thất bại.')
    } finally {
      setBusyGroupKey(null)
    }
  }

  const handleGroupBump = (group: ExcelFileGroup<JobOut>) =>
    withGroupBusy(group.key, async () => {
      const targets = group.items.filter((j) => j.status === 'queued')
      await Promise.all(targets.map((j) => bumpPriority(j.id)))
      notify.success(`Đã ưu tiên ${targets.length} job trong "${group.label}".`)
    })

  const handleGroupCancel = (group: ExcelFileGroup<JobOut>) => async () => {
    const targets = group.items.filter((j) => j.status === 'queued' || j.status === 'rendering')
    const ok = await confirm({ message: `Hủy ${targets.length} job đang chờ/render trong "${group.label}"?` })
    if (!ok) return
    await withGroupBusy(group.key, async () => {
      await Promise.all(targets.map((j) => cancelJob(j.id)))
      notify.success(`Đã hủy ${targets.length} job trong "${group.label}".`)
    })
  }

  const handleGroupRetry = (group: ExcelFileGroup<JobOut>) =>
    withGroupBusy(group.key, async () => {
      const targets = group.items.filter((j) => j.status === 'failed')
      await Promise.all(targets.map((j) => retryJob(j.id)))
      notify.success(`Đã đưa lại ${targets.length} job trong "${group.label}" vào hàng đợi.`)
    })

  const handleGroupDelete = (group: ExcelFileGroup<JobOut>) => async () => {
    const targets = group.items.filter((j) => j.status !== 'rendering')
    const ok = await confirm({ message: `Xóa ${targets.length} job trong "${group.label}"? Không thể hoàn tác.` })
    if (!ok) return
    await withGroupBusy(group.key, async () => {
      await Promise.all(targets.map((j) => deleteJob(j.id)))
      notify.success(`Đã xóa ${targets.length} job trong "${group.label}".`)
      if (previewJob && targets.some((j) => j.id === previewJob.id)) setPreviewJob(null)
    })
  }

  const handleCancelAll = async () => {
    const ok = await confirm({ message: `Hủy ${activeCount} job đang chờ/render?` })
    if (!ok) return
    try {
      const res = await cancelAll()
      notify.success(res.message)
    } catch (err) {
      notify.error(err instanceof ApiError ? err.message : 'Không hủy được.')
    }
  }

  const handleClear = async (status: 'done' | 'failed') => {
    const label = status === 'done' ? STATUS_LABEL.done : STATUS_LABEL.failed
    const ok = await confirm({ message: `Xóa hết job ${label}? Không thể hoàn tác.` })
    if (!ok) return
    try {
      const res = await clearJobsByStatus(status)
      notify.success(res.message)
    } catch (err) {
      notify.error(err instanceof ApiError ? err.message : 'Không xóa được.')
    }
  }

  const applyOrder = async (order: number[]) => {
    try {
      await reorderPriority(order)
      notify.success('Đã lưu danh sách ưu tiên.')
      setPriorityOpen(false)
    } catch (err) {
      notify.error(err instanceof ApiError ? err.message : 'Không áp dụng được thứ tự ưu tiên.')
    }
  }

  return (
    <div className="flex h-full flex-col gap-4">
      <Card className="!p-0 flex min-h-0 flex-1 flex-col overflow-hidden">
        <CardHeader
          icon={ListChecks}
          title="Trạng thái queue"
          actions={
            <>
              <LinkAction onClick={() => setPriorityOpen(true)}>
                <ListChecks className="h-3.5 w-3.5" /> Ưu tiên job ({priorityChoicesList.length})
              </LinkAction>
              <span className="inline-flex items-center gap-1.5 rounded-lg bg-emerald-50 dark:bg-emerald-950 px-2 py-1 text-xs font-medium text-emerald-700 dark:text-emerald-400">
                <span className={`h-1.5 w-1.5 rounded-full ${wsStatus === 'connected' ? 'bg-emerald-500' : 'bg-slate-400'}`} />
                {wsStatus === 'connected' ? 'Live' : 'Offline'}
              </span>
            </>
          }
        />
        <div className="min-h-0 flex-1 overflow-y-auto overflow-x-auto">
          <table className="w-full text-left text-sm border-separate border-spacing-0">
            <thead>
              <tr>
                {[
                  { label: 'ID' },
                  { label: 'Tên' },
                  { label: 'Trạng thái' },
                  { label: 'Engine', tier: 'min-[1900px]:table-cell' },
                  { label: 'Model', tier: 'min-[1900px]:table-cell' },
                  { label: 'Ưu tiên', tier: 'min-[1900px]:table-cell' },
                  { label: 'Thời gian render', tier: 'xl:table-cell' },
                  { label: 'Retry', tier: 'min-[1900px]:table-cell' },
                  { label: 'Ngày tạo', tier: 'xl:table-cell' },
                  { label: 'Lỗi', tier: 'xl:table-cell' },
                  { label: 'Drive', tier: 'xl:table-cell' },
                  { label: 'Thao tác' },
                ].map(({ label, tier }) => (
                  <th
                    key={label}
                    className={`sticky top-0 z-10 whitespace-nowrap border-b border-slate-200 dark:border-slate-800 bg-white dark:bg-slate-900 px-2 py-2.5 text-xs font-normal uppercase tracking-wide text-slate-500 sm:px-4 ${label === 'Tên' ? 'w-full' : ''} ${tier ? `hidden ${tier}` : ''}`}
                  >
                    {label}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100 dark:divide-slate-800">
              {jobs.length === 0 && (
                <tr>
                  <td colSpan={12} className="px-4 py-10 text-center text-slate-400">
                    Chưa có job nào
                  </td>
                </tr>
              )}
              {groups.map((group) => {
                const groupQueuedCount = group.items.filter((j) => j.status === 'queued').length
                const groupActiveCount = group.items.filter((j) => j.status === 'queued' || j.status === 'rendering').length
                const groupFailedCount = group.items.filter((j) => j.status === 'failed').length
                const groupDeletableCount = group.items.filter((j) => j.status !== 'rendering').length
                const groupBusy = busyGroupKey === group.key
                return (
                <Fragment key={group.key}>
                  {showGroupHeaders && (
                    <tr className="bg-slate-50 dark:bg-slate-900/80">
                      <td colSpan={12} className="px-2 py-1.5 sm:px-4">
                        <div className="flex flex-col items-start gap-1 sm:flex-row sm:items-center sm:justify-between sm:gap-x-4">
                          <span className="inline-flex items-center gap-1.5 text-xs font-medium text-slate-500">
                            <FileSpreadsheet className="h-3.5 w-3.5 shrink-0" /> {group.label} ({group.items.length})
                          </span>
                          <div className="flex items-center gap-0.5">
                            {groupQueuedCount > 0 && (
                              <button
                                disabled={groupBusy}
                                onClick={() => handleGroupBump(group)}
                                title={`Ưu tiên cả cụm (${groupQueuedCount})`}
                                className="rounded-lg p-1.5 text-brand-600 hover:bg-brand-50 dark:text-brand-400 dark:hover:bg-brand-500/10"
                              >
                                <Zap className="h-3.5 w-3.5" />
                              </button>
                            )}
                            {groupActiveCount > 0 && (
                              <button
                                disabled={groupBusy}
                                onClick={handleGroupCancel(group)}
                                title={`Hủy cả cụm (${groupActiveCount})`}
                                className="rounded-lg p-1.5 text-amber-600 hover:bg-amber-50 dark:text-amber-400 dark:hover:bg-amber-950"
                              >
                                <Ban className="h-3.5 w-3.5" />
                              </button>
                            )}
                            {groupFailedCount > 0 && (
                              <button
                                disabled={groupBusy}
                                onClick={() => handleGroupRetry(group)}
                                title={`Thử lại cả cụm (${groupFailedCount})`}
                                className="rounded-lg p-1.5 text-emerald-600 hover:bg-emerald-50 dark:text-emerald-400 dark:hover:bg-emerald-950"
                              >
                                <RotateCcw className="h-3.5 w-3.5" />
                              </button>
                            )}
                            {groupDeletableCount > 0 && (
                              <button
                                disabled={groupBusy}
                                onClick={handleGroupDelete(group)}
                                title={`Xóa cả cụm (${groupDeletableCount})`}
                                className="rounded-lg p-1.5 text-rose-600 hover:bg-rose-50 dark:text-rose-400 dark:hover:bg-rose-950"
                              >
                                <Trash2 className="h-3.5 w-3.5" />
                              </button>
                            )}
                          </div>
                        </div>
                      </td>
                    </tr>
                  )}
                  {group.items.map((job) => (
                    <tr key={job.id} className="hover:bg-slate-50 dark:hover:bg-slate-900/60">
                      <td className="px-2 py-2 font-mono text-xs text-slate-500 sm:px-4">#{job.id}</td>
                      <td className="max-w-[56px] truncate px-2 py-2 font-medium sm:max-w-[220px] sm:px-4">
                        {job.status === 'done' ? (
                          <button className="text-brand-600 hover:underline dark:text-brand-400" onClick={() => setPreviewJob(job)}>
                            {job.name}
                          </button>
                        ) : (
                          job.name
                        )}
                      </td>
                      <td className="px-2 py-2 sm:px-4">
                        <Badge tone={STATUS_TONE[job.status]} icon={STATUS_ICON[job.status]} spin={STATUS_SPIN[job.status]}>
                          {STATUS_LABEL[job.status]}
                        </Badge>
                      </td>
                      <td className="hidden px-4 py-2 text-xs text-slate-500 min-[1900px]:table-cell">{job.engine}</td>
                      <td className="hidden px-4 py-2 text-xs text-slate-500 min-[1900px]:table-cell">{job.model_res}</td>
                      <td className="hidden px-4 py-2 text-xs min-[1900px]:table-cell">{job.priority}</td>
                      <td className="hidden whitespace-nowrap px-4 py-2 text-xs xl:table-cell">
                        {formatDuration(renderSeconds(job), { inProgress: job.status === 'rendering' })}
                      </td>
                      <td className="hidden px-4 py-2 text-xs min-[1900px]:table-cell">{job.retries}</td>
                      <td className="hidden whitespace-nowrap px-4 py-2 text-xs text-slate-500 xl:table-cell">
                        {(() => {
                          const p = formatDateTimeParts(job.created_at)
                          return p ? (
                            <div className="leading-tight">
                              <div>{p.time}</div>
                              <div className="text-slate-400">{p.date}</div>
                            </div>
                          ) : (
                            '—'
                          )
                        })()}
                      </td>
                      <td className="hidden max-w-[160px] truncate px-4 py-2 text-xs text-rose-600 dark:text-rose-400 xl:table-cell" title={job.error ?? ''}>
                        {job.error ?? ''}
                      </td>
                      <td className="hidden px-4 py-2 xl:table-cell">
                        {job.drive_link ? (
                          <a href={job.drive_link} target="_blank" rel="noreferrer" className="text-brand-600 dark:text-brand-400">
                            <Cloud className="h-4 w-4" />
                          </a>
                        ) : (
                          <span className="text-slate-300 dark:text-slate-700" title={job.drive_error ?? ''}>
                            <CloudOff className="h-4 w-4" />
                          </span>
                        )}
                      </td>
                      <td className="px-0.5 py-2 sm:px-2">
                        <div className="flex items-center gap-0.5">
                          {job.status === 'queued' && (
                            <button
                              disabled={busyId === job.id}
                              onClick={() => handleBump(job.id)}
                              title="Ưu tiên"
                              className="rounded-lg p-1 sm:p-1.5 text-brand-600 hover:bg-brand-50 dark:text-brand-400 dark:hover:bg-brand-500/10"
                            >
                              <Zap className="h-3.5 w-3.5" />
                            </button>
                          )}
                          {(job.status === 'queued' || job.status === 'rendering') && (
                            <button
                              disabled={busyId === job.id}
                              onClick={() => handleCancel(job)}
                              title="Hủy"
                              className="rounded-lg p-1 sm:p-1.5 text-amber-600 hover:bg-amber-50 dark:text-amber-400 dark:hover:bg-amber-950"
                            >
                              <Ban className="h-3.5 w-3.5" />
                            </button>
                          )}
                          {job.status === 'failed' && (
                            <button
                              disabled={busyId === job.id}
                              onClick={() => handleRetry(job.id)}
                              title="Thử lại"
                              className="rounded-lg p-1 sm:p-1.5 text-emerald-600 hover:bg-emerald-50 dark:text-emerald-400 dark:hover:bg-emerald-950"
                            >
                              <RotateCcw className="h-3.5 w-3.5" />
                            </button>
                          )}
                          {job.status !== 'rendering' && (
                            <button
                              disabled={busyId === job.id}
                              onClick={() => handleDelete(job)}
                              title="Xóa"
                              className="rounded-lg p-1 sm:p-1.5 text-rose-600 hover:bg-rose-50 dark:text-rose-400 dark:hover:bg-rose-950"
                            >
                              <Trash2 className="h-3.5 w-3.5" />
                            </button>
                          )}
                        </div>
                      </td>
                    </tr>
                  ))}
                </Fragment>
                )
              })}
            </tbody>
          </table>
        </div>
        <div className="flex shrink-0 flex-wrap items-center justify-between gap-2 border-t border-slate-100 dark:border-slate-800 px-4 sm:px-5 py-3 text-sm">
          <div className="flex gap-4">
            <LinkAction onClick={() => handleClear('failed')}>Xóa hết job lỗi</LinkAction>
            <LinkAction onClick={() => handleClear('done')}>Xóa hết job xong</LinkAction>
          </div>
          {activeCount > 0 && (
            <LinkAction
              onClick={handleCancelAll}
              className="!text-rose-600 hover:!text-rose-700 dark:!text-rose-400"
            >
              <Ban className="h-3.5 w-3.5" /> Hủy {activeCount} đang chờ/render
            </LinkAction>
          )}
        </div>
      </Card>

      {previewJob && (
        <Card>
          <div className="mb-2 flex items-center justify-between">
            <span className="truncate text-sm font-medium">{previewJob.name}</span>
            <button onClick={() => setPreviewJob(null)} className="text-slate-400 hover:text-slate-600">
              <X className="h-4 w-4" />
            </button>
          </div>
          <video src={streamUrl(previewJob.name)} controls className="w-full max-h-[50vh] rounded-xl bg-black" />
          <a href={downloadUrl(previewJob.name)} download className="mt-2 inline-block text-xs text-brand-600 hover:underline dark:text-brand-400">
            Tải xuống
          </a>
        </Card>
      )}

      {priorityOpen && (
        <PriorityModal choices={priorityChoicesList} onClose={() => setPriorityOpen(false)} onApply={applyOrder} />
      )}
    </div>
  )
}
