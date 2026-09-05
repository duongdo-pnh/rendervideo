import { useEffect, useRef } from 'react'

/** Runs `fn` immediately, then again every `intervalMs`, until unmounted or `enabled` goes false. */
export function usePolling(fn: () => void | Promise<void>, intervalMs: number, enabled = true) {
  const fnRef = useRef(fn)
  fnRef.current = fn

  useEffect(() => {
    if (!enabled) return
    let cancelled = false
    const tick = () => {
      if (!cancelled) void fnRef.current()
    }
    tick()
    const id = setInterval(tick, intervalMs)
    return () => {
      cancelled = true
      clearInterval(id)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [intervalMs, enabled])
}
