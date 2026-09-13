// One row of either backlog list. The same anatomy for a held item and a news item —
// checkbox, title, meta, age, a quiet action — so that the two sections read as one
// screen and "select all" means the same thing in both.
//
//   [☐] Title, one line, ellipsis ....... meta         age   [action]
//       (expanded content, when the row has any)
//
// The title is a button: for a held item it opens the lifecycle drawer, for a news item
// it expands the row. The checkbox is the only thing that selects, on purpose — a row
// that selects on click and opens on click is a row that does the wrong one of the two
// on every second click.

import type { ReactNode } from 'react'

export function ItemRow({
  id,
  title,
  meta,
  age,
  ageTitle,
  selected,
  onSelect,
  onOpen,
  expanded = false,
  muted = false,
  flash = false,
  action,
  children,
}: {
  /** The DOM id, so a lifecycle link can scroll to this row. */
  id?: string | undefined
  title: string
  meta: ReactNode
  age: string
  ageTitle?: string | undefined
  selected: boolean
  onSelect: () => void
  onOpen: () => void
  /** Whether the title button controls an inline expansion (news) or a drawer (held). */
  expanded?: boolean
  muted?: boolean
  flash?: boolean
  action?: ReactNode
  children?: ReactNode
}) {
  const className = [
    'brow',
    selected ? 'selected' : '',
    muted ? 'muted' : '',
    flash ? 'flash' : '',
    expanded ? 'expanded' : '',
  ]
    .filter(Boolean)
    .join(' ')
  return (
    <li id={id} className={className}>
      <div className="brow-main">
        <input
          type="checkbox"
          className="brow-check"
          aria-label={`Select ${title}`}
          checked={selected}
          onChange={onSelect}
        />
        <button
          type="button"
          className="brow-title"
          onClick={onOpen}
          aria-expanded={children !== undefined ? expanded : undefined}
          title={title}
        >
          {title}
        </button>
        <span className="brow-meta">{meta}</span>
        <span className="brow-age" title={ageTitle}>
          {age}
        </span>
        {action !== undefined && <span className="brow-action">{action}</span>}
      </div>
      {expanded && children !== undefined && <div className="brow-detail">{children}</div>}
    </li>
  )
}

/** The heading of a list section: a name, a count, and select-all for the rows under it. */
export function SectionHead({
  title,
  count,
  shown,
  allSelected,
  someSelected,
  onToggleAll,
  children,
}: {
  title: string
  count: number
  /** How many of `count` the search is showing, when it is hiding any. */
  shown?: number
  allSelected: boolean
  someSelected: boolean
  onToggleAll: () => void
  children?: ReactNode
}) {
  return (
    <div className="bsection-head">
      <h3>
        {title}{' '}
        <span className="tab-count">
          {shown !== undefined && shown !== count ? `${shown} of ${count}` : count}
        </span>
      </h3>
      {children}
      {count > 0 && (
        <label className="select-all">
          <input
            type="checkbox"
            aria-label={`Select all in ${title}`}
            checked={allSelected}
            ref={(el) => {
              if (el) el.indeterminate = someSelected && !allSelected
            }}
            onChange={onToggleAll}
          />
          <span>all</span>
        </label>
      )}
    </div>
  )
}
