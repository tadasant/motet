// PROTOTYPE — what has been pulled in and not yet paid for, as a hook.
//
// Connecting a source does the free, deterministic work (poll, fetch, extract) at once;
// inference is spent only when a person picks items and says "ingest". This is the data
// half of that step: the held list, polled, and the one call that queues a selection.
// The screen half is `HeldList` and the selection bar.
//
// Through the typed client rather than a raw `fetch` (proto/issues/09, "Held.tsx uses a
// raw fetch"), so a 404 from an older API reads as an `ApiError` like every other route.

import { useCallback, useEffect, useState } from 'react'

import { ApiError, type HeldSourceItem, api, apiPost } from '../../api/client'

export type { HeldSourceItem }

/** How often the held list re-asks. A sync landing has to be seen to land. */
export const HELD_POLL_MS = 5_000

export type IntegrateResult = { queued: number; skipped: number }

export async function integrateHeld(ids: string[]): Promise<IntegrateResult> {
  return apiPost('/v1/source-items/integrate', { ids })
}

export function useHeld(): {
  /** `null` until the first answer; an error leaves the last good list in place. */
  held: HeldSourceItem[] | null
  error: string
  refresh: () => void
} {
  const [held, setHeld] = useState<HeldSourceItem[] | null>(null)
  const [error, setError] = useState('')

  const refresh = useCallback(() => {
    api
      .heldSourceItems()
      .then((next) => {
        setHeld(next)
        setError('')
      })
      .catch((err: unknown) =>
        setError(err instanceof ApiError ? err.message : err instanceof Error ? err.message : String(err)),
      )
  }, [])

  useEffect(() => {
    refresh()
    const timer = window.setInterval(refresh, HELD_POLL_MS)
    return () => window.clearInterval(timer)
  }, [refresh])

  return { held, error, refresh }
}
