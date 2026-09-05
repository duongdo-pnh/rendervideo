import { useEffect, useRef, useState } from 'react'
import { AudioLines, Play, Plug, Save } from 'lucide-react'
import { Card } from '../../components/Card'
import { CardHeader } from '../../components/CardHeader'
import { Button } from '../../components/Button'
import { TextField } from '../../components/form/TextField'
import { SelectField } from '../../components/form/SelectField'
import { Badge } from '../../components/Badge'
import { Spinner } from '../../components/Spinner'
import { useNotify } from '../../components/notifications/NotificationProvider'
import {
  ausynclabConnect,
  ausynclabTest,
  autovoiceConnect,
  autovoiceTest,
  getTtsConfig,
  saveTtsConfig,
  ttsConfigSchema,
  vbeeConnect,
  vbeeTest,
} from '../../api/ttsConfig'
import { ApiError } from '../../api/client'
import type { TtsConfigSchema, VoiceChoice } from '../../types/api'

const PROVIDER_ORDER = ['vbee', 'ausynclab', 'autovoice', 'api', 'local']

function findVoiceKey(fields: [string, string, boolean][]): string | undefined {
  return fields.find(([key]) => key.endsWith('_DEFAULT_VOICE'))?.[0]
}

export default function TtsConfigPage() {
  const notify = useNotify()
  const [schema, setSchema] = useState<TtsConfigSchema | null>(null)
  const [values, setValues] = useState<Record<string, string>>({})
  const [defaultProvider, setDefaultProvider] = useState('vbee')
  const [providerStatus, setProviderStatus] = useState<Record<string, unknown>[]>([])
  const [activeProvider, setActiveProvider] = useState('vbee')
  const [saving, setSaving] = useState(false)
  const [connecting, setConnecting] = useState(false)
  const [testing, setTesting] = useState(false)
  const [testText, setTestText] = useState('Xin chào, đây là giọng đọc thử nghiệm.')
  const [voiceChoices, setVoiceChoices] = useState<VoiceChoice[]>([])
  const audioRef = useRef<HTMLAudioElement | null>(null)
  if (audioRef.current === null && typeof Audio !== 'undefined') audioRef.current = new Audio()

  useEffect(() => {
    Promise.all([ttsConfigSchema(), getTtsConfig()])
      .then(([s, c]) => {
        setSchema(s)
        setValues(c.values)
        setDefaultProvider(c.default_provider)
        setProviderStatus(c.providers)
        setVoiceChoices(s.curated_voices[activeProvider] ?? [])
      })
      .catch((err) => notify.error(err instanceof ApiError ? err.message : 'Không tải được cấu hình TTS.'))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  useEffect(() => {
    setVoiceChoices(schema?.curated_voices[activeProvider] ?? [])
  }, [activeProvider, schema])

  const setField = (key: string, value: string) => setValues((v) => ({ ...v, [key]: value }))

  const save = async () => {
    setSaving(true)
    try {
      const res = await saveTtsConfig({ default_provider: defaultProvider, values })
      setValues(res.values)
      setDefaultProvider(res.default_provider)
      setProviderStatus(res.providers)
      notify.success('Đã lưu cấu hình TTS.')
    } catch (err) {
      notify.error(err instanceof ApiError ? err.message : 'Không lưu được cấu hình.')
    } finally {
      setSaving(false)
    }
  }

  const connect = async () => {
    setConnecting(true)
    try {
      let res
      if (activeProvider === 'vbee') {
        res = await vbeeConnect({ app_id: values.VBEE_APP_ID ?? '', token: values.VBEE_TOKEN ?? '' })
      } else if (activeProvider === 'ausynclab') {
        res = await ausynclabConnect({ api_key: values.AUSYNCLAB_API_KEY ?? '' })
      } else {
        res = await autovoiceConnect({
          api_key: values.AUTOVOICE_API_KEY ?? '',
          url: values.AUTOVOICE_URL ?? '',
          voices_url: values.AUTOVOICE_VOICES_URL ?? '',
        })
      }
      setVoiceChoices(res.voices)
      notify.success(res.message)
    } catch (err) {
      notify.error(err instanceof ApiError ? err.message : 'Kết nối thất bại.')
    } finally {
      setConnecting(false)
    }
  }

  const test = async () => {
    if (!testText.trim()) {
      notify.error('Nhập câu cần đọc thử.')
      return
    }
    setTesting(true)
    try {
      const fields = schema?.provider_fields[activeProvider] ?? []
      const voiceKey = findVoiceKey(fields)
      const voice = voiceKey ? values[voiceKey] : undefined
      let blob
      if (activeProvider === 'vbee') {
        blob = await vbeeTest({ app_id: values.VBEE_APP_ID ?? '', token: values.VBEE_TOKEN ?? '', voice, text: testText })
      } else if (activeProvider === 'ausynclab') {
        blob = await ausynclabTest({ api_key: values.AUSYNCLAB_API_KEY ?? '', voice, text: testText })
      } else {
        blob = await autovoiceTest({
          api_key: values.AUTOVOICE_API_KEY ?? '',
          voice,
          url: values.AUTOVOICE_URL ?? '',
          speed: values.AUTOVOICE_SPEED ?? '',
          text: testText,
        })
      }
      if (audioRef.current) {
        audioRef.current.src = URL.createObjectURL(blob)
        void audioRef.current.play()
      }
    } catch (err) {
      notify.error(err instanceof ApiError ? err.message : 'Nghe thử thất bại.')
    } finally {
      setTesting(false)
    }
  }

  if (!schema) {
    return (
      <Card className="flex justify-center py-16">
        <Spinner className="h-6 w-6" />
      </Card>
    )
  }

  const providers = PROVIDER_ORDER.filter((p) => schema.provider_fields[p])
  const fields = schema.provider_fields[activeProvider] ?? []
  const canConnect = ['vbee', 'ausynclab', 'autovoice'].includes(activeProvider)
  const voiceKey = findVoiceKey(fields)

  return (
    <div className="space-y-4">
      <Card className="!p-0 overflow-hidden">
        <CardHeader
          icon={AudioLines}
          title="Cấu hình TTS"
          actions={
            <>
              <SelectField
                value={defaultProvider}
                onChange={setDefaultProvider}
                options={providers.map((p) => ({ label: `${schema.provider_labels[p]}${p === defaultProvider ? ' (mặc định)' : ''}`, value: p }))}
              />
              <Button variant="primary" loading={saving} onClick={save}>
                <Save className="h-3.5 w-3.5" /> Lưu
              </Button>
            </>
          }
        />
        <div className="space-y-4 p-5">
          {schema.common_fields.map(([key, label]) => (
            <TextField key={key} label={label} value={values[key] ?? ''} onChange={(e) => setField(key, e.target.value)} />
          ))}

          <div className="flex gap-1 border-b border-slate-100 dark:border-slate-800">
            {providers.map((p) => (
              <button
                key={p}
                onClick={() => setActiveProvider(p)}
                className={`-mb-px border-b-2 px-3 py-2 text-sm font-medium transition-colors ${
                  activeProvider === p
                    ? 'border-brand-500 text-brand-600 dark:text-brand-400'
                    : 'border-transparent text-slate-500 hover:text-slate-700 dark:hover:text-slate-300'
                }`}
              >
                {schema.provider_labels[p]}
              </button>
            ))}
          </div>

          <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
            {fields.map(([key, label, isSecret]) => (
              <TextField
                key={key}
                label={label}
                type={isSecret ? 'password' : 'text'}
                value={values[key] ?? ''}
                onChange={(e) => setField(key, e.target.value)}
              />
            ))}
          </div>

          {voiceKey && voiceChoices.length > 0 && (
            <SelectField
              label="Giọng có sẵn — chọn để đặt làm mặc định"
              placeholder="— chọn giọng —"
              value=""
              onChange={(v) => setField(voiceKey, v)}
              options={voiceChoices.map((v) => ({ label: v.label, value: v.value }))}
            />
          )}

          {canConnect && (
            <button
              onClick={connect}
              disabled={connecting}
              className="inline-flex items-center gap-1.5 text-sm font-medium text-brand-600 hover:text-brand-700 dark:text-brand-400 disabled:opacity-50"
            >
              <Plug className="h-3.5 w-3.5" /> {connecting ? 'Đang kết nối…' : 'Kết nối & tải giọng'}
            </button>
          )}

          {canConnect && (
            <div className="flex items-center gap-2 rounded-xl bg-slate-50 dark:bg-slate-800/60 p-3">
              <input
                className="flex-1 rounded-lg border border-slate-300 dark:border-slate-700 bg-white dark:bg-slate-900 px-3 py-1.5 text-sm"
                value={testText}
                onChange={(e) => setTestText(e.target.value)}
              />
              <Button loading={testing} onClick={test}>
                <Play className="h-3.5 w-3.5" /> Nghe thử
              </Button>
            </div>
          )}
        </div>
      </Card>

      <Card className="!p-0 overflow-hidden">
        <CardHeader icon={Plug} title="Trạng thái provider" />
        <div className="flex flex-wrap gap-2 p-5">
          {providerStatus.map((p, i) => {
            const row = p as { label?: string; enabled?: boolean; is_default?: boolean }
            const suffix = row.is_default ? ' · mặc định' : row.enabled ? '' : ' · chưa cấu hình'
            return (
              <Badge key={i} tone={row.enabled ? 'success' : 'neutral'}>
                {row.label}
                {suffix}
              </Badge>
            )
          })}
        </div>
      </Card>
    </div>
  )
}
