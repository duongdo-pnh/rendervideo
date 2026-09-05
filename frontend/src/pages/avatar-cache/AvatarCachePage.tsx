import { useEffect, useState } from 'react'
import { AlertTriangle, CheckCircle2, FlaskConical, HelpCircle, RefreshCw, Sparkles, Trash2, UserRound, type LucideIcon } from 'lucide-react'
import { Card } from '../../components/Card'
import { CardHeader, IconAction, LinkAction } from '../../components/CardHeader'
import { Badge } from '../../components/Badge'
import { Spinner } from '../../components/Spinner'
import { useNotify } from '../../components/notifications/NotificationProvider'
import { useConfirm } from '../../components/ConfirmDialog'
import { clearStaleAvatarCache, deleteAvatarCache, listAvatarCache } from '../../api/avatars'
import { ApiError } from '../../api/client'
import { formatBytes, formatDateTime, formatDuration } from '../../utils/format'
import type { AvatarCacheEntry, AvatarCacheStatus } from '../../types/api'

const STATUS_LABEL: Record<AvatarCacheStatus, string> = {
  LIVE: 'LIVE',
  STALE: 'STALE',
  TEST: 'TEST',
  MISSING: 'MISSING',
}
const STATUS_TONE: Record<AvatarCacheStatus, 'success' | 'warning' | 'neutral' | 'error'> = {
  LIVE: 'success',
  STALE: 'warning',
  TEST: 'neutral',
  MISSING: 'error',
}
const STATUS_ICON: Record<AvatarCacheStatus, LucideIcon> = {
  LIVE: CheckCircle2,
  STALE: AlertTriangle,
  TEST: FlaskConical,
  MISSING: HelpCircle,
}

export default function AvatarCachePage() {
  const notify = useNotify()
  const confirm = useConfirm()
  const [entries, setEntries] = useState<AvatarCacheEntry[]>([])
  const [loading, setLoading] = useState(true)
  const [clearing, setClearing] = useState(false)

  const load = async () => {
    setLoading(true)
    try {
      setEntries(await listAvatarCache())
    } catch (err) {
      notify.error(err instanceof ApiError ? err.message : 'Không tải được avatar cache.')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    void load()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const totalBytes = entries.reduce((sum, e) => sum + e.size_bytes, 0)
  const staleEntries = entries.filter((e) => e.status === 'STALE')
  const staleBytes = staleEntries.reduce((sum, e) => sum + e.size_bytes, 0)

  const handleDelete = async (entry: AvatarCacheEntry) => {
    const ok = await confirm({ message: `Xóa avatar cache "${entry.video_name}"? Không thể hoàn tác.` })
    if (!ok) return
    try {
      await deleteAvatarCache(entry.avatar_id)
      notify.success('Đã xóa avatar cache.')
      await load()
    } catch (err) {
      notify.error(err instanceof ApiError ? err.message : 'Xóa thất bại.')
    }
  }

  const handleClearStale = async () => {
    if (staleEntries.length === 0) return
    const ok = await confirm({
      message: `Xóa ${staleEntries.length} avatar cache STALE (~${formatBytes(staleBytes)})? Không thể hoàn tác.`,
    })
    if (!ok) return
    setClearing(true)
    try {
      const res = await clearStaleAvatarCache()
      notify.success(res.message)
      await load()
    } catch (err) {
      notify.error(err instanceof ApiError ? err.message : 'Không xóa được.')
    } finally {
      setClearing(false)
    }
  }

  return (
    <Card className="!p-0 flex h-full flex-col overflow-hidden">
      <CardHeader
        icon={UserRound}
        title="Avatar Cache"
        actions={
          <>
            <span className="text-xs text-slate-500">{formatBytes(totalBytes)}</span>
            <IconAction onClick={load} title="Làm mới">
              <RefreshCw className="h-3.5 w-3.5" />
            </IconAction>
            <LinkAction onClick={handleClearStale} disabled={clearing || staleEntries.length === 0}>
              <Sparkles className="h-3.5 w-3.5" /> Xóa cache STALE
            </LinkAction>
          </>
        }
      />
      <div className="min-h-0 flex-1 overflow-y-auto overflow-x-auto pb-2 snap-y snap-mandatory">
        <table className="w-full text-left text-sm border-separate border-spacing-0">
          <thead>
            <tr>
              {[
                { label: 'Video' },
                { label: 'Nguồn', hideBelowMd: true },
                { label: 'Thời lượng', hideBelowMd: true },
                { label: 'Kích thước', hideBelowMd: true },
                { label: 'Trạng thái' },
                { label: 'Build lúc', hideBelowMd: true },
                { label: '' },
              ].map(({ label, hideBelowMd }) => (
                <th
                  key={label}
                  className={`sticky top-0 z-10 whitespace-nowrap border-b border-slate-200 dark:border-slate-800 bg-white dark:bg-slate-900 px-4 py-2.5 text-xs font-normal uppercase tracking-wide text-slate-500 ${hideBelowMd ? 'hidden md:table-cell' : ''}`}
                >
                  {label}
                </th>
              ))}
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-100 dark:divide-slate-800">
            {loading && (
              <tr>
                <td colSpan={7} className="px-4 py-10 text-center text-slate-400">
                  <Spinner className="mx-auto h-5 w-5" />
                </td>
              </tr>
            )}
            {!loading && entries.length === 0 && (
              <tr>
                <td colSpan={7} className="px-4 py-10 text-center text-slate-400">
                  Chưa có avatar nào được cache
                </td>
              </tr>
            )}
            {entries.map((e) => (
              <tr key={e.avatar_id} className="snap-start scroll-mt-10 hover:bg-slate-50 dark:hover:bg-slate-900/60">
                <td className="max-w-[140px] truncate px-4 py-2 font-medium sm:max-w-[220px]">{e.video_name}</td>
                <td className="hidden max-w-[180px] truncate px-4 py-2 text-xs text-slate-500 md:table-cell">{e.source_name ?? '—'}</td>
                <td className="hidden px-4 py-2 text-xs md:table-cell">{formatDuration(e.duration_secs)}</td>
                <td className="hidden px-4 py-2 text-xs md:table-cell">{formatBytes(e.size_bytes)}</td>
                <td className="px-4 py-2">
                  <Badge tone={STATUS_TONE[e.status]} icon={STATUS_ICON[e.status]}>
                    {STATUS_LABEL[e.status]}
                  </Badge>
                </td>
                <td className="hidden whitespace-nowrap px-4 py-2 text-xs text-slate-500 md:table-cell">{formatDateTime(e.built_at)}</td>
                <td className="px-4 py-2 text-right">
                  <button
                    onClick={() => handleDelete(e)}
                    title="Xóa"
                    className="rounded-lg p-1.5 text-rose-600 hover:bg-rose-50 dark:text-rose-400 dark:hover:bg-rose-950"
                  >
                    <Trash2 className="h-3.5 w-3.5" />
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Card>
  )
}
