import { api } from './client'
import type { DeletedJobsResponse, VideoOut } from '../types/api'

export function listVideos(): Promise<VideoOut[]> {
  return api.get('/api/videos')
}

export function streamUrl(name: string): string {
  return `/api/videos/${encodeURIComponent(name)}/stream`
}

export function downloadUrl(name: string): string {
  return `/api/videos/${encodeURIComponent(name)}/download`
}

export function deleteVideo(name: string): Promise<DeletedJobsResponse> {
  return api.del(`/api/videos/${encodeURIComponent(name)}`)
}
