import { useEffect, useState } from 'react'
import { Activity, Ban, Play, Power, Radio, RefreshCw, Send, Square } from 'lucide-react'
import { Card } from '../../components/Card'
import { CardHeader, LinkAction } from '../../components/CardHeader'
import { Button } from '../../components/Button'
import { TextField } from '../../components/form/TextField'
import { NativeFileField } from '../../components/form/NativeFileField'
import { useNotify } from '../../components/notifications/NotificationProvider'
import { usePolling } from '../../hooks/usePolling'
import {
  enqueueAudio,
  interruptSession,
  previewUrl,
  sampleAudio,
  sessionStatus,
  serverStart,
  serverStatus,
  serverStop,
  startSession,
} from '../../api/livestream'
import { ApiError } from '../../api/client'
import type { SampleAudioChoice } from '../../types/api'

export default function LivestreamPage() {
  const notify = useNotify()
  const [serverRunning, setServerRunning] = useState(false)
  const [sessionId, setSessionId] = useState('facebook-live')
  const [avatarVideo, setAvatarVideo] = useState<File | null>(null)
  const [avatarMaxSeconds, setAvatarMaxSeconds] = useState(0)
  const [renderAhead, setRenderAhead] = useState(10)
  const [batchSize, setBatchSize] = useState(32)
  const [obsFps] = useState(30)
  const [audioDelay, setAudioDelay] = useState(0)
  const [status, setStatus] = useState<Record<string, unknown> | null>(null)
  const [starting, setStarting] = useState(false)
  const [audio, setAudio] = useState<File | null>(null)
  const [samples, setSamples] = useState<SampleAudioChoice[]>([])
  const [previewTick, setPreviewTick] = useState(0)

  const refreshServer = () => serverStatus().then((s) => setServerRunning(s.running)).catch(() => {})

  useEffect(() => {
    void refreshServer()
    sampleAudio().then(setSamples).catch(() => {})
  }, [])

  const active = Boolean(status && status.running !== false)

  usePolling(
    async () => {
      try {
        setStatus(await sessionStatus(sessionId))
      } catch {
        // no active session yet
      }
    },
    2000,
    active,
  )
  usePolling(() => setPreviewTick((t) => t + 1), 1000, active)

  const toggleServer = async () => {
    try {
      const res = serverRunning ? await serverStop() : await serverStart()
      setServerRunning(res.running)
    } catch (err) {
      notify.error(err instanceof ApiError ? err.message : 'Không đổi được trạng thái server.')
    }
  }

  const checkStatus = async () => {
    try {
      setStatus(await sessionStatus(sessionId))
    } catch (err) {
      notify.error(err instanceof ApiError ? err.message : 'Không tìm thấy phiên nào.')
    }
  }

  const handleStart = async () => {
    setStarting(true)
    try {
      const form = new FormData()
      if (avatarVideo) form.append('avatar_video', avatarVideo)
      form.append('render_ahead_seconds', String(renderAhead))
      form.append('batch_size', String(batchSize))
      form.append('obs_output_fps', String(obsFps))
      form.append('audio_delay_ms', String(audioDelay))
      form.append('avatar_max_seconds', String(avatarMaxSeconds))
      await startSession(sessionId, form)
      notify.success('Đang chuẩn bị avatar — có thể mất vài phút đến 40 phút cho lần đầu.')
    } catch (err) {
      notify.error(err instanceof ApiError ? err.message : 'Không bắt đầu được stream.')
    } finally {
      setStarting(false)
    }
  }

  const handleEnqueue = async () => {
    if (!audio) {
      notify.error('Chọn file audio trước.')
      return
    }
    try {
      const form = new FormData()
      form.append('audio', audio)
      const res = await enqueueAudio(sessionId, form)
      notify.success(res.message)
      setAudio(null)
    } catch (err) {
      notify.error(err instanceof ApiError ? err.message : 'Không phát được audio.')
    }
  }

  const handleInterrupt = async () => {
    try {
      await interruptSession(sessionId)
      notify.success('Đã ngắt câu hiện tại.')
    } catch (err) {
      notify.error(err instanceof ApiError ? err.message : 'Không ngắt được.')
    }
  }

  return (
    <div className="space-y-4">
      <Card className="flex flex-wrap items-center justify-between gap-2">
        <span className="flex items-center gap-2 text-sm text-slate-600 dark:text-slate-300">
          <span className={`h-2 w-2 rounded-full ${serverRunning ? 'bg-emerald-500' : 'bg-slate-400'}`} />
          Server streaming (MuseTalk) {serverRunning ? 'đang chạy' : 'đã tắt'}
        </span>
        <LinkAction onClick={toggleServer}>
          <Power className="h-3.5 w-3.5" /> {serverRunning ? 'Tắt server' : 'Bật server'}
        </LinkAction>
      </Card>

      <Card className="!p-0 overflow-hidden">
        <CardHeader
          icon={Radio}
          title="Livestream"
          actions={
            <>
              <input
                className="w-full max-w-[220px] rounded-lg border border-slate-300 dark:border-slate-700 bg-white dark:bg-slate-900 px-3 py-1.5 text-sm"
                value={sessionId}
                onChange={(e) => setSessionId(e.target.value)}
              />
              <LinkAction onClick={checkStatus}>
                <RefreshCw className="h-3.5 w-3.5" /> Kiểm tra
              </LinkAction>
            </>
          }
        />
      </Card>

      <Card className="!p-0 overflow-hidden">
        <CardHeader icon={Activity} title="Cấu hình / bắt đầu phiên mới" />
        <div className="space-y-4 p-5">
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
            <NativeFileField
              label="Avatar video"
              accept="video/*"
              files={avatarVideo ? [avatarVideo] : []}
              onChange={(files) => setAvatarVideo(files[0] ?? null)}
            />
            <TextField
              label="Avatar tối đa (giây, 0 = cả video)"
              type="number"
              value={avatarMaxSeconds}
              onChange={(e) => setAvatarMaxSeconds(Number(e.target.value))}
            />
          </div>
          <p className="text-xs text-slate-400">
            Session {sessionId || 'facebook-live'} luôn phát qua UDP nội bộ tới OBS — không cần RTMP server/key. Đổi
            Session ID ở trên (khác "facebook-live") để nhập RTMP tuỳ chỉnh.
          </p>
          <div className="grid grid-cols-2 sm:grid-cols-4 gap-4">
            <TextField label="Render ahead (s)" type="number" value={renderAhead} onChange={(e) => setRenderAhead(Number(e.target.value))} />
            <TextField label="Batch size" type="number" value={batchSize} onChange={(e) => setBatchSize(Number(e.target.value))} />
            <TextField label="OBS output FPS" type="number" value={obsFps} disabled />
            <TextField label="Audio delay (ms)" type="number" value={audioDelay} onChange={(e) => setAudioDelay(Number(e.target.value))} />
          </div>
          <Button variant="primary" loading={starting} onClick={handleStart}>
            <Play className="h-3.5 w-3.5" /> Bắt đầu stream
          </Button>
        </div>
      </Card>

      {active && (
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
          <Card className="!p-0 overflow-hidden">
            <CardHeader icon={Radio} title="Xem trước" />
            <div className="p-3">
              <img
                key={previewTick}
                src={`${previewUrl(sessionId)}?t=${previewTick}`}
                alt="preview"
                className="w-full rounded-xl bg-black"
              />
            </div>
          </Card>
          <Card className="!p-0 overflow-hidden">
            <CardHeader
              icon={Send}
              title="Phát audio"
              actions={
                <LinkAction onClick={handleInterrupt}>
                  <Ban className="h-3.5 w-3.5" /> Ngắt câu
                </LinkAction>
              }
            />
            <div className="space-y-3 p-5">
              <NativeFileField label="Audio" accept="audio/*" files={audio ? [audio] : []} onChange={(files) => setAudio(files[0] ?? null)} />
              <Button variant="primary" onClick={handleEnqueue}>
                <Send className="h-3.5 w-3.5" /> Phát audio
              </Button>
              {samples.length > 0 && (
                <p className="text-xs text-slate-400">{samples.length} voice mẫu có sẵn.</p>
              )}
            </div>
          </Card>
        </div>
      )}

      {status && (
        <Card className="!p-0 overflow-hidden">
          <CardHeader icon={Square} title="Trạng thái phiên" />
          <pre className="max-h-64 overflow-auto p-5 text-xs text-slate-500">{JSON.stringify(status, null, 2)}</pre>
        </Card>
      )}
    </div>
  )
}
