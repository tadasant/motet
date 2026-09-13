// The admin screen's waitlist table: who asked to join from the getmotet.com landing page.
//
// Its own component rather than more of Admin.tsx, so the operator view gains one line.
// Fetched when the screen opens and on Refresh, not on the overview's three-second poll —
// a signup is not something an operator watches arrive. Admins only, server-side: the
// route answers 403 to anybody else, whatever this renders.

import { useCallback, useEffect, useRef, useState } from 'react'

import { type AdminWaitlist as Page, api } from '../api/client'

function when(iso: string): string {
  return new Date(iso).toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' })
}

export function AdminWaitlist() {
  const [page, setPage] = useState<Page | null>(null)
  const [error, setError] = useState('')
  // null for the newest page, otherwise the cursor that fetched the one on screen.
  const [before, setBefore] = useState<number | null>(null)
  const latest = useRef(0)

  const refresh = useCallback(() => {
    const request = ++latest.current
    api
      .adminWaitlist({ before })
      .then((next) => {
        if (request !== latest.current) return
        setPage(next)
        setError('')
      })
      .catch((err: unknown) => {
        if (request !== latest.current) return
        setError(err instanceof Error ? err.message : String(err))
      })
  }, [before])

  useEffect(() => {
    refresh()
  }, [refresh])

  return (
    <section aria-label="Waitlist">
      <div className="row">
        <h2>Waitlist</h2>
        <span className="hint">
          {page ? `${page.total} ${page.total === 1 ? 'address' : 'addresses'}` : '—'}
        </span>
        <button type="button" disabled={before === null} onClick={() => setBefore(null)}>
          Newest
        </button>
        <button
          type="button"
          disabled={!page?.next_before}
          onClick={() => setBefore(page?.next_before ?? null)}
        >
          Older signups
        </button>
        <button type="button" onClick={refresh}>
          Refresh waitlist
        </button>
      </div>
      {error && <p className="error">{error}</p>}
      <table className="grid">
        <thead>
          <tr>
            <th>email</th>
            <th>joined</th>
            <th>last submitted</th>
            <th className="num">times</th>
          </tr>
        </thead>
        <tbody>
          {page?.signups.map((signup) => (
            <tr key={signup.id}>
              <td className="mono">{signup.email}</td>
              <td title={signup.created_at}>{when(signup.created_at)}</td>
              <td title={signup.last_submitted_at}>{when(signup.last_submitted_at)}</td>
              <td className="num">{signup.submissions}</td>
            </tr>
          ))}
          {page && page.signups.length === 0 && (
            <tr>
              <td colSpan={4} className="hint">
                nobody has joined yet
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </section>
  )
}
