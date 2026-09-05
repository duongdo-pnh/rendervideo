import { api } from './client'
import type { AvatarCacheEntry, ClearStaleResponse, DeletedJobsResponse } from '../types/api'

export function listAvatarCache(): Promise<AvatarCacheEntry[]> {
  return api.get('/api/avatar-cache')
}

export function clearStaleAvatarCache(): Promise<ClearStaleResponse> {
  return api.post('/api/avatar-cache/clear-stale')
}

export function deleteAvatarCache(avatarId: string): Promise<DeletedJobsResponse> {
  return api.del(`/api/avatar-cache/${encodeURIComponent(avatarId)}`)
}
