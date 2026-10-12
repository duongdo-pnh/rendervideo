import { useState } from 'react'
import { Film, FileText, Zap } from 'lucide-react'
import { Card } from '../../components/Card'
import { CardHeader } from '../../components/CardHeader'
import { Button } from '../../components/Button'
import { TextField } from '../../components/form/TextField'
import { RadioGroup } from '../../components/form/RadioGroup'
import { SelectField } from '../../components/form/SelectField'
import { SliderField } from '../../components/form/SliderField'
import { Checkbox } from '../../components/form/Checkbox'
import { NativeFileField } from '../../components/form/NativeFileField'
import { useNotify } from '../../components/notifications/NotificationProvider'
import { createJob } from '../../api/jobs'
import { ApiError } from '../../api/client'
import { useEngineChoices } from '../../hooks/useEngineChoices'
import { buildName, INTENT_CHOICES, KIND_ANSWER, KIND_INTRO, slugKey } from '../../utils/jobNaming'
import type { Engine, InputType, ModelRes, OutRes } from '../../types/api'

const OUT_RES_CHOICES: OutRes[] = ['Gốc', '1080', '720', '480 Nhanh']

export function CreateJobTab() {
  const notify = useNotify()
  const engineChoices = useEngineChoices()

  const [video, setVideo] = useState<File | null>(null)
  const [audio, setAudio] = useState<File | null>(null)
  const [product, setProduct] = useState('')
  const [kind, setKind] = useState<string>(KIND_INTRO)
  const [intent, setIntent] = useState<string>('ASK_PRICE')
  const [otherKey, setOtherKey] = useState('')
  const [inputType, setInputType] = useState<InputType>('real')
  const [engine, setEngine] = useState<Engine>('musetalk')
  const [modelRes, setModelRes] = useState<ModelRes>('512')
  const [outRes, setOutRes] = useState<OutRes>('720')
  const [guidance, setGuidance] = useState(1.5)
  const [steps, setSteps] = useState(24)
  const [seed, setSeed] = useState(1247)
  const [enhanceMouth, setEnhanceMouth] = useState(true)
  const [region, setRegion] = useState<'mouth' | 'face'>('mouth')
  const [submitting, setSubmitting] = useState(false)

  const showIntent = kind === KIND_ANSWER
  const showOtherKey = kind === KIND_ANSWER && intent === 'ASK_OTHER'
  const namePreview = buildName(product, kind, showIntent ? intent : null, otherKey)

  const handleInputTypeChange = (value: InputType) => {
    setInputType(value)
    if (value === 'ai') {
      setGuidance(1.3)
      setSteps(28)
      setEnhanceMouth(false)
    } else {
      setGuidance(1.5)
      setSteps(24)
      setEnhanceMouth(true)
    }
  }

  const submit = async () => {
    if (!video || !audio) {
      notify.error('Cần chọn cả video và audio.')
      return
    }
    if (showOtherKey && !slugKey(otherKey)) {
      notify.error('Nhập "Other key" khi chọn Khác (ASK_OTHER).')
      return
    }
    setSubmitting(true)
    try {
      const form = new FormData()
      form.append('video', video)
      form.append('audio', audio)
      form.append('product', product)
      form.append('kind', kind)
      if (showIntent && intent) form.append('intent', intent)
      if (showOtherKey) form.append('other_key', otherKey)
      form.append('model_res', modelRes)
      form.append('guidance', String(guidance))
      form.append('steps', String(steps))
      form.append('seed', String(seed))
      form.append('enhance_mouth', String(enhanceMouth))
      form.append('enhance_region', region)
      form.append('out_res', outRes)
      form.append('input_type', inputType)
      form.append('engine', engine)
      const res = await createJob(form)
      notify.success(`Đã thêm vào hàng đợi: ${res.name}`)
      setVideo(null)
      setAudio(null)
      setProduct('')
      setKind(KIND_INTRO)
      setOtherKey('')
    } catch (err) {
      notify.error(err instanceof ApiError ? err.message : 'Không thêm được job vào hàng đợi.')
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <Card className="!p-0">
      <CardHeader icon={Film} title="Tạo job render" />
      <div className="space-y-4 p-5">
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
          <NativeFileField
            label="Video"
            accept="video/*"
            files={video ? [video] : []}
            onChange={(files) => setVideo(files[0] ?? null)}
          />
          <NativeFileField
            label="Audio"
            accept="audio/*"
            files={audio ? [audio] : []}
            onChange={(files) => setAudio(files[0] ?? null)}
          />
        </div>

        <TextField
          label="Sản phẩm (tên hoặc Shopee item_id)"
          placeholder="vd: set kep toc  hoặc  23525384022"
          value={product}
          onChange={(e) => setProduct(e.target.value)}
        />

        <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
          <SelectField
            label="Loại video"
            value={kind}
            onChange={setKind}
            options={[
              { label: KIND_INTRO, value: KIND_INTRO },
              { label: KIND_ANSWER, value: KIND_ANSWER },
            ]}
          />
          {showIntent && (
            <SelectField
              label="Câu hỏi"
              value={intent}
              onChange={setIntent}
              options={INTENT_CHOICES.map(([label, value]) => ({ label, value }))}
            />
          )}
        </div>
        {showOtherKey && (
          <TextField
            label="Other key (khi chọn ASK_OTHER)"
            placeholder="vd: khautrang, combo_quatang"
            value={otherKey}
            onChange={(e) => setOtherKey(e.target.value)}
          />
        )}
        <p className="flex items-center gap-1.5 text-xs text-slate-400">
          <FileText className="h-3.5 w-3.5" /> Tên file xuất: {namePreview}.mp4
        </p>

        <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
          <SelectField
            label="Engine"
            value={engine}
            onChange={(v) => setEngine(v as Engine)}
            options={engineChoices}
          />
          <SelectField
            label="Độ phân giải"
            value={outRes}
            onChange={(v) => setOutRes(v as OutRes)}
            options={OUT_RES_CHOICES.map((v) => ({ label: v, value: v }))}
          />
          <SelectField
            label="Loại input"
            value={inputType}
            onChange={(v) => handleInputTypeChange(v as InputType)}
            options={[
              { label: 'Người thật', value: 'real' },
              { label: 'Video AI', value: 'ai' },
            ]}
          />
        </div>

        {engine === 'latentsync' && (
          <RadioGroup
            label="LatentSync checkpoint"
            value={modelRes}
            onChange={(v) => setModelRes(v as ModelRes)}
            options={[
              { label: '256 (nhanh ~2×)', value: '256' },
              { label: '512 (nét/tự nhiên, chậm)', value: '512' },
            ]}
          />
        )}

        <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
          <SliderField label="Guidance Scale" value={guidance} min={1} max={3} step={0.1} onChange={setGuidance} />
          <SliderField label="Inference Steps" value={steps} min={8} max={50} step={1} onChange={setSteps} />
        </div>

        <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
          <TextField label="Seed" type="number" value={seed} onChange={(e) => setSeed(Number(e.target.value))} />
          <RadioGroup
            label="Vùng làm nét"
            value={region}
            onChange={(v) => setRegion(v as 'mouth' | 'face')}
            options={[
              { label: 'mouth', value: 'mouth' },
              { label: 'face', value: 'face' },
            ]}
          />
        </div>

        <Checkbox label="Làm nét miệng (GFPGAN)" checked={enhanceMouth} onChange={setEnhanceMouth} />

        <Button variant="primary" loading={submitting} onClick={submit}>
          <Zap className="h-3.5 w-3.5" /> Thêm vào hàng đợi
        </Button>
      </div>
    </Card>
  )
}
