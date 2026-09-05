/** "45s" / "5p 23s" / "1h 05p" style duration, matching the old web_ui.py `_fmt_secs` helper. */
export function formatDuration(seconds: number | null | undefined, opts?: { inProgress?: boolean }): string {
  if (seconds == null || Number.isNaN(seconds)) return '—'
  const s = Math.max(0, Math.round(seconds))
  const suffix = opts?.inProgress ? '…' : ''
  if (s < 60) return `${s}s${suffix}`
  const h = Math.floor(s / 3600)
  const m = Math.floor((s % 3600) / 60)
  const sec = s % 60
  if (h > 0) return `${h}h ${String(m).padStart(2, '0')}p${suffix}`
  return `${m}p ${sec}s${suffix}`
}

export function formatDateTime(value: string | null | undefined): string {
  if (!value) return '—'
  const d = new Date(value)
  if (Number.isNaN(d.getTime())) return value
  return d.toLocaleString('vi-VN', {
    day: '2-digit',
    month: '2-digit',
    year: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  })
}

/** Same as formatDateTime but split into {time, date} so callers can stack them (time on top). */
export function formatDateTimeParts(value: string | null | undefined): { time: string; date: string } | null {
  if (!value) return null
  const d = new Date(value)
  if (Number.isNaN(d.getTime())) return null
  return {
    time: d.toLocaleTimeString('vi-VN', { hour: '2-digit', minute: '2-digit' }),
    date: d.toLocaleDateString('vi-VN', { day: '2-digit', month: '2-digit', year: 'numeric' }),
  }
}

export function formatMb(mb: number | null | undefined): string {
  if (mb == null || Number.isNaN(mb)) return '—'
  if (mb >= 1024) return `${(mb / 1024).toFixed(2)} GB`
  return `${mb.toFixed(1)} MB`
}

export function formatBytes(bytes: number | null | undefined): string {
  if (bytes == null || Number.isNaN(bytes)) return '—'
  const gb = bytes / 1024 ** 3
  if (gb >= 1) return `${gb.toFixed(2)} GB`
  const mb = bytes / 1024 ** 2
  return `${mb.toFixed(1)} MB`
}
