import { useState } from 'react'
import { Rocket, Settings2, X } from 'lucide-react'
import { Button } from '../../components/Button'
import { SelectField } from '../../components/form/SelectField'
import { RadioGroup } from '../../components/form/RadioGroup'
import { SliderField } from '../../components/form/SliderField'
import { TextField } from '../../components/form/TextField'
import { Checkbox } from '../../components/form/Checkbox'
import { useEngineChoices } from '../../hooks/useEngineChoices'
import type { Engine, InputType, ModelRes, OutRes } from '../../types/api'

const OUT_RES_CHOICES: OutRes[] = ['Gốc', '1080', '720', '480 Nhanh']

export interface ExcelRenderConfig {
  engine: Engine
  model_res: ModelRes
  out_res: OutRes
  input_type: InputType
  guidance: number
  steps: number
  seed: number
  enhance_mouth: boolean
  enhance_region: 'mouth' | 'face'
  dedup: boolean
}

const DEFAULT_CONFIG: ExcelRenderConfig = {
  engine: 'musetalk',
  model_res: '512',
  out_res: '720',
  input_type: 'real',
  guidance: 1.5,
  steps: 24,
  seed: 1247,
  enhance_mouth: true,
  enhance_region: 'mouth',
  dedup: true,
}

export function ExcelRenderConfigModal({
  rowCount,
  submitting,
  onClose,
  onConfirm,
}: {
  rowCount: number
  submitting: boolean
  onClose: () => void
  onConfirm: (config: ExcelRenderConfig) => void
}) {
  const [config, setConfig] = useState<ExcelRenderConfig>(DEFAULT_CONFIG)
  const engineChoices = useEngineChoices()
  const set = <K extends keyof ExcelRenderConfig>(key: K, value: ExcelRenderConfig[K]) =>
    setConfig((prev) => ({ ...prev, [key]: value }))

  const handleInputTypeChange = (value: InputType) => {
    setConfig((prev) => ({
      ...prev,
      input_type: value,
      guidance: value === 'ai' ? 1.3 : 1.5,
      steps: value === 'ai' ? 28 : 24,
      enhance_mouth: value !== 'ai',
    }))
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4">
      <div className="max-h-[85vh] w-full max-w-2xl overflow-y-auto rounded-2xl bg-white dark:bg-slate-900 shadow-card border border-slate-200 dark:border-slate-800">
        <div className="flex items-start justify-between border-b border-slate-100 dark:border-slate-800 px-5 py-4">
          <div>
            <h2 className="flex items-center gap-1.5 text-sm font-semibold text-slate-800 dark:text-slate-100">
              <Settings2 className="h-4 w-4 text-brand-600 dark:text-brand-400" /> Cấu hình render (áp cho toàn bộ Excel)
            </h2>
            <p className="mt-0.5 text-xs text-slate-500">
              Áp dụng cho {rowCount} dòng trước khi đưa vào TTS queue.
            </p>
          </div>
          <button onClick={onClose} className="text-slate-400 hover:text-slate-600">
            <X className="h-4 w-4" />
          </button>
        </div>

        <div className="space-y-4 p-5">
          <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
            <SelectField
              label="Engine"
              value={config.engine}
              onChange={(v) => set('engine', v as Engine)}
              options={engineChoices}
            />
            <SelectField
              label="Độ phân giải"
              value={config.out_res}
              onChange={(v) => set('out_res', v as OutRes)}
              options={OUT_RES_CHOICES.map((v) => ({ label: v, value: v }))}
            />
            <SelectField
              label="Loại input"
              value={config.input_type}
              onChange={(v) => handleInputTypeChange(v as InputType)}
              options={[
                { label: 'Người thật', value: 'real' },
                { label: 'Video AI', value: 'ai' },
              ]}
            />
          </div>

          <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
            <SliderField
              label="Guidance Scale"
              value={config.guidance}
              min={1}
              max={3}
              step={0.1}
              onChange={(v) => set('guidance', v)}
            />
            <SliderField label="Inference Steps" value={config.steps} min={8} max={50} step={1} onChange={(v) => set('steps', v)} />
          </div>

          <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
            <TextField label="Seed" type="number" value={config.seed} onChange={(e) => set('seed', Number(e.target.value))} />
            <RadioGroup
              label="Vùng làm nét"
              value={config.enhance_region}
              onChange={(v) => set('enhance_region', v as 'mouth' | 'face')}
              options={[
                { label: 'mouth', value: 'mouth' },
                { label: 'face', value: 'face' },
              ]}
            />
          </div>

          <div className="flex flex-wrap items-center gap-4">
            <Checkbox label="Làm nét miệng (GFPGAN)" checked={config.enhance_mouth} onChange={(v) => set('enhance_mouth', v)} />
            <Checkbox label="Bỏ qua dòng đang chờ trùng (dedup)" checked={config.dedup} onChange={(v) => set('dedup', v)} />
          </div>
        </div>

        <div className="flex justify-end gap-2 border-t border-slate-100 dark:border-slate-800 px-5 py-3">
          <Button onClick={onClose}>Hủy</Button>
          <Button variant="primary" loading={submitting} onClick={() => onConfirm(config)}>
            <Rocket className="h-3.5 w-3.5" /> Đưa {rowCount} dòng vào TTS queue
          </Button>
        </div>
      </div>
    </div>
  )
}
