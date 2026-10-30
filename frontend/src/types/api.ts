// Hand-transcribed from backend/models.py — keep in sync with that file.

export type JobStatus = 'queued' | 'rendering' | 'done' | 'failed' | 'cancelled'
export type Engine = 'musetalk' | 'latentsync' | 'fun5b'
export type Fun5bPresetKey = 'fast' | 'balanced' | 'natural' | 'natural_light'
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
  /** 0..1 while rendering (Fun-5B only for now) */
  progress?: number | null
  // engine === 'fun5b' only
  ref_image_path?: string | null
  fun5b_preset?: Fun5bPresetKey | null
  keep_background?: boolean | null
  auto_background?: boolean | null
  table_line?: number | null
  sharp_hands?: boolean | null
  has_background?: boolean | null
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

export interface EngineChoice {
  label: string
  value: string
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

export interface AkaliStatus {
  configured: boolean
  akali_url?: string | null
  worker_key?: string | null
  worker_id?: string | null
}

// Pushed over /ws/jobs every 2s (or {error} on server-side failure)
export interface JobsSnapshot {
  jobs: JobOut[]
  priority_choices: PriorityChoice[]
}

// --- Fun-5B (backend/engines.py) ---

export interface Fun5bPreset {
  key: Fun5bPresetKey
  label: string
  steps: number
  note: string
  /** measured render time of 10s of driving video on an RTX 3090 */
  seconds_per_10s: number
}

export interface Fun5bInstallStatus {
  state: 'installed' | 'running' | 'failed' | 'skipped' | 'not_installed'
  step: number
  total: number
  label: string
  message?: string | null
  /** while the models download (step 6) */
  downloaded_gb?: number | null
  models_gb: number
}

export interface Fun5bInfo {
  available: boolean
  reason?: string | null
  missing: string[]
  plate_available: boolean
  presets: Fun5bPreset[]
  defaults: {
    preset: Fun5bPresetKey
    seed: number
    keep_background: boolean
    auto_background: boolean
    sharp_hands: boolean
  }
  install: Fun5bInstallStatus
}

export interface Fun5bServerStatus {
  running: boolean
  loaded: boolean
  busy: boolean
  job_id?: number | null
  vram_mb?: number | null
  gpu_free_mb?: number | null
  gpu_total_mb?: number | null
}

export interface Fun5bStopResponse {
  stopped: boolean
  message: string
}

export interface Fun5bPlate {
  id: string
  source_name?: string | null
  created_at?: string | null
  last_used?: string | null
  size_kb: number
  has_source: boolean
}

export interface Fun5bPlateLookup {
  id: string
  cached: boolean
}
