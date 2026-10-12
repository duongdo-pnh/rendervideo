import { api } from './client'
import type { DashboardStatus } from '../types/api'

export function dashboardStatus(): Promise<DashboardStatus> {
  return api.get('/api/dashboard/status')
}

// Không có dashboard_url: URL cố định trong code phía backend, chỉ đọc qua status().
export function saveDashboardConfig(body: {
  ingest_token?: string
  node_name?: string
}): Promise<DashboardStatus> {
  return api.post('/api/dashboard/config', body)
}
