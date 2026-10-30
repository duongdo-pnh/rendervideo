import { useEffect, useState } from 'react'
import { AlertTriangle, CheckCircle2, Clock, FileText, Pencil, Ruler, Sparkles, Wand2, Zap } from 'lucide-react'
import { Card } from '../../components/Card'
import { CardHeader } from '../../components/CardHeader'
import { Badge } from '../../components/Badge'
import { Button } from '../../components/Button'
import { TextField } from '../../components/form/TextField'
import { SelectField } from '../../components/form/SelectField'
import { Checkbox } from '../../components/form/Checkbox'
import { FileDropzone } from '../../components/form/FileDropzone'
import { useNotify } from '../../components/notifications/NotificationProvider'
import { createAnimateJob } from '../../api/jobs'
import { lookupPlate } from '../../api/engines'
import { ApiError } from '../../api/client'
import type { Fun5bInfo, Fun5bPresetKey } from '../../types/api'
import { TableLineModal } from './TableLineModal'

/** The driving video's duration, read from the browser's own decoder. */
function useVideoDuration(file: File | null): number | null {
  // Kept with the file it was read from, so a new file shows nothing until its own value arrives.
  const [read, setRead] = useState<{ file: File; duration: number | null } | null>(null)
  useEffect(() => {
    if (!file) return
    const url = URL.createObjectURL(file)
    const el = document.createElement('video')
    el.preload = 'metadata'
    el.onloadedmetadata = () => setRead({ file, duration: Number.isFinite(el.duration) ? el.duration : null })
    el.src = url
    return () => URL.revokeObjectURL(url)
  }, [file])
  return read && read.file === file ? read.duration : null
}

