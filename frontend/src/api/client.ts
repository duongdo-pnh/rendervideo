export class ApiError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

const NETWORK_ERROR_MESSAGE = 'Không thể kết nối tới máy chủ. Kiểm tra kết nối mạng và thử lại.'

async function request(url: string, init?: RequestInit): Promise<Response> {
  try {
    return await fetch(url, init)
  } catch (err) {
    console.error('[api] network error', url, err)
    throw new ApiError(0, NETWORK_ERROR_MESSAGE)
  }
}

async function toJson<T>(res: Response): Promise<T> {
  if (!res.ok) {
    let message = `Máy chủ trả về lỗi (HTTP ${res.status}).`
    try {
      const body = await res.json()
      if (body && typeof body.detail === 'string') message = body.detail
    } catch {
      // body wasn't JSON — keep the generic message
    }
    throw new ApiError(res.status, message)
  }
  if (res.status === 204) return undefined as T
  return (await res.json()) as T
}

async function toBlob(res: Response): Promise<Blob> {
  if (!res.ok) {
    let message = `Máy chủ trả về lỗi (HTTP ${res.status}).`
    try {
      const body = await res.json()
      if (body && typeof body.detail === 'string') message = body.detail
    } catch {
      // ignore
    }
    throw new ApiError(res.status, message)
  }
  return res.blob()
}

export const api = {
  get<T>(url: string): Promise<T> {
    return request(url).then((r) => toJson<T>(r))
  },
  post<T>(url: string, body?: unknown): Promise<T> {
    return request(url, {
      method: 'POST',
      headers: body === undefined ? {} : { 'Content-Type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body),
    }).then((r) => toJson<T>(r))
  },
  postForm<T>(url: string, form: FormData): Promise<T> {
    return request(url, { method: 'POST', body: form }).then((r) => toJson<T>(r))
  },
  postBlob(url: string, body?: unknown): Promise<Blob> {
    return request(url, {
      method: 'POST',
      headers: body === undefined ? {} : { 'Content-Type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body),
    }).then(toBlob)
  },
  getBlob(url: string): Promise<Blob> {
    return request(url).then(toBlob)
  },
  del<T>(url: string): Promise<T> {
    return request(url, { method: 'DELETE' }).then((r) => toJson<T>(r))
  },
}

/** Triggers a browser "Save As" for a blob the way a real download link would. */
export function saveBlob(blob: Blob, filename: string) {
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  document.body.appendChild(a)
  a.click()
  a.remove()
  URL.revokeObjectURL(url)
}
