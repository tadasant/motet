import { describe, expect, it } from 'vitest'

import { relative } from './Processing'

const NOW = Date.parse('2026-08-25T22:00:00Z')

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
