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

export function Processing({
  items,
  unavailable = false,
}: {
  items: IngestionItem[]
  unavailable?: boolean
}) {
  // Said out loud rather than rendered as an empty panel. "Nothing is being processed"
  // and "I could not find out what is being processed" are different claims, and showing
  // the first when the second is true is the same lie in a smaller font.
  if (unavailable) {
    return (
      <section className="processing" aria-labelledby="processing-heading">
        <h3 id="processing-heading">Processing</h3>
        <p className="hint">
          Could not check what is still being processed. The backlog below is current;
          anything mid-ingestion is not shown.
        </p>
      </section>
    )
  }
  if (items.length === 0) return null

  return (
    <section className="processing" aria-labelledby="processing-heading">
      <h3 id="processing-heading">Processing</h3>
      <p className="hint">{summarise(items)}</p>
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

/**
 * The headline, counting each state as the thing it actually is.
 *
 * A settled item is not "on the way in": a just-added item is finished, and a failed one
 * is never arriving. Rolling all three into one number would put "3 items on the way in"
 * over a list where nothing is moving.
 */
function summarise(items: IngestionItem[]): string {
  const counts = {
    pending: items.filter((item) => item.state === 'pending').length,
    failed: items.filter((item) => item.state === 'failed').length,
    integrated: items.filter((item) => item.state === 'integrated').length,
  }
  const parts: string[] = []
  if (counts.pending) parts.push(`${counts.pending} on the way in`)
  if (counts.failed) parts.push(`${counts.failed} stuck`)
  if (counts.integrated) parts.push(`${counts.integrated} just added`)
  return `${parts.join(', ')}.`
}

/** The one-word state. `last_error` on a pending item means an attempt has already lost. */
function badge(item: IngestionItem): string {
  if (item.state === 'integrated') return 'Added'
  if (item.state === 'failed') return 'Failed'
  if (item.last_error) return 'Retrying'
  // Attempts spent with nothing to show for them means a worker holds it right now.
  // Calling that "Queued" would contradict the line directly underneath it.
  return item.attempts > 0 ? 'Working' : 'Queued'
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
