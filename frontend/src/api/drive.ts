import { api } from './client'
import type { DriveFilesResponse, DriveReauthStatus, DriveStatus } from '../types/api'

export function driveStatus(): Promise<DriveStatus> {
  return api.get('/api/drive/status')
}

export function saveDriveConfig(body: { folder_id: string; enabled?: boolean }): Promise<DriveStatus> {
  return api.post('/api/drive/config', body)
}

export function driveFiles(folderId?: string): Promise<DriveFilesResponse> {
  const qs = folderId ? `?folder_id=${encodeURIComponent(folderId)}` : ''
  return api.get(`/api/drive/files${qs}`)
}

export function driveReauth(): Promise<DriveReauthStatus> {
  return api.post('/api/drive/reauth')
}

export function driveReauthStatus(): Promise<DriveReauthStatus> {
  return api.get('/api/drive/reauth/status')
}
