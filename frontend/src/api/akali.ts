import { api } from './client'
import type { AkaliStatus } from '../types/api'

export function akaliStatus(): Promise<AkaliStatus> {
  return api.get('/api/akali/status')
}

export function saveAkaliConfig(body: { akali_url?: string; worker_key?: string }): Promise<AkaliStatus> {
  return api.post('/api/akali/config', body)
}
