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
//
// **And a fourth thing, which is what this panel got wrong.** It used to say, of every
// queued item, "a worker takes it off the queue within a few seconds" — as a statement of
// fact, with nothing behind it. Nothing was draining the queue at all (motet#38): the only
// thing that ever did was a human dispatching a workflow in a repo the product's user has
// no reason to know exists, so the promise was false and the failure was silent, because a
// queued item looks the same whether it is about to move or never will.
//
// So the copy is now a function of `/v1/processing`, which reports when a worker last ran.
// Three answers, and they are deliberately three rather than two: a worker is running, no
// worker has run recently, or the question could not be asked — and the last one says
// nothing about seconds either. Guessing from the item's age instead would be the same
// mistake with more arithmetic; age says how long it has waited, never whether anything is
// coming for it.

import type { IngestionItem, ProcessingStatus } from '../api/client'

/**
 * How recently a worker must have run for one to count as running now.
 *
 * A polling worker heartbeats before **every** claim, so a busy one is loud: the gap
 * between two heartbeats is one job. Five minutes rather than one is deliberate — the
 * cost of the two errors is not symmetric. Calling a live worker dead puts a red banner
 * over a pipeline that is working, which is the same class of lie motet#38 was about,
 * pointing the other way; calling a dead one live for five minutes leaves the item's own
 * age on screen saying how long it has really been.
 *
 * The residual case this does not cover is a *single* job longer than this — a large TTS
 * render is the realistic one. Two things keep that from becoming a contradiction rather
 * than merely a delay: the banner ignores items a worker has already picked up, and each
 * item's own line still says "running now" for the one actually in flight.
 */
const WORKER_FRESH_MS = 5 * 60_000

type WorkerState = 'running' | 'idle' | 'never' | 'unknown'

/**
 * Whether anything is draining the queues, from the heartbeat the API reports.
 *
 * Aged against the **server's** clock, which the same response carries, so a browser whose
 * own clock is wrong — resumed from sleep, an unsynced VM — cannot report a healthy worker
 * as gone. `Date.now()` is only the fallback for a response too old to carry one.
 */
export function workerState(processing: ProcessingStatus | null): WorkerState {
  // Null is the route answering nothing — an older API, or a failed fetch. It is not
  // "no worker": the panel must not report an outage as an idle queue.
  if (!processing) return 'unknown'
  if (!processing.worker_last_seen_at) return 'never'
  const seen = new Date(processing.worker_last_seen_at).getTime()
  if (!Number.isFinite(seen)) return 'unknown'
  return serverNow(processing) - seen <= WORKER_FRESH_MS ? 'running' : 'idle'
}

/** The server's clock at the moment it answered, or this browser's if it did not say. */
export function serverNow(processing: ProcessingStatus | null): number {
  const stamp = processing ? new Date(processing.now).getTime() : Number.NaN
  return Number.isFinite(stamp) ? stamp : Date.now()
}

export function Processing({
  items,
  unavailable = false,
  processing = null,
}: {
  items: IngestionItem[]
  unavailable?: boolean
  processing?: ProcessingStatus | null
}) {
  const worker = workerState(processing)
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

  // `attempts === 0` and not merely `pending`: an item a worker has already claimed is
  // still `pending` until it succeeds, and its own line two elements down correctly says
  // "running now". A banner saying nothing is processing, over an item that says it is
  // being processed, is worse than either sentence alone.
  const stalled =
    items.some((item) => item.state === 'pending' && item.attempts === 0) && !isMoving(worker)

  return (
    <section className="processing" aria-labelledby="processing-heading">
      <h3 id="processing-heading">Processing</h3>
      <p className="hint">{summarise(items)}</p>
      {/* Above the list, once, rather than repeated under every queued item: it is one
          fact about the deployment and it is the reason none of them is moving. */}
      {stalled && (
        <p className="stalled" role="status">
          {stalledReason(processing, worker)}
        </p>
      )}
      <ul className="items">
        {items.map((item) => (
          <li key={item.id} className={`ingestion ${item.state}`}>
            <div className="item-head">
              <strong>{item.title}</strong>
              <span className={`badge ${item.state}`}>{badge(item)}</span>
            </div>
            <p className="hint">{explain(item, worker, serverNow(processing))}</p>
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

/** Whether a queued item has any reason to expect to be picked up. */
function isMoving(worker: WorkerState): boolean {
  return worker === 'running' || worker === 'unknown'
}

/**
 * Why nothing is moving, in the words the two cases actually deserve.
 *
 * "No worker has ever run" and "one ran and stopped" are a misconfigured deployment and a
 * stopped one. Collapsing them into "processing is not running" would throw away the one
 * detail that says which — and the timestamp is the thing an owner can act on.
 */
function stalledReason(processing: ProcessingStatus | null, worker: WorkerState): string {
  if (worker === 'never') {
    return (
      'Nothing is processing: no worker has ever drained this queue. Queued items will ' +
      'sit here until one runs.'
    )
  }
  const seen = processing?.worker_last_seen_at
  const when = seen ? ago(seen, serverNow(processing)) : 'a while ago'
  return (
    `Nothing is processing right now — a worker last ran ${when}. ` +
    'Queued items will sit here until one runs again.'
  )
}

function explain(item: IngestionItem, worker: WorkerState, now: number): string {
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
    // The age is here and not only in the banner because it is per item: after a stall
    // clears, the thing worth knowing is which of these has been waiting twenty minutes.
    const queued = `Queued ${ago(item.created_at, now)}.`
    if (worker === 'running') return `${queued} A worker is draining the queue now.`
    if (worker === 'unknown') return `${queued} Waiting for a worker to take it.`
    return `${queued} Nothing is draining the queue, so it is not moving yet.`
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
/**
 * "4m ago" — the mirror of {@link relative}, for a moment already past.
 *
 * Anything at or in the future reads as "just now" rather than as a negative age: two
 * clocks a second apart is normal and "-1s ago" is not a thing to show anybody.
 */
export function ago(iso: string, now: number = Date.now()): string {
  const seconds = Math.round((now - new Date(iso).getTime()) / 1000)
  if (!Number.isFinite(seconds) || seconds <= 0) return 'just now'
  if (seconds < 60) return `${seconds}s ago`
  const minutes = Math.round(seconds / 60)
  if (minutes < 60) return `${minutes}m ago`
  const hours = Math.round(minutes / 60)
  if (hours < 24) return `${hours}h ago`
  return `${Math.round(hours / 24)}d ago`
}

export function relative(iso: string, now: number = Date.now()): string {
  const seconds = Math.round((new Date(iso).getTime() - now) / 1000)
  if (!Number.isFinite(seconds) || seconds <= 0) return 'now'
  if (seconds < 60) return `in ${seconds}s`
  const minutes = Math.round(seconds / 60)
  if (minutes < 60) return `in ${minutes}m`
  return `in ${Math.round(minutes / 60)}h`
}
