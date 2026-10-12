import { useEffect, useState } from 'react'
import { engineChoices } from '../api/jobs'
import type { EngineChoice } from '../types/api'

const DEFAULT_CHOICES: EngineChoice[] = [
  { label: 'MuseTalk 1.5', value: 'musetalk' },
  { label: 'LatentSync', value: 'latentsync' },
]

/** Engine options for the "Engine" SelectField, narrowed server-side by GPU
 * (e.g. LatentSync is dropped on RTX 3080/3080 Ti). Starts from the full list
 * so the field isn't empty while the request is in flight. */
export function useEngineChoices(): EngineChoice[] {
  const [choices, setChoices] = useState<EngineChoice[]>(DEFAULT_CHOICES)

  useEffect(() => {
    let cancelled = false
    engineChoices()
      .then((c) => {
        if (!cancelled) setChoices(c)
      })
      .catch(() => {
        // Endpoint unavailable — keep the full default list rather than block the form.
      })
    return () => {
      cancelled = true
    }
  }, [])

  return choices
}
