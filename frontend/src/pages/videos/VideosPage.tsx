import { useEffect, useState } from 'react'
import { Download, RefreshCw, Trash2, Video as VideoIcon, List } from 'lucide-react'
import { Card } from '../../components/Card'
import { CardHeader, IconAction } from '../../components/CardHeader'
import { Button } from '../../components/Button'
import { Spinner } from '../../components/Spinner'
import { useNotify } from '../../components/notifications/NotificationProvider'
import { useConfirm } from '../../components/ConfirmDialog'
import { deleteVideo, downloadUrl, listVideos, streamUrl } from '../../api/videos'
import { ApiError } from '../../api/client'
import type { VideoOut } from '../../types/api'
import { formatDateTime, formatMb } from '../../utils/format'

export default function VideosPage() {
  const notify = useNotify()
  const confirm = useConfirm()
  const [videos, setVideos] = useState<VideoOut[]>([])
  const [loading, setLoading] = useState(true)
  const [selected, setSelected] = useState<string | null>(null)
  const [deleting, setDeleting] = useState<string | null>(null)

  const load = async (selectNewest = false) => {
    setLoading(true)
    try {
      const rows = await listVideos()
      const sorted = [...rows].sort((a, b) => (a.rendered_at < b.rendered_at ? 1 : -1))
      setVideos(sorted)
      if (selectNewest && sorted.length > 0) setSelected(sorted[0].name)
    } catch (err) {
      notify.error(err instanceof ApiError ? err.message : 'Không tải được danh sách video.')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    void load(true)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const selectedVideo = videos.find((v) => v.name === selected) ?? null

  const handleDelete = async (name: string) => {
    const ok = await confirm({ message: `Xóa video "${name}"? Không thể hoàn tác.` })
    if (!ok) return
    setDeleting(name)
    try {
      await deleteVideo(name)
      notify.success(`Đã xóa "${name}".`)
      if (selected === name) setSelected(null)
      await load()
    } catch (err) {
      notify.error(err instanceof ApiError ? err.message : 'Xóa video thất bại.')
    } finally {
      setDeleting(null)
    }
  }

  return (
    <div className="grid h-full grid-cols-1 lg:grid-cols-[360px_1fr] gap-4">
      <Card className="!p-0 flex h-full flex-col overflow-hidden">
        <CardHeader
          icon={List}
          title={`Video đã render (${videos.length})`}
          actions={
            <IconAction onClick={() => load()} title="Làm mới">
              <RefreshCw className="h-3.5 w-3.5" />
            </IconAction>
          }
        />
        {loading && videos.length === 0 && (
          <div className="flex items-center justify-center gap-2 p-8 text-sm text-slate-500">
            <Spinner /> Đang tải…
          </div>
        )}
        {!loading && videos.length === 0 && (
          <p className="p-6 text-sm text-slate-500">Chưa có video nào được render.</p>
        )}
        <ul className="min-h-0 flex-1 divide-y divide-slate-100 dark:divide-slate-800 overflow-y-auto pb-2">
          {videos.map((v) => (
            <li key={v.name}>
              <button
                type="button"
                onClick={() => setSelected(v.name)}
                className={`flex w-full flex-col gap-0.5 px-4 py-2.5 text-left text-sm transition-colors ${
                  selected === v.name
                    ? 'bg-brand-50 dark:bg-brand-500/10'
                    : 'hover:bg-slate-50 dark:hover:bg-slate-800'
                }`}
              >
                <span className="truncate font-medium text-slate-800 dark:text-slate-200">{v.name}</span>
                <span className="text-xs text-slate-500">
                  {formatDateTime(v.rendered_at)} · {formatMb(v.size_mb)}
                </span>
              </button>
            </li>
          ))}
        </ul>
      </Card>
      <Card className="!p-0 overflow-hidden">
        <CardHeader icon={VideoIcon} title={selectedVideo?.name ?? 'Xem trước'} />
        <div className="p-5">
          {!selectedVideo && (
            <p className="text-sm text-slate-500">Chọn một video ở danh sách bên trái để xem trước.</p>
          )}
          {selectedVideo && (
            <div>
              <video
                key={selectedVideo.name}
                src={streamUrl(selectedVideo.name)}
                controls
                className="w-full rounded-xl bg-black max-h-[60vh]"
              />
              <div className="mt-3 flex items-center justify-between">
                <span className="text-xs text-slate-500">
                  Render lúc {formatDateTime(selectedVideo.rendered_at)} · {formatMb(selectedVideo.size_mb)}
                </span>
                <div className="flex gap-2">
                  <a href={downloadUrl(selectedVideo.name)} download>
                    <Button variant="secondary">
                      <Download className="h-3.5 w-3.5" /> Tải xuống
                    </Button>
                  </a>
                  <Button
                    variant="danger"
                    loading={deleting === selectedVideo.name}
                    onClick={() => handleDelete(selectedVideo.name)}
                  >
                    <Trash2 className="h-3.5 w-3.5" /> Xóa
                  </Button>
                </div>
              </div>
            </div>
          )}
        </div>
      </Card>
    </div>
  )
}
