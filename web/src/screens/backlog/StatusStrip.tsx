// One line about the machine: is anything draining the queues, and is anything in flight.
//
// A strip rather than a card, because most of the time it has one short thing to say —
// and the thing it says is a function of `/v1/processing`, never of an item's age
// (motet#38: age says how long something has waited, not whether anything is coming for
// it). Three worker answers, deliberately three: running, idle-or-never, and "could not
// ask". The last is not the same as idle and must not read like it.
//
// When items are in flight the strip grows a count and a disclosure, and the disclosure
// is the existing Processing panel unchanged — per-item state, retry schedule, the error
// verbatim. Open by default only when something has failed, because a failure is the one
// thing on this line that wants reading rather than glancing.
//
// Held items are subtracted first. The API still counts a held item as `pending`
// (proto/issues/09, "Held items count as pending in /v1/ingestion"), so without this the
// strip would say "11 processing" about eleven items nobody has asked it to process —
// which is the contradiction the before-screenshot shows.

import type { IngestionItem, ProcessingStatus } from '../../api/client'
import { Processing, ago, serverNow, workerState } from '../Processing'

export function StatusStrip({
  ingestion,
  unavailable,
  processing,
  heldIds,
}: {
  ingestion: IngestionItem[]
  unavailable: boolean
  processing: ProcessingStatus | null
  heldIds: Set<string>
}) {
  const items = ingestion.filter((item) => !heldIds.has(item.id))
  const pending = items.filter((item) => item.state === 'pending').length
  const failed = items.filter((item) => item.state === 'failed').length
  const added = items.filter((item) => item.state === 'integrated').length
  const worker = workerState(processing)
  const now = serverNow(processing)

  const workerLine = (() => {
    if (unavailable) return 'Could not check what is still being processed.'
    switch (worker) {
      case 'running':
        return `Worker running · last pass ${ago(processing?.worker_last_seen_at ?? '', now)}`
      case 'idle':
        return `Worker idle · last ran ${ago(processing?.worker_last_seen_at ?? '', now)}`
      case 'never':
        return 'No worker has ever run'
      case 'unknown':
        return 'Worker status unknown'
    }
  })()

  const dot = unavailable
    ? 'unknown'
    : worker === 'running'
      ? 'running'
      : worker === 'unknown'
        ? 'unknown'
        : 'idle'

  const inFlight: string[] = []
  if (pending) inFlight.push(`${pending} processing`)
  if (failed) inFlight.push(`${failed} failed`)
  if (added) inFlight.push(`${added} just added`)

  if (items.length === 0) {
    return (
      <p className={`status-strip ${dot}`} role="status">
        <span className="status-dot" aria-hidden="true" />
        {workerLine}
      </p>
    )
  }

  return (
    <details className={`status-strip ${dot} ${failed ? 'has-failed' : ''}`} open={failed > 0}>
      <summary>
        <span className="status-dot" aria-hidden="true" />
        <span className="status-inflight">{inFlight.join(' · ')}</span>
        <span className="sep">·</span>
        <span className="hint">{workerLine}</span>
        <span className="hint status-more">details</span>
      </summary>
      <Processing items={items} unavailable={unavailable} processing={processing} />
    </details>
  )
}
