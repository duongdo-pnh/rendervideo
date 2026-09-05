import { useEffect, useState } from 'react'
import { ChevronRight, Cloud, ExternalLink, Folder, KeyRound, RefreshCw, Save } from 'lucide-react'
import { Card } from '../../components/Card'
import { CardHeader, IconAction } from '../../components/CardHeader'
import { Checkbox } from '../../components/form/Checkbox'
import { Button } from '../../components/Button'
import { Spinner } from '../../components/Spinner'
import { useNotify } from '../../components/notifications/NotificationProvider'
import { usePolling } from '../../hooks/usePolling'
import { driveFiles, driveReauth, driveReauthStatus, driveStatus, saveDriveConfig } from '../../api/drive'
import { ApiError } from '../../api/client'
import { formatBytes, formatDateTime } from '../../utils/format'
import type { DriveFilesResponse, DriveStatus } from '../../types/api'

export default function DrivePage() {
  const notify = useNotify()
  const [status, setStatus] = useState<DriveStatus | null>(null)
  const [folderId, setFolderId] = useState('')
  const [enabled, setEnabled] = useState(true)
  const [files, setFiles] = useState<DriveFilesResponse | null>(null)
  const [loadingFiles, setLoadingFiles] = useState(false)
  const [saving, setSaving] = useState(false)
  const [reauthing, setReauthing] = useState(false)
  const [pathStack, setPathStack] = useState<{ id?: string; name: string }[]>([])

  const loadStatus = async () => {
    try {
      const s = await driveStatus()
      setStatus(s)
      setFolderId(s.folder_id ?? '')
      setEnabled(s.enabled)
    } catch (err) {
      notify.error(err instanceof ApiError ? err.message : 'Không tải được trạng thái Drive.')
    }
  }

  const loadFiles = async (folder?: string) => {
    setLoadingFiles(true)
    try {
      const res = await driveFiles(folder)
      setFiles(res)
      if (!folder) setPathStack([{ id: undefined, name: res.folder_name }])
    } catch (err) {
      notify.error(err instanceof ApiError ? err.message : 'Không tải được danh sách file Drive.')
    } finally {
      setLoadingFiles(false)
    }
  }

  const openFolder = async (id: string, name: string) => {
    setLoadingFiles(true)
    try {
      setFiles(await driveFiles(id))
      setPathStack((prev) => [...prev, { id, name }])
    } catch (err) {
      notify.error(err instanceof ApiError ? err.message : 'Không tải được danh sách file Drive.')
    } finally {
      setLoadingFiles(false)
    }
  }

  const openBreadcrumb = async (index: number) => {
    const entry = pathStack[index]
    setLoadingFiles(true)
    try {
      setFiles(await driveFiles(entry.id))
      setPathStack((prev) => prev.slice(0, index + 1))
    } catch (err) {
      notify.error(err instanceof ApiError ? err.message : 'Không tải được danh sách file Drive.')
    } finally {
      setLoadingFiles(false)
    }
  }

  useEffect(() => {
    void loadStatus()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  useEffect(() => {
    if (status?.configured) void loadFiles()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [status?.configured])

  usePolling(
    async () => {
      const r = await driveReauthStatus()
      if (r.status !== 'pending') {
        setReauthing(false)
        if (r.status === 'success') {
          notify.success('Đã lấy token Drive mới.')
          void loadStatus()
        } else if (r.status === 'error') {
          notify.error(r.message ?? 'Lấy token Drive thất bại.')
        }
      }
    },
    2000,
    reauthing,
  )

  const save = async () => {
    setSaving(true)
    try {
      const s = await saveDriveConfig({ folder_id: folderId, enabled })
      setStatus(s)
      if (s.error) notify.error(s.error)
      else notify.success('Đã lưu cấu hình Drive.')
    } catch (err) {
      notify.error(err instanceof ApiError ? err.message : 'Không lưu được cấu hình.')
    } finally {
      setSaving(false)
    }
  }

  const reauth = async () => {
    try {
      await driveReauth()
      setReauthing(true)
      notify.success('Đã mở trình duyệt để đăng nhập lại Drive — chờ hoàn tất…')
    } catch (err) {
      notify.error(err instanceof ApiError ? err.message : 'Không bắt đầu được reauth.')
    }
  }

  return (
    <Card className="!p-0 flex h-full flex-col overflow-hidden">
      <CardHeader
        icon={Cloud}
        title="Google Drive"
        actions={
          <>
            {status?.folder_link && (
              <a href={status.folder_link} target="_blank" rel="noreferrer">
                <IconAction title="Mở trong Drive">
                  <ExternalLink className="h-3.5 w-3.5" />
                </IconAction>
              </a>
            )}
            <IconAction onClick={() => loadFiles(pathStack.at(-1)?.id)} title="Làm mới">
              <RefreshCw className="h-3.5 w-3.5" />
            </IconAction>
            <IconAction onClick={reauth} title="Lấy token Drive mới" disabled={reauthing}>
              <KeyRound className="h-3.5 w-3.5" />
            </IconAction>
          </>
        }
      />
      <div className="shrink-0 space-y-3 p-5">
        <div className="flex flex-wrap items-center gap-3">
          <input
            className="min-w-0 flex-1 rounded-lg border border-slate-300 dark:border-slate-700 bg-white dark:bg-slate-900 px-3 py-1.5 text-sm"
            placeholder="Dán ID hoặc link thư mục Drive (drive.google.com/drive/folders/...)"
            value={folderId}
            onChange={(e) => setFolderId(e.target.value)}
          />
          <Checkbox label="Bật upload" checked={enabled} onChange={setEnabled} />
          <Button variant="primary" loading={saving} onClick={save}>
            <Save className="h-3.5 w-3.5" /> Lưu
          </Button>
        </div>
        {status?.error && <p className="text-sm text-rose-600 dark:text-rose-400">{status.error}</p>}
        {!status?.configured && !status?.error && <p className="text-sm text-slate-500">Drive chưa được cấu hình.</p>}
        {status?.configured && pathStack.length > 0 && (
          <div className="flex flex-wrap items-center gap-1 text-sm text-slate-500">
            {pathStack.map((seg, i) => (
              <span key={i} className="flex items-center gap-1">
                {i > 0 && <ChevronRight className="h-3.5 w-3.5 text-slate-300 dark:text-slate-700" />}
                {i === pathStack.length - 1 ? (
                  <span className="font-medium text-slate-700 dark:text-slate-300">{seg.name}</span>
                ) : (
                  <button onClick={() => openBreadcrumb(i)} className="hover:underline hover:text-brand-600 dark:hover:text-brand-400">
                    {seg.name}
                  </button>
                )}
              </span>
            ))}
          </div>
        )}
      </div>
      {status?.configured && (
        <div className="min-h-0 flex-1 overflow-y-auto overflow-x-auto pb-2 snap-y snap-mandatory">
          <table className="w-full text-left text-sm border-separate border-spacing-0">
            <thead>
              <tr>
                {['Tên', 'Kích thước', 'Sửa lúc'].map((label) => (
                  <th
                    key={label}
                    className="sticky top-0 z-10 whitespace-nowrap border-b border-t border-slate-100 dark:border-slate-800 bg-white dark:bg-slate-900 px-4 py-2.5 text-xs font-normal uppercase tracking-wide text-slate-500"
                  >
                    {label}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100 dark:divide-slate-800">
              {loadingFiles && (
                <tr>
                  <td colSpan={3} className="px-4 py-10 text-center text-slate-400">
                    <Spinner className="mx-auto h-5 w-5" />
                  </td>
                </tr>
              )}
              {!loadingFiles && files?.files.length === 0 && (
                <tr>
                  <td colSpan={3} className="px-4 py-10 text-center text-slate-400">
                    Thư mục trống
                  </td>
                </tr>
              )}
              {!loadingFiles && files?.files.map((f) => (
                <tr key={f.id} className="snap-start scroll-mt-10 hover:bg-slate-50 dark:hover:bg-slate-900/60">
                  <td className="px-4 py-2">
                    {f.is_folder ? (
                      <button
                        onClick={() => openFolder(f.id, f.name)}
                        className="inline-flex items-center gap-1.5 text-brand-600 hover:underline dark:text-brand-400"
                      >
                        <Folder className="h-3.5 w-3.5" /> {f.name}
                      </button>
                    ) : f.web_view_link ? (
                      <a href={f.web_view_link} target="_blank" rel="noreferrer" className="hover:underline">
                        {f.name}
                      </a>
                    ) : (
                      f.name
                    )}
                  </td>
                  <td className="px-4 py-2 text-xs text-slate-500">{f.size ? formatBytes(f.size) : '—'}</td>
                  <td className="whitespace-nowrap px-4 py-2 text-xs text-slate-500">{formatDateTime(f.modified_time)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Card>
  )
}
