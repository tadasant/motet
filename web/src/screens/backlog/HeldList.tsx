// "Waiting for you": what a source pulled in and nobody has yet decided to spend on.
//
// Nothing here has cost inference, and nothing here will until it is selected and
// ingested — that is the gate proto/issues/03 describes. The section says so once, in
// the head, and then gets out of the way: a row is a title, where it came from, how big
// it is and how old it is, and the title opens the lifecycle drawer.

import { ago } from '../Processing'
import { ItemRow, SectionHead } from './ItemRow'
import type { HeldSourceItem } from './useHeld'

/** "28k" — thousands of characters, the unit the size hint has always used. */
export function kchars(chars: number): string {
  if (chars < 950) return `${(chars / 1000).toFixed(1)}k`
  return `${Math.round(chars / 1000)}k`
}

export function HeldList({
  items,
  total,
  query,
  selected,
  onToggle,
  onToggleAll,
  onOpen,
  now,
  error,
}: {
  /** The rows to show — already searched and sorted. */
  items: HeldSourceItem[]
  /** How many are held in all, so the head can count what the search is hiding. */
  total: number
  query: string
  selected: Set<string>
  onToggle: (id: string) => void
  onToggleAll: (ids: string[]) => void
  onOpen: (id: string) => void
  now: number
  error: string
}) {
  const ids = items.map((item) => item.id)
  const allSelected = ids.length > 0 && ids.every((id) => selected.has(id))
  const someSelected = ids.some((id) => selected.has(id))

  return (
    <section className="bsection held" aria-label="Waiting for you">
      <SectionHead
        title="Waiting for you"
        count={total}
        shown={items.length}
        allSelected={allSelected}
        someSelected={someSelected}
        onToggleAll={() => onToggleAll(ids)}
      >
        {total > 0 && (
          <span className="hint bsection-note">
            Pulled in, not yet processed. Nothing here has cost anything.
          </span>
        )}
      </SectionHead>
      {error && (
        <p className="error" role="alert">
          Could not load what is waiting for you: {error}
        </p>
      )}
      {total === 0 ? (
        !error && (
          <p className="hint bsection-empty">
            Nothing waiting. Items your sources pull in land here until you ingest them.
          </p>
        )
      ) : items.length === 0 ? (
        <p className="hint bsection-empty">No titles here match “{query}”.</p>
      ) : (
        <ul className="blist">
          {items.map((item) => (
            <ItemRow
              key={item.id}
              title={item.title || '(untitled)'}
              meta={
                <>
                  <span className="brow-source" title={item.source_name}>
                    {shortSource(item.source_name)}
                  </span>
                  <span className="brow-size" title={`${item.chars.toLocaleString()} characters`}>
                    {kchars(item.chars)}
                  </span>
                </>
              }
              age={ago(item.received_at, now)}
              ageTitle={item.received_at}
              selected={selected.has(item.id)}
              onSelect={() => onToggle(item.id)}
              onOpen={() => onOpen(item.id)}
              action={
                <button type="button" className="linkish row-action" onClick={() => onOpen(item.id)}>
                  Details
                </button>
              }
            />
          ))}
        </ul>
      )}
    </section>
  )
}

/** "Gmail (tadas@…)" → "Gmail". The account is in the drawer and the tooltip. */
function shortSource(name: string): string {
  const paren = name.indexOf(' (')
  return paren > 0 ? name.slice(0, paren) : name
}
