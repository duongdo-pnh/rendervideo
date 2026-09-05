import { eventChannel, type EventChannel } from 'redux-saga'
import { call, put, take } from 'redux-saga/effects'
import { snapshotReceived, wsConnected, wsDisconnected } from '../slices/jobsSlice'
import type { JobsSnapshot } from '../../types/api'

type ChannelEvent =
  | { type: 'connected' }
  | { type: 'disconnected' }
  | { type: 'snapshot'; payload: JobsSnapshot }

function wsUrl(path: string): string {
  const proto = window.location.protocol === 'https:' ? 'wss' : 'ws'
  return `${proto}://${window.location.host}${path}`
}

function createJobsChannel(): EventChannel<ChannelEvent> {
  return eventChannel<ChannelEvent>((emit) => {
    let socket: WebSocket | null = null
    let reconnectTimer: ReturnType<typeof setTimeout> | null = null
    let torndown = false

    const connect = () => {
      socket = new WebSocket(wsUrl('/ws/jobs'))
      socket.onopen = () => emit({ type: 'connected' })
      socket.onmessage = (event) => {
        try {
          const data = JSON.parse(event.data)
          if (!data.error) emit({ type: 'snapshot', payload: data as JobsSnapshot })
        } catch {
          // ignore malformed frames
        }
      }
      socket.onclose = () => {
        emit({ type: 'disconnected' })
        if (!torndown) reconnectTimer = setTimeout(connect, 3000)
      }
      socket.onerror = () => socket?.close()
    }

    connect()

    return () => {
      torndown = true
      if (reconnectTimer) clearTimeout(reconnectTimer)
      socket?.close()
    }
  })
}

export function* jobsWsSaga() {
  const channel: EventChannel<ChannelEvent> = yield call(createJobsChannel)
  try {
    while (true) {
      const event: ChannelEvent = yield take(channel)
      if (event.type === 'connected') yield put(wsConnected())
      else if (event.type === 'disconnected') yield put(wsDisconnected())
      else if (event.type === 'snapshot') yield put(snapshotReceived(event.payload))
    }
  } finally {
    channel.close()
  }
}
