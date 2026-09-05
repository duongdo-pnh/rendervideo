import { useState } from 'react'
import { Upload, FileText, ListChecks, Download, FileSpreadsheet, Rocket, XCircle } from 'lucide-react'
import { Card } from '../../components/Card'
import { CardHeader, LinkAction } from '../../components/CardHeader'
import { Button } from '../../components/Button'
import { NativeFileField } from '../../components/form/NativeFileField'
import { TextField } from '../../components/form/TextField'
import { SelectField } from '../../components/form/SelectField'
import { Spinner } from '../../components/Spinner'
import { useNotify } from '../../components/notifications/NotificationProvider'
import { downloadTemplate, exportResults, previewExcel, submitExcel } from '../../api/excel'
import { ApiError } from '../../api/client'
import { ExcelTtsStatusTab } from './ExcelTtsStatusTab'
import { ExcelRenderConfigModal, type ExcelRenderConfig } from './ExcelRenderConfigModal'
import type { ExcelPreviewResponse } from '../../types/api'

const TABS = [
  { key: 'import', label: 'Import Excel', icon: Upload },
  { key: 'queue', label: 'Trạng thái TTS queue', icon: FileText },
] as const

export default function ExcelPage() {
  const notify = useNotify()
  const [tab, setTab] = useState<'import' | 'queue'>('import')

  const [excelFile, setExcelFile] = useState<File | null>(null)
  const [avatarVideos, setAvatarVideos] = useState<File[]>([])
  const [shopeeId, setShopeeId] = useState('')
  const [defaultProvider, setDefaultProvider] = useState('')
  const [previewing, setPreviewing] = useState(false)
  const [submitting, setSubmitting] = useState(false)
  const [preview, setPreview] = useState<ExcelPreviewResponse | null>(null)
  const [lastBatchId, setLastBatchId] = useState<string | null>(null)
  const [configOpen, setConfigOpen] = useState(false)

  const handlePreview = async () => {
    if (!excelFile) {
      notify.error('Chọn file Excel trước.')
      return
    }
    setPreviewing(true)
    try {
      const form = new FormData()
      form.append('excel_file', excelFile)
      avatarVideos.forEach((f) => form.append('avatar_videos', f))
      if (defaultProvider) form.append('default_provider', defaultProvider)
      if (shopeeId) form.append('shopee_item_id', shopeeId)
      const res = await previewExcel(form)
      setPreview(res)
      notify.success(res.summary)
    } catch (err) {
      notify.error(err instanceof ApiError ? err.message : 'Xem trước thất bại.')
    } finally {
      setPreviewing(false)
    }
  }

  const handleSubmit = async (config: ExcelRenderConfig) => {
    if (!preview) return
    setSubmitting(true)
    try {
      const res = await submitExcel({ state: preview.state, ...config })
      setLastBatchId(res.batch_id)
      notify.success(res.message)
      setPreview(null)
      setExcelFile(null)
      setConfigOpen(false)
    } catch (err) {
      notify.error(err instanceof ApiError ? err.message : 'Submit thất bại.')
    } finally {
      setSubmitting(false)
    }
  }

  const columns = preview && preview.rows.length > 0 ? Object.keys(preview.rows[0]).slice(0, 6) : []
  const previewLines = preview
    ? [
        ...preview.rows.map((row) => ({ kind: 'row' as const, excelRow: row.row as number, row })),
        ...preview.errors.map((err) => ({ kind: 'error' as const, excelRow: err.row, error: err })),
      ].sort((a, b) => a.excelRow - b.excelRow)
    : []

  return (
    <div className="flex h-full flex-col">
      <div className="flex shrink-0 gap-1 overflow-x-auto border-b border-slate-200 dark:border-slate-800">
        {TABS.map((t) => (
          <button
            key={t.key}
            title={t.label}
            onClick={() => setTab(t.key)}
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

      <div className="flex min-h-0 flex-1 flex-col gap-4 pt-4">
      {tab === 'queue' ? (
        <ExcelTtsStatusTab />
      ) : (
        <>
          <Card className="!p-0 shrink-0 overflow-hidden">
            <CardHeader
              icon={FileSpreadsheet}
              title="Import Excel"
              actions={
                <>
                  <LinkAction onClick={() => setTab('queue')}>
                    <ListChecks className="h-3.5 w-3.5" /> Xem trạng thái queue
                  </LinkAction>
                  <LinkAction onClick={() => downloadTemplate()}>
                    <Download className="h-3.5 w-3.5" /> Tải template
                  </LinkAction>
                </>
              }
            />
            <div className="space-y-4 p-5">
              <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
                <NativeFileField
                  label="File Excel"
                  accept=".xlsx"
                  files={excelFile ? [excelFile] : []}
                  onChange={(files) => setExcelFile(files[0] ?? null)}
                />
                <div>
                  <NativeFileField
                    label="Kho avatar mẫu (nếu Excel không ghi video_path)"
                    accept="video/*"
                    multiple
                    files={avatarVideos}
                    onChange={setAvatarVideos}
                  />
                  <p className="mt-1 text-xs text-slate-400">
                    Chọn nhiều video avatar cùng lúc (giữ Ctrl/Shift khi chọn file) — đặt tên file avatar TRÙNG tên
                    sản phẩm trong Excel để hệ tự khớp đúng avatar cho đúng sản phẩm.
                  </p>
                </div>
              </div>
              <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
                <TextField
                  label="Shopee item_id (áp cho cả batch, tuỳ chọn)"
                  placeholder="vd: 23525384022"
                  value={shopeeId}
                  onChange={(e) => setShopeeId(e.target.value)}
                />
                <SelectField
                  label="TTS provider mặc định"
                  placeholder="(theo cấu hình hệ thống)"
                  value={defaultProvider}
                  onChange={setDefaultProvider}
                  options={[
                    { label: 'Vbee', value: 'vbee' },
                    { label: 'Audiosynclab', value: 'ausynclab' },
                    { label: 'Voice hệ thống', value: 'autovoice' },
                    { label: 'Local (offline)', value: 'local' },
                  ]}
                />
              </div>
              <Button variant="primary" loading={previewing} onClick={handlePreview}>
                <FileText className="h-3.5 w-3.5" /> Xem trước
              </Button>
            </div>
          </Card>

          <Card className="!p-0 flex min-h-0 flex-1 flex-col overflow-hidden">
            {!preview && (
              <div className="flex flex-1 flex-col items-center justify-center gap-2 text-slate-400">
                <FileSpreadsheet className="h-8 w-8" />
                <p className="text-sm">Chọn file Excel và bấm "Xem trước" để bắt đầu</p>
              </div>
            )}
            {preview && (
              <>
                <CardHeader
                  icon={ListChecks}
                  title="Kết quả xem trước"
                  actions={
                    <Button
                      variant="primary"
                      disabled={preview.rows.length === 0 || preview.errors.length > 0}
                      onClick={() => setConfigOpen(true)}
                    >
                      <Rocket className="h-3.5 w-3.5" /> Đưa {preview.rows.length} dòng vào TTS queue
                    </Button>
                  }
                />
                <div className="flex min-h-0 flex-1 flex-col p-5">
                  <p className="mb-3 shrink-0 text-sm text-slate-500">{preview.summary}</p>
                  {previewing ? (
                    <Spinner />
                  ) : (
                    <div className="min-h-0 flex-1 overflow-y-auto overflow-x-auto">
                      <table className="w-full text-left text-sm border-separate border-spacing-0">
                        <thead>
                          <tr>
                            {columns.map((c) => (
                              <th
                                key={c}
                                className="sticky top-0 z-10 whitespace-nowrap border-b border-slate-200 dark:border-slate-800 bg-white dark:bg-slate-900 py-2 pr-4 text-xs font-normal uppercase tracking-wide text-slate-500"
                              >
                                {c}
                              </th>
                            ))}
                          </tr>
                        </thead>
                        <tbody className="divide-y divide-slate-100 dark:divide-slate-800">
                          {previewLines.map((line) =>
                            line.kind === 'row' ? (
                              <tr key={`row-${line.excelRow}`}>
                                {columns.map((c) => (
                                  <td key={c} className="max-w-[100px] truncate py-1.5 pr-2 sm:max-w-[200px] sm:pr-4">
                                    {String((line.row as Record<string, unknown>)[c] ?? '')}
                                  </td>
                                ))}
                              </tr>
                            ) : (
                              <tr key={`err-${line.excelRow}`} className="text-rose-600 dark:text-rose-400">
                                <td colSpan={Math.max(columns.length, 1)} className="py-1.5 pr-4">
                                  <span className="inline-flex items-center gap-1.5">
                                    <XCircle className="h-3.5 w-3.5 shrink-0" /> Dòng {line.error.row}: {line.error.error}
                                  </span>
                                </td>
                              </tr>
                            ),
                          )}
                        </tbody>
                      </table>
                    </div>
                  )}
                </div>
              </>
            )}
          </Card>

          {lastBatchId && (
            <Card className="shrink-0">
              <div className="flex items-center justify-between">
                <span className="inline-flex items-center gap-1.5 text-sm text-slate-600 dark:text-slate-300">
                  <FileText className="h-3.5 w-3.5" /> Batch: {lastBatchId}
                </span>
                <Button onClick={() => exportResults(lastBatchId)}>
                  <Download className="h-3.5 w-3.5" /> Tải Excel kết quả (điền video_done)
                </Button>
              </div>
            </Card>
          )}

          {configOpen && preview && (
            <ExcelRenderConfigModal
              rowCount={preview.rows.length}
              submitting={submitting}
              onClose={() => setConfigOpen(false)}
              onConfirm={handleSubmit}
            />
          )}
        </>
      )}
      </div>
    </div>
  )
}
