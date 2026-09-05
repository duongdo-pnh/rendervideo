// Hand-transcribed from backend/models.py — keep in sync with that file.

export type JobStatus = 'queued' | 'rendering' | 'done' | 'failed' | 'cancelled'
export type Engine = 'musetalk' | 'latentsync'
export type ModelRes = '512' | '256'
export type OutRes = 'Gốc' | '1080' | '720' | '480 Nhanh'
export type InputType = 'real' | 'ai'

export interface JobOut {
  id: number
  name: string
  status: JobStatus
  engine: Engine
  model_res: ModelRes
  out_res: OutRes
  input_type: InputType
  guidance: number
  steps: number
  seed: number
  enhance_mouth: boolean
  enhance_region: string
  priority: number
  retries: number
  error?: string | null
  output_path?: string | null
  created_at?: string | null
  started_at?: string | null
  finished_at?: string | null
  drive_link?: string | null
  batch_id?: string | null
  excel_file?: string | null
  drive_error?: string | null
  drive_folder?: string | null
}

export interface JobSubmitResponse {
  job_id: number
  name: string
  engine: string
  message: string
}

export interface PriorityChoice {
  id: number
  label: string
}

export interface DeletedJobsResponse {
  deleted: number
  message: string
}

export interface CancelJobResponse {
  status_before: string
  message: string
}

export interface CancelAllResponse {
  cancelled_queued: number
  flagged_rendering: number
  message: string
}

export interface VideoOut {
  name: string
  rendered_at: string
  size_mb: number
}

export interface ExcelPreviewResponse {
  rows: Record<string, unknown>[]
  errors: { row: number; error: string }[]
  summary: string
  state: Record<string, unknown>
}

export interface ExcelSubmitRequest {
  state: Record<string, unknown>
  model_res?: ModelRes
  out_res?: OutRes
  enhance_mouth?: boolean
  guidance?: number
  steps?: number
  seed?: number
  enhance_region?: string
  input_type?: InputType
  engine?: Engine
  dedup?: boolean
}

export interface ExcelSubmitResponse {
  batch_id: string
  enqueued: number
  skipped: number
  warnings: number
  skipped_detail: Record<string, unknown>[]
  warnings_detail: Record<string, unknown>[]
  message: string
}

export interface TtsJobRow {
  id: number
  row: number
  product: string
  text: string
  status: string
  status_label: string
  error: string | null
  updated_at: string
  batch_id?: string | null
  excel_file?: string | null
}

export interface TtsStatusResponse {
  counts: Record<string, number>
  dead_letter: { row: number; error: string }[]
  jobs: TtsJobRow[]
}

export interface RequeueResponse {
  requeued: number
  message: string
}

export interface VoiceChoice {
  label: string
  value: string
}

export type AvatarCacheStatus = 'LIVE' | 'STALE' | 'TEST' | 'MISSING'

export interface AvatarCacheEntry {
  video_name: string
  source_name?: string | null
  duration_secs?: number | null
  avatar_id: string
  size_bytes: number
  built_at: string
  status: AvatarCacheStatus
}

export interface ClearStaleResponse {
  removed: string[]
  freed_bytes: number
  message: string
}

export interface SampleAudioChoice {
  label: string
  path: string
}

export interface LivestreamEnqueueResponse {
  status: Record<string, unknown>
  message: string
}

export interface TtsConfigOut {
  default_provider: string
  values: Record<string, string>
  providers: Record<string, unknown>[]
}

export interface TtsConfigSaveRequest {
  default_provider: string
  values: Record<string, string>
}

// [key, label, is_secret]
export type TtsFieldSpec = [string, string, boolean]

export interface TtsConfigSchema {
  common_fields: TtsFieldSpec[]
  provider_fields: Record<string, TtsFieldSpec[]>
  provider_labels: Record<string, string>
  curated_voices: Record<string, VoiceChoice[]>
}

export interface VoiceConnectResponse {
  voices: VoiceChoice[]
  message: string
}

export interface DriveStatus {
  enabled: boolean
  configured: boolean
  folder_id?: string | null
  folder_name?: string | null
  folder_link?: string | null
  error?: string | null
}

export interface DriveFile {
  id: string
  name: string
  mime_type: string
  web_view_link?: string | null
  modified_time?: string | null
  size?: number | null
  is_folder: boolean
}

export interface DriveFilesResponse {
  folder_id: string
  folder_name: string
  folder_link?: string | null
  files: DriveFile[]
}

export interface DriveReauthStatus {
  status: 'idle' | 'pending' | 'success' | 'error'
  message?: string | null
}

export interface DashboardStatus {
  configured: boolean
  dashboard_url?: string | null
  ingest_token?: string | null
  node_name?: string | null
  error?: string | null
}

// Pushed over /ws/jobs every 2s (or {error} on server-side failure)
export interface JobsSnapshot {
  jobs: JobOut[]
  priority_choices: PriorityChoice[]
}
