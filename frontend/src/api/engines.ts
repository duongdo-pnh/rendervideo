import { api } from './client'
import type {
  DeletedJobsResponse, Fun5bInfo, Fun5bInstallStatus, Fun5bPlate, Fun5bPlateLookup, Fun5bServerStatus, Fun5bStopResponse,
} from '../types/api'

const BASE = '/api/engines/fun5b'

export function getFun5bInfo(): Promise<Fun5bInfo> {
  return api.get(BASE)
}

export function getFun5bServer(): Promise<Fun5bServerStatus> {
  return api.get(`${BASE}/server`)
}

export function stopFun5bServer(): Promise<Fun5bStopResponse> {
  return api.post(`${BASE}/server/stop`)
}

export function listPlates(): Promise<Fun5bPlate[]> {
  return api.get(`${BASE}/plates`)
}

export function deletePlate(id: string): Promise<DeletedJobsResponse> {
  return api.del(`${BASE}/plates/${id}`)
}

export function plateImageUrl(id: string, kind: 'plate' | 'source' = 'plate'): string {
  return `${BASE}/plates/${id}/image?kind=${kind}`
}

/** Whether this character image already has a saved clean background (no ~7 min FireRed wait). */
export function lookupPlate(image: File): Promise<Fun5bPlateLookup> {
  const form = new FormData()
  form.append('image', image)
  return api.postForm(`${BASE}/plates/lookup`, form)
}

/** Start (or retry) the background install of the engine; it also starts by itself at app start. */
export function startFun5bInstall(): Promise<Fun5bInstallStatus> {
  return api.post(`${BASE}/install`)
}
