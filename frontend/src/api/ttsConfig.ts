import { api } from './client'
import type { TtsConfigOut, TtsConfigSaveRequest, TtsConfigSchema, VoiceConnectResponse } from '../types/api'

export function ttsConfigSchema(): Promise<TtsConfigSchema> {
  return api.get('/api/tts-config/schema')
}

export function getTtsConfig(): Promise<TtsConfigOut> {
  return api.get('/api/tts-config')
}

export function saveTtsConfig(body: TtsConfigSaveRequest): Promise<TtsConfigOut> {
  return api.post('/api/tts-config', body)
}

export function vbeeConnect(body: { app_id: string; token: string }): Promise<VoiceConnectResponse> {
  return api.post('/api/tts-config/vbee/connect', body)
}

export function vbeeTest(body: { app_id: string; token: string; voice?: string; text: string }): Promise<Blob> {
  return api.postBlob('/api/tts-config/vbee/test', body)
}

export function ausynclabConnect(body: { api_key: string }): Promise<VoiceConnectResponse> {
  return api.post('/api/tts-config/ausynclab/connect', body)
}

export function ausynclabTest(body: { api_key: string; voice?: string; text: string }): Promise<Blob> {
  return api.postBlob('/api/tts-config/ausynclab/test', body)
}

export function autovoiceConnect(body: { api_key: string; url?: string; voices_url?: string }): Promise<VoiceConnectResponse> {
  return api.post('/api/tts-config/autovoice/connect', body)
}

export function autovoiceTest(body: { api_key: string; voice?: string; url?: string; speed?: string; text: string }): Promise<Blob> {
  return api.postBlob('/api/tts-config/autovoice/test', body)
}
