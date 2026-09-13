// A drawer on the right edge for one source item's lifecycle.
//
// A drawer rather than an inline block because the lifecycle card is ~600px tall and
// used to be inserted into the list in two different places — a table row under a held
// item, an `<li>` under a news item — so opening one shoved everything below it down
// the page. Over the list, the list stays where it was, and "show in backlog" from stage
// 3 has a stable list to scroll.
//
// Escape closes it. No scrim: the list behind it stays usable, which is what lets a
// person open the next item's lifecycle without closing this one first.

import { useEffect, useRef } from 'react'

import { SourceItemDetail } from '../SourceItemDetail'

export function Drawer({
  sourceItemId,
  onClose,
  onJumpToNewsItem,
}: {
  sourceItemId: string
  onClose: () => void
  onJumpToNewsItem: (newsItemId: string) => void
}) {
  const ref = useRef<HTMLElement>(null)

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose()
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [onClose])

  // A new item scrolls the drawer back to its top; the previous one may have been read
  // down to stage 3.
  useEffect(() => {
    // Guarded: jsdom has no scrollTo on elements.
    if (typeof ref.current?.scrollTo === 'function') ref.current.scrollTo({ top: 0 })
  }, [sourceItemId])

  return (
    <aside className="drawer" ref={ref} aria-label="Source item" role="dialog" aria-modal="false">
      <SourceItemDetail id={sourceItemId} onClose={onClose} onJumpToNewsItem={onJumpToNewsItem} />
    </aside>
  )
}
