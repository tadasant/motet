// What has been pasted but is not in the backlog yet.
//
// This is the answer to "I pasted something, it said it was pending, and I never saw it
// again." The backlog lists *news items*, and an item that fails ingestion never becomes
// one — so the only surface that could have shown the failure was structurally incapable
// of it. Everything the system already knew (queued, retrying, gave up, and why) went
// nowhere a person could look.
//
// Three states, and the distinction between the first two is the point: an item on its
// fourth attempt is not the same as an item sitting there, and a single spinner for both
// says nothing. `attempts`, `next_attempt_at` and `last_error` come straight off the
// contract precisely so this can say which.

import type { IngestionItem } from '../api/client'

export function Processing({ items }: { items: IngestionItem[] }) {
  if (items.length === 0) return null
  const stuck = items.filter((item) => item.state === 'failed').length

  return (
    <section className="processing" aria-labelledby="processing-heading">
      <h3 id="processing-heading">Processing</h3>
      <p className="hint">
        {items.length} item{items.length === 1 ? '' : 's'} on the way in
        {stuck > 0 ? `, ${stuck} of them stuck` : ''}.
      </p>
      <ul className="items">
        {items.map((item) => (
          <li key={item.id} className={`ingestion ${item.state}`}>
            <div className="item-head">
              <strong>{item.title}</strong>
              <span className={`badge ${item.state}`}>{badge(item)}</span>
            </div>
            <p className="hint">{explain(item)}</p>
            {item.last_error && (
              // The reason, verbatim. A truncated or prettified vendor error is one a
              // person cannot search for and cannot paste into an issue.
              <p className="reason" role={item.state === 'failed' ? 'alert' : undefined}>
                {item.last_error}
              </p>
            )}
          </li>
        ))}
      </ul>
    </section>
  )
}

/** The one-word state. `last_error` on a pending item means an attempt has already lost. */
function badge(item: IngestionItem): string {
  if (item.state === 'integrated') return 'Added'
  if (item.state === 'failed') return 'Failed'
  return item.last_error ? 'Retrying' : 'Queued'
}

function explain(item: IngestionItem): string {
  if (item.state === 'integrated') {
    return 'Integrated. It is in the backlog below — under whatever title dedup settled on.'
  }
  if (item.state === 'failed') {
    return (
      `Gave up after ${item.attempts} attempt${item.attempts === 1 ? '' : 's'}. ` +
      'Nothing further will happen to it: paste it again once the reason below is fixed.'
    )
  }
  if (item.attempts === 0) {
    return 'Queued. A worker takes it off the queue within a few seconds.'
  }
  if (item.next_attempt_at) {
    return (
      `Attempt ${item.attempts} of ${item.max_attempts} failed. ` +
      `Trying again ${relative(item.next_attempt_at)}.`
    )
  }
  return `Attempt ${item.attempts} of ${item.max_attempts}, running now.`
}

/**
 * "in 30s" — a duration rather than a clock time.
 *
 * A backoff is a wait, and a wait is what someone standing there wants to know the length
 * of; "at 21:47:03" makes them do the subtraction. Anything already due reads as "now",
 * because a schedule in the past means the worker has simply not got to it yet.
 */
export function relative(iso: string, now: number = Date.now()): string {
  const seconds = Math.round((new Date(iso).getTime() - now) / 1000)
  if (!Number.isFinite(seconds) || seconds <= 0) return 'now'
  if (seconds < 60) return `in ${seconds}s`
  const minutes = Math.round(seconds / 60)
  if (minutes < 60) return `in ${minutes}m`
  return `in ${Math.round(minutes / 60)}h`
}
