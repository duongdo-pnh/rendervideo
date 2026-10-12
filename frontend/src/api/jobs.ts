import { api } from './client'
import type { CancelAllResponse, CancelJobResponse, DeletedJobsResponse, EngineChoice, JobOut, JobSubmitResponse, PriorityChoice } from '../types/api'

export function listJobs(): Promise<JobOut[]> {
  return api.get('/api/jobs')
}

export function priorityChoices(): Promise<PriorityChoice[]> {
  return api.get('/api/jobs/priority-choices')
}

export function engineChoices(): Promise<EngineChoice[]> {
  return api.get('/api/jobs/engine-choices')
}

export function createJob(form: FormData): Promise<JobSubmitResponse> {
  return api.postForm('/api/jobs', form)
}

export function bumpPriority(jobId: number): Promise<JobOut> {
  return api.post(`/api/jobs/${jobId}/priority`)
}

export function reorderPriority(jobIds: number[]): Promise<number[]> {
  return api.post('/api/jobs/priority', { job_ids: jobIds })
}

export function retryJob(jobId: number): Promise<JobOut> {
  return api.post(`/api/jobs/${jobId}/retry`)
}

export function cancelJob(jobId: number): Promise<CancelJobResponse> {
  return api.post(`/api/jobs/${jobId}/cancel`)
}

export function cancelAll(): Promise<CancelAllResponse> {
  return api.post('/api/jobs/cancel-all')
}

export function deleteJob(jobId: number): Promise<DeletedJobsResponse> {
  return api.del(`/api/jobs/${jobId}`)
}

export function clearJobsByStatus(status: string): Promise<DeletedJobsResponse> {
  return api.post(`/api/jobs/clear/${status}`)
}
