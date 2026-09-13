// The summary line and the one primary action, sticky under the top bar.
//
// Three counts in one sentence — waiting for you, ready to brief, read — and "Make an
// episode" as the only primary button on the screen. The cap is folded into it: a small
// "20 min" toggle beside the button opens a popover with the number, so the cap is one
// click away and never a co-equal control. The button itself makes the episode with
// whatever the cap is, which is what the App test asserts (20 minutes by default).

import { useEffect, useRef, useState } from 'react'

export const DEFAULT_MAX_MINUTES = 20

export function BacklogHeader({
  waiting,
  ready,
  read,
  minutes,
  onMinutes,
  busy,
  onMakeEpisode,
  children,
}: {
  waiting: number
  ready: number
  read: number
  minutes: number
  onMinutes: (minutes: number) => void
  busy: boolean
  onMakeEpisode: () => void
  /** The search and sort controls, rendered on the header's second line. */
  children?: React.ReactNode
}) {
  const [capOpen, setCapOpen] = useState(false)
  const capRef = useRef<HTMLDivElement>(null)

  // Close on a click outside or Escape, the way the shell's account popover behaves.
  useEffect(() => {
    if (!capOpen) return
    const onDown = (event: MouseEvent) => {
      if (capRef.current && !capRef.current.contains(event.target as Node)) setCapOpen(false)
    }
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setCapOpen(false)
    }
    document.addEventListener('mousedown', onDown)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onDown)
      document.removeEventListener('keydown', onKey)
    }
  }, [capOpen])

  return (
    <header className="backlog-head">
      <div className="backlog-head-row">
        <p className="backlog-counts" aria-live="polite">
          <Count value={waiting} label="waiting for you" emphasis />
          <span className="sep">·</span>
          <Count value={ready} label="ready to brief" />
          <span className="sep">·</span>
          <Count value={read} label="read" />
        </p>
        <div className="make-episode" ref={capRef}>
          <button
            type="button"
            className="primary"
            onClick={onMakeEpisode}
            disabled={busy || ready === 0}
            title={ready === 0 ? 'Nothing unread to brief' : `Everything unread, oldest first, up to ${minutes} min`}
          >
            {busy ? 'Creating…' : 'Make an episode'}
          </button>
          <button
            type="button"
            className="cap-toggle"
            aria-haspopup="dialog"
            aria-expanded={capOpen}
            aria-label={`Episode cap: ${minutes} minutes`}
            onClick={() => setCapOpen((open) => !open)}
          >
            {minutes} min
          </button>
          {capOpen && (
            <div className="popover cap-popover" role="dialog" aria-label="Episode cap">
              <label htmlFor="episode-minutes">Cap (minutes)</label>
              <input
                id="episode-minutes"
                type="number"
                min={1}
                max={120}
                value={minutes}
                autoFocus
                onChange={(event) => onMinutes(Math.max(1, Number(event.target.value) || 1))}
              />
              <p className="hint">
                An episode takes everything unread, oldest first, until it reaches this.
              </p>
            </div>
          )}
        </div>
      </div>
      {children}
    </header>
  )
}

function Count({ value, label, emphasis = false }: { value: number; label: string; emphasis?: boolean }) {
  return (
    <span className={['count', value === 0 ? 'zero' : '', emphasis && value > 0 ? 'emphasis' : '']
      .filter(Boolean)
      .join(' ')}>
      <strong>{value}</strong> {label}
    </span>
  )
}
