import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import type { IngestionItem, ProcessingStatus } from '../api/client'
import { Processing, ago, relative, workerState } from './Processing'

const NOW_ISO = '2026-08-25T22:00:00Z'
const NOW = Date.parse(NOW_ISO)

const QUEUED: IngestionItem = {
  id: 'si_1',
  title: 'Something I pasted',
  state: 'pending',
  attempts: 0,
  max_attempts: 5,
  next_attempt_at: null,
  last_error: null,
  created_at: '2026-08-25T21:55:00Z',
  source_kind: 'paste',
}

/** The same paste, after a worker has claimed it. Still `pending` until it succeeds. */
const CLAIMED: IngestionItem = { ...QUEUED, id: 'si_2', attempts: 1 }

/** A paste the pipeline gave up on. Re-pasting it is a repair a person can perform. */
const FAILED_PASTE: IngestionItem = {
  ...QUEUED,
  id: 'si_3',
  state: 'failed',
  attempts: 5,
  last_error: 'OpenRouter refused: 402 insufficient credits',
}

/**
 * A mailbox message that never became a source item at all — motet#35's row.
 *
 * It comes off the extract job rather than off `source_items`, which is why its title is
 * the provider's message id: nothing has read the subject line, because reading it is the
 * step that failed.
 */
const FAILED_MESSAGE: IngestionItem = {
  ...FAILED_PASTE,
  id: 'extract:41',
  title: 'Gmail message 18f2a3b4c5',
  source_kind: 'gmail',
  last_error: 'PermanentFailure: source src_gmail needs reconnecting: invalid_grant',
}

// Every fixture carries the server's clock, which is the whole point of the field: these
// assertions are then about the code rather than about what day the suite is run on.
const running: ProcessingStatus = {
  now: NOW_ISO,
  worker_last_seen_at: '2026-08-25T21:59:30Z',
  queues: [],
}
const idle: ProcessingStatus = {
  now: NOW_ISO,
  worker_last_seen_at: '2026-08-25T20:00:00Z',
  queues: [],
}
const never: ProcessingStatus = { now: NOW_ISO, worker_last_seen_at: null, queues: [] }

describe('relative', () => {
  it('reports a backoff as the wait it is', () => {
    expect(relative('2026-08-25T22:00:30Z', NOW)).toBe('in 30s')
    expect(relative('2026-08-25T22:02:00Z', NOW)).toBe('in 2m')
    expect(relative('2026-08-25T23:00:00Z', NOW)).toBe('in 1h')
  })

  it('reads anything already due as "now" rather than as a negative wait', () => {
    // A schedule in the past means the worker has not got to it yet, which is "now" to
    // the person watching — not "in -4s", and not an error.
    expect(relative('2026-08-25T21:59:56Z', NOW)).toBe('now')
    expect(relative('2026-08-25T22:00:00Z', NOW)).toBe('now')
  })

  it('does not render NaN when the timestamp is unparseable', () => {
    expect(relative('not a date', NOW)).toBe('now')
  })
})

describe('workerState', () => {
  it('separates running, idle, never-run, and could-not-ask', () => {
    // Four answers rather than two, because "no worker" and "I could not find out" must
    // not produce the same sentence — the second one is an outage in this panel, not in
    // the pipeline.
    expect(workerState(running)).toBe('running')
    expect(workerState(idle)).toBe('idle')
    expect(workerState(never)).toBe('never')
    expect(workerState(null)).toBe('unknown')
    expect(workerState({ ...never, worker_last_seen_at: 'not a date' })).toBe('unknown')
  })

  it('ages the heartbeat against the server clock, not this browser\'s', () => {
    // A laptop resumed from sleep, or an unsynced VM. Without the server's own `now` in
    // the response this reports a perfectly healthy worker as gone, permanently.
    const skewed = { ...running, now: '2026-08-25T21:59:31Z' }
    expect(workerState(skewed)).toBe('running')
  })
})

