// The contextual action bar: appears when something is selected, says what and how much,
// and offers the one verb the selected list has — ingest for held items, mark read or
// unread for news items. One list at a time, so the bar has one verb and no arbitration.

import type { NewsItem } from '../../api/client'
import { kchars } from './HeldList'
import type { HeldSourceItem } from './useHeld'

export type Selection = { list: 'held' | 'news'; ids: Set<string> }

export function SelectionBar({
  selection,
  held,
  news,
  busy,
  onIngest,
  onSetRead,
  onClear,
}: {
  selection: Selection
  held: HeldSourceItem[]
  news: NewsItem[]
  busy: boolean
  onIngest: () => void
  onSetRead: (read: boolean) => void
  onClear: () => void
}) {
  const n = selection.ids.size
  if (n === 0) return null

  if (selection.list === 'held') {
    const chars = held
      .filter((item) => selection.ids.has(item.id))
      .reduce((sum, item) => sum + item.chars, 0)
    return (
      <div className="selection-bar" role="region" aria-label="Selection">
        <span className="selection-summary">
          <strong>{n}</strong> selected · {kchars(chars)} chars to process
        </span>
        <button type="button" className="primary" onClick={onIngest} disabled={busy}>
          {busy ? 'Queueing…' : `Ingest ${n}`}
        </button>
        <button type="button" className="linkish" onClick={onClear} aria-label="Clear selection">
          Clear
        </button>
      </div>
    )
  }

  const picked = news.filter((item) => selection.ids.has(item.id))
  const unreadPicked = picked.filter((item) => !item.read).length
  const readPicked = picked.length - unreadPicked
  return (
    <div className="selection-bar" role="region" aria-label="Selection">
      <span className="selection-summary">
        <strong>{n}</strong> selected
      </span>
      {unreadPicked > 0 && (
        <button type="button" className="primary" onClick={() => onSetRead(true)} disabled={busy}>
          Mark {unreadPicked} read
        </button>
      )}
      {readPicked > 0 && (
        <button type="button" onClick={() => onSetRead(false)} disabled={busy}>
          Mark {readPicked} unread
        </button>
      )}
      <button type="button" className="linkish" onClick={onClear} aria-label="Clear selection">
        Clear
      </button>
    </div>
  )
}
