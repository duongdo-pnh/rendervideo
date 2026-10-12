import { useEffect, useState } from 'react'
import { CheckCircle2, PlugZap, Save } from 'lucide-react'
import { Card } from '../../components/Card'
import { CardHeader } from '../../components/CardHeader'
import { Button } from '../../components/Button'
import { TextField } from '../../components/form/TextField'
import { useNotify } from '../../components/notifications/NotificationProvider'
import { akaliStatus, saveAkaliConfig } from '../../api/akali'
import { ApiError } from '../../api/client'
import type { AkaliStatus } from '../../types/api'

export function AkaliTab() {
  const notify = useNotify()
  const [url, setUrl] = useState('')
  const [workerKey, setWorkerKey] = useState('')
  const [status, setStatus] = useState<AkaliStatus | null>(null)
  const [saving, setSaving] = useState(false)

  useEffect(() => {
    akaliStatus()
      .then((s) => {
        setStatus(s)
        setUrl(s.akali_url ?? '')
        setWorkerKey(s.worker_key ?? '')
      })
      .catch(() => {})
  }, [])

  const save = async () => {
    setSaving(true)
    try {
      const res = await saveAkaliConfig({ akali_url: url, worker_key: workerKey })
      setStatus(res)
      notify.success('Đã lưu cấu hình kết nối akali.')
    } catch (err) {
      notify.error(err instanceof ApiError ? err.message : 'Không lưu được cấu hình.')
    } finally {
      setSaving(false)
    }
  }

  return (
    <Card className="!p-0 overflow-hidden">
      <CardHeader icon={PlugZap} title="Kết nối akali" />
      <div className="space-y-4 p-5">
        <p className="text-sm text-slate-500">
          Máy này chạy <code>akali_bridge.py</code> (script nền riêng, không nằm trong backend web) để tự lấy job từ
          hàng đợi akali, render hết một batch Excel rồi trả link thư mục Drive về. Lưu cấu hình ở đây chỉ ghi vào{' '}
          <code>.env</code> — không tự khởi động lại <code>akali_bridge.py</code>, và không có bước "kiểm tra kết
          nối" thật vì gọi thử API akali có thể vô tình chiếm mất 1 job thật khỏi hàng đợi.
        </p>
        <TextField label="Akali URL" value={url} onChange={(e) => setUrl(e.target.value)} placeholder="http://10.10.11.103:8000" />
        <TextField label="Worker API Key" value={workerKey} onChange={(e) => setWorkerKey(e.target.value)} />
        <p className="text-xs text-slate-500">
          Worker ID gửi cho akali: <code>{status?.worker_id || '(đang tải…)'}</code> — lấy theo tên máy ở tab{' '}
          <strong>Dashboard Fleet</strong> (trống thì dùng hostname). Đổi tên máy thì sang tab đó.
        </p>
        <div className="flex items-center gap-3">
          <Button variant="primary" loading={saving} onClick={save}>
            <Save className="h-3.5 w-3.5" /> Lưu cấu hình
          </Button>
          {status?.configured && (
            <span className="inline-flex items-center gap-1.5 text-sm text-emerald-600 dark:text-emerald-400">
              <CheckCircle2 className="h-4 w-4" /> Đã điền đủ URL + Worker Key
            </span>
          )}
        </div>
      </div>
    </Card>
  )
}