export function CreateAnimateTab({ info }: { info: Fun5bInfo }) {
  const notify = useNotify()
  const [image, setImage] = useState<File | null>(null)
  const [video, setVideo] = useState<File | null>(null)
  const [preset, setPreset] = useState<Fun5bPresetKey>(info.defaults.preset)
  const [seed, setSeed] = useState(info.defaults.seed)
  const [keepBackground, setKeepBackground] = useState(info.defaults.keep_background)
  const [autoBackground, setAutoBackground] = useState(info.defaults.auto_background && info.plate_available)
  const [tablePercent, setTablePercent] = useState(0)
  // The table line is required for every image: the modal opens on choosing one, and the job can
  // only be queued once it was confirmed for that exact file.
  const [tableConfirmedFor, setTableConfirmedFor] = useState<File | null>(null)
  const [tableModalOpen, setTableModalOpen] = useState(false)
  const [sharpHands, setSharpHands] = useState(info.defaults.sharp_hands)
  const [plateLookup, setPlateLookup] = useState<{ image: File; cached: boolean } | null>(null)
  const [submitting, setSubmitting] = useState(false)
  const duration = useVideoDuration(video)

  useEffect(() => {
    if (!image) return
    let alive = true
    lookupPlate(image)
      .then((r) => alive && setPlateLookup({ image, cached: r.cached }))
      .catch(() => undefined) // unknown: no badge, the render still reuses a saved plate
    return () => {
      alive = false
    }
  }, [image])
  const plateCached = plateLookup && plateLookup.image === image ? plateLookup.cached : null

  const stem = (f: File) => f.name.replace(/\.[^.]+$/, '')
  const namePreview = image && video ? `${stem(image)}_${stem(video)}` : null // backend fun5b_job_name
  const presetInfo = info.presets.find((p) => p.key === preset)
  const tableConfirmed = !!image && tableConfirmedFor === image

  const chooseImage = (file: File | null) => {
    setImage(file)
    setTablePercent(0)
    setTableConfirmedFor(null)
    setTableModalOpen(!!file)
  }
  const confirmTable = (percent: number) => {
    setTablePercent(percent)
    setTableConfirmedFor(image)
    setTableModalOpen(false)
  }
  const cancelTable = () => {
    setTableModalOpen(false)
    if (!tableConfirmed) chooseImage(null) // required step: no line, no image
  }
  const submit = async () => {
    if (!image || !video) {
      notify.error('Cần chọn cả ảnh nhân vật và video mẫu.')
      return
    }
    if (!tableConfirmed) {
      setTableModalOpen(true)
      return
    }
    setSubmitting(true)
    try {
      const form = new FormData()
      form.append('image', image)
      form.append('video', video)
      form.append('preset', preset)
      form.append('seed', String(seed))
      form.append('keep_background', String(keepBackground))
      form.append('auto_background', String(autoBackground))
      if (tablePercent > 0) form.append('table_line', String(tablePercent))
      form.append('sharp_hands', String(sharpHands))
      const res = await createAnimateJob(form)
      notify.success(`Đã thêm vào hàng đợi: ${res.name}`)
      chooseImage(null)
      setVideo(null)
    } catch (err) {
      notify.error(err instanceof ApiError ? err.message : 'Không thêm được job vào hàng đợi.')
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <Card className="!p-0">
      <CardHeader icon={Wand2} title="Tạo video chuyển động nhân vật" />
      <div className="space-y-4 p-5">
        <p className="text-xs text-slate-500">
          Nhân vật trong ảnh làm theo động tác của video mẫu, giữ nền và ánh sáng của ảnh. Video ra luôn 720×1280 (9:16),
          30fps, <strong>không có tiếng</strong>.
        </p>
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
          <div>
            <FileDropzone label="1. Ảnh nhân vật" accept="image/png,image/jpeg,image/webp" kind="image" file={image} onChange={chooseImage} preview={false} />
            {tableConfirmed && (
              <button
                type="button"
                onClick={() => setTableModalOpen(true)}
                className="mt-1.5 inline-flex items-center gap-1.5 text-xs text-slate-500 hover:text-brand-600 dark:hover:text-brand-400"
              >
                <Ruler className="h-3.5 w-3.5" /> Mép bàn: {tablePercent > 0 ? `${tablePercent}%` : 'tự động'}
                <Pencil className="h-3 w-3" />
              </button>
            )}
            {image && plateCached === true && keepBackground && (
              <p className="mt-1.5"><Badge tone="success" icon={CheckCircle2}>Đã có nền sạch đã lưu — không phải vẽ lại</Badge></p>
            )}
            {image && plateCached === false && keepBackground && autoBackground && (
              <p className="mt-1.5"><Badge tone="warning" icon={Clock}>Ảnh mới: vẽ nền sạch lần đầu ~7 phút (sau đó dùng lại)</Badge></p>
            )}
          </div>
          <FileDropzone label="2. Video chuyển động (mẫu)" accept="video/*" kind="video" file={video} onChange={setVideo} preview={false} />
        </div>

        {namePreview && (
          <p className="flex items-center gap-1.5 text-xs text-slate-400">
            <FileText className="h-3.5 w-3.5" /> Tên file xuất: {namePreview}.mp4
          </p>
        )}

        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
          <div>
            <SelectField
              label="Chế độ"
              value={preset}
              onChange={(v) => setPreset(v as Fun5bPresetKey)}
              options={info.presets.map((p) => ({ label: `${p.label} (${p.steps} bước)`, value: p.key }))}
            />
            {presetInfo && <p className="mt-1 text-xs text-slate-400">{presetInfo.note}</p>}
          </div>
          <TextField label="Seed" type="number" value={seed} onChange={(e) => setSeed(Number(e.target.value))} />
        </div>

        <div className="space-y-2.5 rounded-xl border border-slate-200 p-4 dark:border-slate-800">
          <Checkbox label="Giữ nguyên nền & ánh sáng của ảnh gốc" checked={keepBackground} onChange={setKeepBackground} />
          {keepBackground && (
            <div className="space-y-3 pl-6">
              <Checkbox
                label={
                  info.plate_available
                    ? 'Tự vẽ lại nền sạch bằng AI (FireRed) — nền đã vẽ được lưu và dùng lại'
                    : 'Tự vẽ lại nền sạch (chưa cài FireRed — dùng LaMa)'
                }
                checked={autoBackground}
                onChange={setAutoBackground}
              />
            </div>
          )}
          <Checkbox label="Tay nét khi cử động nhanh (chậm hơn ~10–25%)" checked={sharpHands} onChange={setSharpHands} />
        </div>

        {duration !== null && duration > 90 && (
          <p className="flex items-center gap-1.5 text-xs text-amber-600 dark:text-amber-400">
            <AlertTriangle className="h-3.5 w-3.5" /> Video dài: nền có thể trôi dần sau vài chục giây.
          </p>
        )}

        {image && tableModalOpen && (
          <TableLineModal
            image={image}
            initialPercent={tablePercent}
            onConfirm={confirmTable}
            onCancel={cancelTable}
            cancelLabel={tableConfirmed ? 'Huỷ' : 'Bỏ ảnh'}
          />
        )}

        <Button variant="primary" loading={submitting} onClick={submit}>
          <Zap className="h-3.5 w-3.5" /> Thêm vào hàng đợi
        </Button>
        {!info.plate_available && (
          <p className="flex items-center gap-1.5 text-xs text-slate-400">
            <Sparkles className="h-3.5 w-3.5" /> FireRed chưa được cài: nền sẽ được xoá người bằng LaMa.
          </p>
        )}
      </div>
    </Card>
  )
}
