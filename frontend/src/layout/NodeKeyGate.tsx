import { useEffect, useState } from 'react'
import type { ReactNode } from 'react'
import { KeyRound, Save } from 'lucide-react'
import { Card } from '../components/Card'
import { CardHeader } from '../components/CardHeader'
import { Button } from '../components/Button'
import { TextField } from '../components/form/TextField'
import { Spinner } from '../components/Spinner'
import { useNotify } from '../components/notifications/NotificationProvider'
import { dashboardStatus, saveDashboardConfig } from '../api/dashboard'
import { ApiError } from '../api/client'
import type { DashboardStatus } from '../types/api'

/**
 * Máy chưa nhập Ingest Token (Node key) thì không vào được phần nào của app.
 * Backend cũng chặn song song (middleware require_node_key trong backend/app.py) —
 * cửa này chỉ để người dùng biết phải làm gì, không phải lớp bảo vệ duy nhất.
 */
export function NodeKeyGate({ children }: { children: ReactNode }) {
  const notify = useNotify()
  const [status, setStatus] = useState<DashboardStatus | null>(null)
  const [loading, setLoading] = useState(true)
  const [token, setToken] = useState('')
  const [nodeName, setNodeName] = useState('')
  const [saving, setSaving] = useState(false)

  useEffect(() => {
    dashboardStatus()
      .then((s) => {
        setStatus(s)
        setNodeName(s.node_name ?? '')
      })
      .catch(() => setStatus(null))
      .finally(() => setLoading(false))
  }, [])

  const activate = async () => {
    if (!token.trim()) {
      notify.error('Nhập Ingest Token trước đã.')
      return
    }
    setSaving(true)
    try {
      const res = await saveDashboardConfig({ ingest_token: token, node_name: nodeName })
      setStatus(res)
      if (res.error) notify.error(res.error)
      else notify.success('Đã kích hoạt máy.')
    } catch (err) {
      notify.error(err instanceof ApiError ? err.message : 'Không lưu được token.')
    } finally {
      setSaving(false)
    }
  }

  if (loading) {
    return (
      <div className="flex min-h-screen items-center justify-center text-slate-400">
        <Spinner className="h-6 w-6" />
      </div>
    )
  }

  if (status?.configured) return <>{children}</>

  return (
    <div className="flex min-h-screen items-center justify-center bg-slate-50 p-4 dark:bg-slate-950">
      <Card className="!p-0 w-full max-w-lg overflow-hidden">
        <CardHeader icon={KeyRound} title="Kích hoạt máy render" />
        <div className="space-y-4 p-5">
          <p className="text-sm text-slate-500">
            Máy này chưa đăng ký với dashboard giám sát nên chưa dùng được. Lấy token ở dashboard{' '}
            <code>{status?.dashboard_url ?? 'render.relive.vn'}</code>, mục <strong>Node keys</strong> →{' '}
            <strong>Tạo node mới</strong> — mỗi máy một token riêng, chỉ hiện đúng một lần lúc tạo.
          </p>
          <TextField
            label="Ingest Token (Node key)"
            value={token}
            onChange={(e) => setToken(e.target.value)}
            placeholder="dán token vào đây"
          />
          <TextField
            label="Tên máy (tuỳ chọn — trống thì lấy hostname)"
            value={nodeName}
            onChange={(e) => setNodeName(e.target.value)}
          />
          <Button variant="primary" loading={saving} onClick={activate}>
            <Save className="h-3.5 w-3.5" /> Kích hoạt
          </Button>
        </div>
      </Card>
    </div>
  )
}
