// Where an episode is between "make it" and a file to play: the pure reading, and the two
// surfaces that render it.
//
// The complaint this answers is Tadas's, on a production episode: "Says queued but unclear
// to me when it's gonna process." Every test below is one thing that sentence wanted to
// know — which step, how far, how long, and whether anything is coming for it.

import { render, screen, within } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import type { Episode } from '../api/client'
import { EpisodeScreen } from './EpisodeScreen'
import { Episodes } from './Episodes'
import {
  type BuildProgress,
  describeBuildProgress,
  describeRowProgress,
  formatElapsed,
} from './episodeProgress'

const BASE: BuildProgress = {
  step: 'assemble',
  stage: 'queued',
  steps_done: 0,
  steps_total: 3,
  news_items: 0,
  claims: 0,
  segments_rendered: 0,
  elapsed_ms: 12_000,
  estimate_ms: null,
  estimate_samples: 0,
  attempt: 0,
  max_attempts: 5,
  next_attempt_at: null,
  error: null,
  waiting_on_worker: false,
  worker_starting: false,
}

const at = (over: Partial<BuildProgress>): BuildProgress => ({ ...BASE, ...over })

describe('the clock', () => {
  it('reads as time rather than as a millisecond count', () => {
    expect(formatElapsed(0)).toBe('just started')
    expect(formatElapsed(12_000)).toBe('12s')
    expect(formatElapsed(93_000)).toBe('1m 33s')
    expect(formatElapsed(3_900_000)).toBe('1h 05m')
  })
})

