import { api } from './client'
import type { RequeueResponse, TtsStatusResponse, VoiceChoice } from '../types/api'

export function ttsStatus(): Promise<TtsStatusResponse> {
  return api.get('/api/tts/status')
}

export function requeueDeadLetter(): Promise<RequeueResponse> {
  return api.post('/api/tts/requeue-dead-letter')
}

export function requeueFailedPermanent(ttsJobIds: number[]): Promise<RequeueResponse> {
  return api.post('/api/tts/requeue-failed-permanent', { tts_job_ids: ttsJobIds })
}

export function requeueByIds(ttsJobIds: number[]): Promise<RequeueResponse> {
  return api.post('/api/tts/requeue-by-ids', { tts_job_ids: ttsJobIds })
}

export function ttsProviders(): Promise<Record<string, unknown>[]> {
  return api.get('/api/tts/providers')
}

export function providerVoices(provider: string): Promise<VoiceChoice[]> {
  return api.get(`/api/tts/providers/${encodeURIComponent(provider)}/voices`)
}
