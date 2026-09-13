// "Ready to brief" and, folded under it, "Read": the news items — deduped stories that an
// episode is made from.
//
// Read state is per news item (invariant 5) and the toggle writes the same column that
// "I listened to this episode" does — so marking something read here and having heard it
// on a walk are one fact, not two that drift. Read rows are the same row component in a
// `<details>`, muted, so un-reading one is the same gesture in the same place.
//
// A row expands in place to its summary and its sources; a source opens the lifecycle
// drawer. The expansion is the one thing allowed to move the list, because it is the
// row's own content and it is short.

import type { NewsItem } from '../../api/client'
import { ago } from '../Processing'
import { ItemRow, SectionHead } from './ItemRow'

export function NewsList({
  unread,
  read,
  totalUnread,
  totalRead,
  nothingAtAll,
  query,
  selected,
  expanded,
  flash,
  readOpen,
  onReadOpen,
  now,
  onToggle,
  onToggleAll,
  onExpand,
  onOpenSource,
  onSetRead,
}: {
  /** Searched and sorted, both of them. */
  unread: NewsItem[]
  read: NewsItem[]
  totalUnread: number
  totalRead: number
  /** No news items, nothing held, nothing in flight: the first-run state. */
  nothingAtAll: boolean
  query: string
  selected: Set<string>
  expanded: string | null
  flash: string | null
  /** Whether the Read fold is open — state, so a jump into it can open it and a person can close it. */
  readOpen: boolean
  onReadOpen: (open: boolean) => void
  now: number
  onToggle: (id: string) => void
  onToggleAll: (ids: string[]) => void
  onExpand: (id: string | null) => void
  onOpenSource: (sourceItemId: string) => void
  onSetRead: (item: NewsItem, read: boolean) => void
}) {
  const row = (item: NewsItem) => (
    <ItemRow
      key={item.id}
      id={`ni-${item.id}`}
      title={item.title}
      meta={
        <span className="brow-sources">
          {item.sources.length} source{item.sources.length === 1 ? '' : 's'}
        </span>
      }
      age={ago(item.created_at, now)}
      ageTitle={item.created_at}
      selected={selected.has(item.id)}
      muted={item.read}
      flash={flash === item.id}
      expanded={expanded === item.id}
      onSelect={() => onToggle(item.id)}
      onOpen={() => onExpand(expanded === item.id ? null : item.id)}
      action={
        <button
          type="button"
          className="linkish row-action"
          onClick={() => onSetRead(item, !item.read)}
        >
          {item.read ? 'Mark unread' : 'Mark read'}
        </button>
      }
    >
      <p className="brow-summary">{item.summary}</p>
      <p className="hint brow-source-list">
        {item.sources.map((source, index) => (
          <span key={source.id}>
            {index > 0 && ' · '}
            <button type="button" className="linkish hint" onClick={() => onOpenSource(source.id)}>
              {source.title || source.id}
            </button>
          </span>
        ))}
      </p>
    </ItemRow>
  )

  const unreadIds = unread.map((item) => item.id)
  const readIds = read.map((item) => item.id)
  const all = (ids: string[]) => ids.length > 0 && ids.every((id) => selected.has(id))
  const some = (ids: string[]) => ids.some((id) => selected.has(id))

  return (
    <>
      <section className="bsection news" aria-label="Ready to brief">
        <SectionHead
          title="Ready to brief"
          count={totalUnread}
          shown={unread.length}
          allSelected={all(unreadIds)}
          someSelected={some(unreadIds)}
          onToggleAll={() => onToggleAll(unreadIds)}
        >
          {totalUnread > 0 && (
            <span className="hint bsection-note">An episode takes these, oldest first, up to the cap.</span>
          )}
        </SectionHead>
        {nothingAtAll ? (
          <p className="hint bsection-empty">Nothing here yet. Paste a newsletter in.</p>
        ) : totalUnread === 0 ? (
          <p className="hint bsection-empty">
            {totalRead > 0
              ? 'Everything has been briefed. Mark something unread to brief it again.'
              : 'Nothing to brief yet. Ingest what is waiting, or paste a newsletter in.'}
          </p>
        ) : unread.length === 0 ? (
          <p className="hint bsection-empty">No unread titles match “{query}”.</p>
        ) : (
          <ul className="blist">{unread.map(row)}</ul>
        )}
      </section>

      {totalRead > 0 && (
        <details
          className="bsection read-fold"
          open={readOpen}
          onToggle={(event) => onReadOpen(event.currentTarget.open)}
        >
          <summary>
            <h3>
              Read{' '}
              <span className="tab-count">
                {read.length !== totalRead ? `${read.length} of ${totalRead}` : totalRead}
              </span>
            </h3>
          </summary>
          {read.length === 0 ? (
            <p className="hint bsection-empty">No read titles match “{query}”.</p>
          ) : (
            <>
              <div className="bsection-head sub">
                <span className="hint">Heard on a walk, or marked read here.</span>
                <label className="select-all">
                  <input
                    type="checkbox"
                    aria-label="Select all in Read"
                    checked={all(readIds)}
                    ref={(el) => {
                      if (el) el.indeterminate = some(readIds) && !all(readIds)
                    }}
                    onChange={() => onToggleAll(readIds)}
                  />
                  <span>all</span>
                </label>
              </div>
              <ul className="blist">{read.map(row)}</ul>
            </>
          )}
        </details>
      )}
    </>
  )
}
