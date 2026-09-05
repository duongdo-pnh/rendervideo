import { Ban, CheckCircle2, Clock, Loader2, XCircle, type LucideIcon } from 'lucide-react'
import type { JobStatus } from '../../types/api'

export const STATUS_LABEL: Record<JobStatus, string> = {
  queued: 'Chờ',
  rendering: 'Đang render',
  done: 'Xong',
  failed: 'Lỗi',
  cancelled: 'Đã hủy',
}

export const STATUS_TONE: Record<JobStatus, 'neutral' | 'success' | 'warning' | 'error' | 'brand'> = {
  queued: 'neutral',
  rendering: 'brand',
  done: 'success',
  failed: 'error',
  cancelled: 'warning',
}

export const STATUS_ICON: Record<JobStatus, LucideIcon> = {
  queued: Clock,
  rendering: Loader2,
  done: CheckCircle2,
  failed: XCircle,
  cancelled: Ban,
}

export const STATUS_SPIN: Record<JobStatus, boolean> = {
  queued: false,
  rendering: true,
  done: false,
  failed: false,
  cancelled: false,
}
