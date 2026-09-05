import { createSlice, type PayloadAction } from '@reduxjs/toolkit'
import type { JobOut, JobsSnapshot, PriorityChoice } from '../../types/api'

export type WsStatus = 'connecting' | 'connected' | 'disconnected'

interface JobsState {
  jobs: JobOut[]
  priorityChoices: PriorityChoice[]
  wsStatus: WsStatus
  lastUpdated: string | null
}

const initialState: JobsState = {
  jobs: [],
  priorityChoices: [],
  wsStatus: 'connecting',
  lastUpdated: null,
}

const jobsSlice = createSlice({
  name: 'jobs',
  initialState,
  reducers: {
    wsConnected(state) {
      state.wsStatus = 'connected'
    },
    wsDisconnected(state) {
      state.wsStatus = 'disconnected'
    },
    snapshotReceived(state, action: PayloadAction<JobsSnapshot>) {
      state.jobs = action.payload.jobs
      state.priorityChoices = action.payload.priority_choices
      state.lastUpdated = new Date().toISOString()
    },
  },
})

export const { wsConnected, wsDisconnected, snapshotReceived } = jobsSlice.actions
export default jobsSlice.reducer
