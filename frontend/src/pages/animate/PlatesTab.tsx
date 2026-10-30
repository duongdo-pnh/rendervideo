import { useEffect, useState } from 'react'
import { Images, RefreshCw, Trash2 } from 'lucide-react'
import { Card } from '../../components/Card'
import { CardHeader, IconAction } from '../../components/CardHeader'
import { Spinner } from '../../components/Spinner'
import { useNotify } from '../../components/notifications/NotificationProvider'
import { useConfirm } from '../../components/ConfirmDialog'
import { deletePlate, listPlates, plateImageUrl } from '../../api/engines'
import { ApiError } from '../../api/client'
import { formatBytes, formatDateTime } from '../../utils/format'
import type { Fun5bPlate } from '../../types/api'

/** Clean backgrounds FireRed painted (fun5b_cache/plates): reused for the same character image. */
export function PlatesTab() {
  const notify = useNotify()
  const confirm = useConfirm()
  const [plates, setPlates] = useState<Fun5bPlate[]>([])
  const [loading, setLoading] = useState(true)

  const load = async () => {
    setLoading(true)
    try {
      setPlates(await listPlates())
    } catch (err) {
      notify.error(err instanceof ApiError ? err.message : 'Không tải được danh sách nền.')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    void load()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const remove = async (plate: Fun5bPlate) => {
    const ok = await confirm({
      message: `Xoá nền đã lưu${plate.source_name ? ` của "${plate.source_name}"` : ''}? Lần sau dùng ảnh này sẽ phải vẽ lại (~7 phút).`,
    })
    if (!ok) return
    try {
      notify.success((await deletePlate(plate.id)).message)
      await load()
    } catch (err) {
      notify.error(err instanceof ApiError ? err.message : 'Xoá thất bại.')
    }
  }

  const total = plates.reduce((sum, p) => sum + p.size_kb * 1024, 0)

  return (
    <Card className="!p-0 flex h-full flex-col overflow-hidden">
      <CardHeader
        icon={Images}
        title="Nền sạch đã lưu"
        actions={
          <>
            <span className="text-xs text-slate-500">
              {plates.length} nền · {formatBytes(total)}
            </span>
            <IconAction onClick={load} title="Làm mới">
              <RefreshCw className="h-3.5 w-3.5" />
            </IconAction>
          </>
        }
      />
      <div className="min-h-0 flex-1 overflow-y-auto p-4">
        {loading && <Spinner className="mx-auto my-10 h-5 w-5 text-slate-400" />}
        {!loading && plates.length === 0 && (
          <p className="py-10 text-center text-sm text-slate-400">
            Chưa có nền nào. Nền được vẽ tự động (FireRed) ở lần render đầu tiên với mỗi ảnh nhân vật.
          </p>
        )}
        <div className="grid grid-cols-2 gap-4 sm:grid-cols-3 lg:grid-cols-4 xl:grid-cols-6">
          {plates.map((p) => (
            <figure key={p.id} className="overflow-hidden rounded-xl border border-slate-200 dark:border-slate-800">
              <div className="relative bg-slate-100 dark:bg-slate-800">
                <img src={plateImageUrl(p.id)} alt="Nền sạch" title="Nền sạch" loading="lazy"
                  className="aspect-[640/1152] w-full object-cover" />
                {p.has_source && (
                  <img src={plateImageUrl(p.id, 'source')} alt="Ảnh gốc" title="Ảnh nhân vật gốc" loading="lazy"
                    className="absolute bottom-2 left-2 aspect-[640/1152] w-1/3 rounded-md border-2 border-white object-cover shadow-md dark:border-slate-900" />
                )}
              </div>
              <figcaption className="flex items-start justify-between gap-2 px-3 py-2">
                <div className="min-w-0 text-xs">
                  <div className="truncate font-medium text-slate-700 dark:text-slate-200">{p.source_name ?? p.id.slice(0, 10)}</div>
                  <div className="text-slate-400">Dùng: {formatDateTime(p.last_used)}</div>
                </div>
                <IconAction onClick={() => remove(p)} title="Xoá nền này" className="hover:!text-rose-600">
                  <Trash2 className="h-3.5 w-3.5" />
                </IconAction>
              </figcaption>
            </figure>
          ))}
        </div>
      </div>
    </Card>
  )
}
