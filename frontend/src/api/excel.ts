import { api, saveBlob } from './client'
import type { ExcelPreviewResponse, ExcelSubmitRequest, ExcelSubmitResponse } from '../types/api'

export async function downloadTemplate(): Promise<void> {
  const blob = await api.getBlob('/api/excel/template')
  saveBlob(blob, 'template.xlsx')
}

export function previewExcel(form: FormData): Promise<ExcelPreviewResponse> {
  return api.postForm('/api/excel/preview', form)
}

export function submitExcel(body: ExcelSubmitRequest): Promise<ExcelSubmitResponse> {
  return api.post('/api/excel/submit', body)
}

export async function exportResults(batchId: string): Promise<void> {
  const blob = await api.getBlob(`/api/excel/export/${encodeURIComponent(batchId)}`)
  saveBlob(blob, `ket-qua-${batchId}.xlsx`)
}