describe('ago', () => {
  it('reads an age as an age, and anything not yet past as "just now"', () => {
    expect(ago('2026-08-25T21:59:30Z', NOW)).toBe('30s ago')
    expect(ago('2026-08-25T21:55:00Z', NOW)).toBe('5m ago')
    expect(ago('2026-08-25T19:00:00Z', NOW)).toBe('3h ago')
    expect(ago('2026-08-23T22:00:00Z', NOW)).toBe('2d ago')
    expect(ago('2026-08-25T22:00:01Z', NOW)).toBe('just now')
    expect(ago('not a date', NOW)).toBe('just now')
  })
})

describe('Processing', () => {
  // motet#38: the panel used to state, of every queued item and whatever was true, that a
  // worker would take it within a few seconds. Nothing was draining the queue at all.
  it('does not promise seconds when nothing is draining the queue', () => {
    render(<Processing items={[QUEUED]} processing={never} />)
    expect(screen.getByText(/no worker has ever drained this queue/)).toBeDefined()
    expect(screen.queryByText(/within a few seconds/)).toBeNull()
  })

  it('says when a worker last ran, so a stopped one is not mistaken for a slow one', () => {
    render(<Processing items={[QUEUED]} processing={idle} />)
    expect(screen.getByText(/a worker last ran/)).toBeDefined()
  })

  it('says a worker is on it when one actually is', () => {
    render(<Processing items={[QUEUED]} processing={running} />)
    expect(screen.getByText(/A worker is draining the queue now/)).toBeDefined()
    expect(screen.queryByText(/Nothing is processing/)).toBeNull()
  })

  it('claims nothing either way when the question could not be asked', () => {
    render(<Processing items={[QUEUED]} processing={null} />)
    expect(screen.getByText(/Waiting for a worker to take it/)).toBeDefined()
    expect(screen.queryByText(/Nothing is processing/)).toBeNull()
  })

  it('shows how long an item has been queued, so a stalled queue looks stalled', () => {
    render(<Processing items={[QUEUED]} processing={never} />)
    expect(screen.getByText(/Queued \d+[smhd] ago/)).toBeDefined()
  })

  it('offers re-pasting for a failed paste, which is a thing a person can actually do', () => {
    render(<Processing items={[FAILED_PASTE]} processing={running} />)
    expect(screen.getByText(/paste it again once the reason below is fixed/)).toBeDefined()
  })

  it('does not tell someone to re-paste a mailbox message they have never seen', () => {
    // The advice has to be true of the row it is under. A polled message has no text on
    // this screen to paste, and the poll cursor moved past it in the same transaction
    // that queued the fetch, so no later poll offers it again either.
    render(<Processing items={[FAILED_MESSAGE]} processing={running} />)
    expect(screen.getByText(/the mailbox poll has already moved past this message/)).toBeDefined()
    expect(screen.queryByText(/paste it again/)).toBeNull()
    // And it says which message, because the subject line is exactly what was never read.
    expect(screen.getByText('Gmail message 18f2a3b4c5')).toBeDefined()
    expect(screen.getByText(/invalid_grant/)).toBeDefined()
  })

  it('claims no particular repair for a source kind it has never heard of', () => {
    // X bookmarks are the next source kind. An unknown one has no more claim to the
    // mailbox sentence than to the paste one, and a confident wrong repair is worse than
    // saying only what is certain.
    render(
      <Processing items={[{ ...FAILED_MESSAGE, source_kind: 'x' }]} processing={running} />,
    )
    expect(screen.getByText(/Nothing further will happen to it\.$/)).toBeDefined()
    expect(screen.queryByText(/paste it again/)).toBeNull()
    expect(screen.queryByText(/mailbox poll/)).toBeNull()
  })

  it('does not accuse the queue over an item a worker has already claimed', () => {
    // A claimed item is still `pending`, and its own line says "running now". A banner
    // saying nothing is processing above it would be the panel contradicting itself —
    // which is what a heartbeat older than one long job would otherwise produce.
    render(<Processing items={[CLAIMED]} processing={idle} />)
    expect(screen.getByText(/running now/)).toBeDefined()
    expect(screen.queryByText(/Nothing is processing/)).toBeNull()
  })
})
