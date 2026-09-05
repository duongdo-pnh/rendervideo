import { api } from './client'
import type { LivestreamEnqueueResponse, SampleAudioChoice } from '../types/api'

export function sampleAudio(): Promise<SampleAudioChoice[]> {
  return api.get('/api/livestream/sample-audio')
}

export function serverStatus(): Promise<{ running: boolean }> {
  return api.get('/api/livestream/server/status')
}

export function serverStart(): Promise<{ running: boolean }> {
  return api.post('/api/livestream/server/start')
}

export function serverStop(): Promise<{ running: boolean }> {
  return api.post('/api/livestream/server/stop')
}

export function startSession(sessionId: string, form: FormData): Promise<Record<string, unknown>> {
  return api.postForm(`/api/livestream/${encodeURIComponent(sessionId)}/start`, form)
}

export function sessionStatus(sessionId: string): Promise<Record<string, unknown>> {
  return api.get(`/api/livestream/${encodeURIComponent(sessionId)}/status`)
}

export function previewUrl(sessionId: string): string {
  return `/api/livestream/${encodeURIComponent(sessionId)}/preview.jpg`
}

export function enqueueAudio(sessionId: string, form: FormData): Promise<LivestreamEnqueueResponse> {
  return api.postForm(`/api/livestream/${encodeURIComponent(sessionId)}/enqueue`, form)
}

export function enqueueSample(
  sessionId: string,
  body: { sample_path: string; request_id?: string; priority?: number; interrupt?: boolean },
): Promise<LivestreamEnqueueResponse> {
  return api.post(`/api/livestream/${encodeURIComponent(sessionId)}/enqueue-sample`, body)
}

export function interruptSession(sessionId: string): Promise<Record<string, unknown>> {
  return api.post(`/api/livestream/${encodeURIComponent(sessionId)}/interrupt`)
}

export function endSession(sessionId: string): Promise<Record<string, unknown>> {
  return api.del(`/api/livestream/${encodeURIComponent(sessionId)}`)
}