describe('describeBuildProgress', () => {
  it('names the step a queued episode is waiting for, never a bare "queued"', () => {
    expect(describeBuildProgress(BASE).headline).toBe('Waiting for a worker to start')
    expect(describeBuildProgress(at({ step: 'script' })).headline).toBe(
      'Queued to write the script',
    )
    expect(describeBuildProgress(at({ step: 'tts' })).headline).toBe('Queued to record the audio')
  })

  it('says which step of three, so "queued" has a position as well as a name', () => {
    const shown = describeBuildProgress(at({ step: 'script', steps_done: 1 }))
    expect(shown.detail).toBe('Step 2 of 3 · writing the script')
  })

  it('counts the stories the script is being written for', () => {
    const shown = describeBuildProgress(
      at({ step: 'script', stage: 'running', steps_done: 1, news_items: 8 }),
    )
    expect(shown.headline).toBe('Writing the script for 8 stories')
    expect(shown.count).toBe('8 stories')
  })

  it('counts segments through the render, which is the one step with an inside', () => {
    const shown = describeBuildProgress(
      at({ step: 'tts', stage: 'running', steps_done: 2, news_items: 9, segments_rendered: 4 }),
    )
    expect(shown.headline).toBe('Recording the audio')
    expect(shown.count).toBe('Recorded 4 of 9 segments · 5 left')
    expect(shown.fraction).toBeCloseTo(4 / 9)
  })

  it('leaves the bar indeterminate where there is nothing honest to count', () => {
    // Assembly and scripting are one model call each: a fraction there would be invented.
    expect(describeBuildProgress(at({ stage: 'running' })).fraction).toBeNull()
    expect(describeBuildProgress(at({ step: 'script', stage: 'running' })).fraction).toBeNull()
  })

  it('shows elapsed time alone when too few episodes have finished to estimate from', () => {
    expect(describeBuildProgress(at({ elapsed_ms: 45_000 })).timing).toBe('45s so far')
  })

  it('labels an estimate as an estimate and says what it is made of', () => {
    const shown = describeBuildProgress(
      at({ elapsed_ms: 45_000, estimate_ms: 93_000, estimate_samples: 5 }),
    )
    expect(shown.timing).toBe(
      '45s so far · usually about 1m 33s (an estimate, from the last 5 episodes)',
    )
  })

  it('never turns the estimate into a countdown that could go negative', () => {
    const shown = describeBuildProgress(
      at({ elapsed_ms: 600_000, estimate_ms: 93_000, estimate_samples: 5 }),
    )
    expect(shown.timing).toContain('10m 00s so far')
    expect(shown.timing).not.toContain('remaining')
    expect(shown.timing).not.toContain('-')
  })

  it('says plainly when nothing will build it, rather than spinning', () => {
    const shown = describeBuildProgress(at({ waiting_on_worker: true }))
    expect(shown.tone).toBe('stalled')
    expect(shown.detail).toBe(
      'No worker has run in the last five minutes, so this will not move until one does.',
    )
  })

  it('says a worker is starting, not missing, while the one just asked for boots', () => {
    // Production's shape: the worker is one-shot, so at the moment of creation the last
    // heartbeat is always stale and the container the API asked for is a minute away. The
    // sentence is the sync panel's own (motet#137), so the two panels never disagree.
    const shown = describeBuildProgress(at({ worker_starting: true }))
    expect(shown.headline).toBe('Starting a worker to build the episode')
    expect(shown.detail).toBe('A worker has been asked for — that usually takes a minute or two.')
    expect(shown.tone).toBe('working')
  })

  it('treats an API that does not send worker_starting as not starting', () => {
    const legacy = { ...BASE } as Partial<BuildProgress>
    delete legacy.worker_starting
    expect(describeBuildProgress(legacy as BuildProgress).headline).toBe(
      'Waiting for a worker to start',
    )
  })

  it('reports a retry with the attempt, the step and what the last one said', () => {
    const shown = describeBuildProgress(
      at({ step: 'script', stage: 'retrying', attempt: 3, error: 'OpenRouter 429' }),
    )
    expect(shown.headline).toBe('Trying again after a failure (attempt 3 of 5)')
    expect(shown.detail).toBe('Stopped while writing the script. Last attempt: OpenRouter 429')
    expect(shown.tone).toBe('working')
  })

  it('names the step a failed episode stopped at and what to do about it', () => {
    const shown = describeBuildProgress(
      at({ step: 'tts', stage: 'failed', steps_done: 2, error: 'Cartesia refused the text' }),
    )
    expect(shown.headline).toBe('Stopped while recording the audio')
    expect(shown.detail).toContain('Cartesia refused the text')
    expect(shown.detail).toContain('make the episode again')
    expect(shown.tone).toBe('error')
    // Determinate, so the bar stands still: an indeterminate one sweeps, and a moving bar
    // over a build that has stopped says the opposite of what happened.
    expect(shown.fraction).toBeCloseTo(2 / 3)
  })

  it('says how long a finished episode took rather than vanishing the moment it lands', () => {
    const shown = describeBuildProgress(
      at({ step: null, stage: 'ready', steps_done: 3, elapsed_ms: 93_000 }),
    )
    expect(shown.headline).toBe('Ready to play · made in 1m 33s')
    expect(shown.tone).toBe('done')
  })

  it('does not guess at a stage from a newer API', () => {
    const shown = describeBuildProgress(at({ stage: 'transcoding' as BuildProgress['stage'] }))
    expect(shown.headline).toBe('Working')
  })
})

describe('describeRowProgress', () => {
  it('is one clause: the step, the count where there is one, and the clock', () => {
    expect(describeRowProgress(at({ elapsed_ms: 12_000 }))).toBe(
      'queued · choosing the stories · 12s',
    )
    expect(
      describeRowProgress(
        at({ step: 'tts', stage: 'running', news_items: 9, segments_rendered: 4, elapsed_ms: 93_000 }),
      ),
    ).toBe('recording the audio, 4 of 9 · 1m 33s')
    expect(describeRowProgress(at({ stage: 'failed' }))).toBe('stopped')
  })
})

// --- the screens ------------------------------------------------------------------------

