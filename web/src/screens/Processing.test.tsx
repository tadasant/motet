import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import type { IngestionItem, ProcessingStatus } from '../api/client'
import { Processing, ago, relative, workerState } from './Processing'

const NOW = Date.parse('2026-08-25T22:00:00Z')

const QUEUED: IngestionItem = {
  id: 'si_1',
  title: 'Something I pasted',
  state: 'pending',
  attempts: 0,
  max_attempts: 5,
  next_attempt_at: null,
  last_error: null,
  created_at: '2026-08-25T21:55:00Z',
}

const running: ProcessingStatus = { worker_last_seen_at: new Date().toISOString(), queues: [] }
const idle: ProcessingStatus = { worker_last_seen_at: '2026-08-25T20:00:00Z', queues: [] }
const never: ProcessingStatus = { worker_last_seen_at: null, queues: [] }

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
    expect(workerState(idle, NOW)).toBe('idle')
    expect(workerState(never)).toBe('never')
    expect(workerState(null)).toBe('unknown')
    expect(workerState({ worker_last_seen_at: 'not a date', queues: [] })).toBe('unknown')
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
})
