import { api } from './client'
import type { DashboardStatus } from '../types/api'

export function dashboardStatus(): Promise<DashboardStatus> {
  return api.get('/api/dashboard/status')
}

export function saveDashboardConfig(body: {
  dashboard_url?: string
  ingest_token?: string
  node_name?: string
}): Promise<DashboardStatus> {
  return api.post('/api/dashboard/config', body)
}