const EPISODE: Episode = {
  id: 'ep_1',
  title: 'Episode — Sat, Sep 20',
  state: 'rendering',
  duration_ms: 0,
  max_duration_ms: 1_200_000,
  audio_bytes: null,
  audio_media_type: null,
  last_error: null,
  created_at: '2026-09-20T03:00:00Z',
  published_at: null,
  listened_through_ms: 0,
  keep_in_backlog: false,
  build_progress: at({
    step: 'tts',
    stage: 'running',
    steps_done: 2,
    news_items: 9,
    claims: 31,
    segments_rendered: 4,
    elapsed_ms: 93_000,
    estimate_ms: 150_000,
    estimate_samples: 5,
  }),
  segments: [],
}

describe('the episode detail', () => {
  it('shows the step, the count, the bar and the labelled estimate', () => {
    render(
      <EpisodeScreen
        episode={EPISODE}
        onPositionReported={vi.fn()}
        onBacklogChanged={vi.fn()}
      />,
    )
    const box = screen.getByLabelText('Episode progress')
    expect(within(box).getByText(/Recording the audio/)).toBeTruthy()
    expect(within(box).getByText('Recorded 4 of 9 segments · 5 left')).toBeTruthy()
    expect(within(box).getByText(/an estimate, from the last 5 episodes/)).toBeTruthy()
    expect(box.querySelector('[role="progressbar"]')?.getAttribute('aria-valuenow')).toBe('44')
  })

  it('says a stalled build is stalled instead of drawing a moving bar', () => {
    const stalled: Episode = {
      ...EPISODE,
      state: 'pending',
      build_progress: at({ waiting_on_worker: true }),
    }
    render(
      <EpisodeScreen episode={stalled} onPositionReported={vi.fn()} onBacklogChanged={vi.fn()} />,
    )
    const box = screen.getByLabelText('Episode progress')
    expect(box.className).toContain('build-stalled')
    expect(within(box).getByText(/No worker has run in the last five minutes/)).toBeTruthy()
  })

  it('still reports a failure against an API with no build_progress', () => {
    const old: Episode = {
      ...EPISODE,
      state: 'failed',
      last_error: 'no news items match this rule',
      build_progress: null,
    }
    render(<EpisodeScreen episode={old} onPositionReported={vi.fn()} onBacklogChanged={vi.fn()} />)
    expect(screen.getByText('no news items match this rule')).toBeTruthy()
  })
})

describe('the episode shelf', () => {
  const shelf = (episode: Episode) =>
    render(
      <Episodes
        episodes={[episode]}
        openId={null}
        loaded
        unavailable={false}
        onOpen={vi.fn()}
        onBack={vi.fn()}
        onPositionReported={vi.fn()}
        onChanged={vi.fn()}
      />,
    )

  it('replaces the bare state word with the step and the clock', () => {
    shelf(EPISODE)
    expect(screen.getByText(/recording the audio, 4 of 9 · 1m 33s/)).toBeTruthy()
    expect(screen.queryByText(/· rendering$/)).toBeNull()
  })

  it('falls back to the state word against an API with no build_progress', () => {
    shelf({ ...EPISODE, build_progress: null })
    expect(screen.getByText(/· rendering/)).toBeTruthy()
  })

  it('says on the row when nothing is going to build it', () => {
    shelf({ ...EPISODE, state: 'pending', build_progress: at({ waiting_on_worker: true }) })
    expect(screen.getByText(/Not moving: no worker has run in the last five minutes/)).toBeTruthy()
  })

  it('does not wear a Working… badge over a build the server says has stopped', () => {
    // `scripting` with no job on any queue: the state word still says in progress, and
    // the server's reading — which is the one that looked at the queue — says it is over.
    shelf({
      ...EPISODE,
      state: 'scripting',
      build_progress: at({ step: 'script', stage: 'failed', steps_done: 1, error: 'Lost.' }),
    })
    expect(screen.queryByText('Working…')).toBeNull()
    expect(screen.getByText('Failed')).toBeTruthy()
    expect(screen.getByText('Lost.')).toBeTruthy()
  })
})
