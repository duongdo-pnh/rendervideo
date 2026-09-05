import { useEffect, useState } from 'react'
import { CheckCircle2, LayoutDashboard, Save } from 'lucide-react'
import { Card } from '../../components/Card'
import { CardHeader } from '../../components/CardHeader'
import { Button } from '../../components/Button'
import { TextField } from '../../components/form/TextField'
import { useNotify } from '../../components/notifications/NotificationProvider'
import { dashboardStatus, saveDashboardConfig } from '../../api/dashboard'
import { ApiError } from '../../api/client'

export default function DashboardFleetPage() {
  const notify = useNotify()
  const [url, setUrl] = useState('')
  const [token, setToken] = useState('')
  const [nodeName, setNodeName] = useState('')
  const [status, setStatus] = useState<{ configured: boolean; error?: string | null } | null>(null)
  const [saving, setSaving] = useState(false)

  useEffect(() => {
    dashboardStatus()
      .then((s) => {
        setStatus(s)
        setUrl(s.dashboard_url ?? '')
        setToken(s.ingest_token ?? '')
        setNodeName(s.node_name ?? '')
      })
      .catch(() => {})
  }, [])

  const save = async () => {
    setSaving(true)
    try {
      const res = await saveDashboardConfig({ dashboard_url: url, ingest_token: token, node_name: nodeName })
      setStatus(res)
      if (res.error) notify.error(res.error)
      else notify.success('Đã lưu cấu hình Dashboard Fleet.')
    } catch (err) {
      notify.error(err instanceof ApiError ? err.message : 'Không lưu được cấu hình.')
    } finally {
      setSaving(false)
    }
  }

  return (
    <Card className="!p-0 overflow-hidden">
      <CardHeader icon={LayoutDashboard} title="Dashboard Fleet" />
      <div className="space-y-4 p-5">
        <p className="text-sm text-slate-500">
          Máy này tự đẩy CPU/GPU/hàng đợi render/TTS lên dashboard giám sát mỗi 10s (và thống kê mỗi 60s). Token lấy
          từ dashboard, mục "Node keys" → "Tạo node mới" — mỗi máy 1 token riêng, hiện đúng 1 lần lúc tạo.
        </p>
        <TextField label="Dashboard URL" value={url} onChange={(e) => setUrl(e.target.value)} placeholder="https://..." />
        <TextField label="Ingest Token" value={token} onChange={(e) => setToken(e.target.value)} />
        <TextField
          label="Tên hiển thị (tuỳ chọn)"
          value={nodeName}
          onChange={(e) => setNodeName(e.target.value)}
        />
        <div className="flex items-center gap-3">
          <Button variant="primary" loading={saving} onClick={save}>
            <Save className="h-3.5 w-3.5" /> Lưu &amp; kiểm tra kết nối
          </Button>
          {status?.configured && !status.error && (
            <span className="inline-flex items-center gap-1.5 text-sm text-emerald-600 dark:text-emerald-400">
              <CheckCircle2 className="h-4 w-4" /> Đã kết nối được dashboard
            </span>
          )}
          {status?.error && <span className="text-sm text-rose-600 dark:text-rose-400">{status.error}</span>}
        </div>
      </div>
    </Card>
  )
}
