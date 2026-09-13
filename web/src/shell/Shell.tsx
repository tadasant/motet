// The app shell: a sidebar, a top bar, and a content area. Structure only — colours and
// type are Phase 3, and every screen inside it renders as it did in the tab strip, less
// the title the top bar now carries.
//
// Under ~800px the sidebar folds into a bar across the top with a menu button; the nav
// is the same element either way, so there is one list of sections and one active state.

import { type MouseEvent, type ReactNode, useEffect, useId, useRef, useState } from 'react'

import { ChevronIcon, HOME, MenuIcon, type Section, sectionById } from './sections'

export function Shell({
  section,
  sections,
  onNavigate,
  badge,
  account,
  children,
}: {
  section: Section
  /** The sections the sidebar offers — every one this caller can use. */
  sections: Section[]
  onNavigate: (path: string) => void
  /** A count on the Backlog item: what has been pasted and is not a news item yet. */
  badge?: { count: number; failed: boolean } | null
  /** The right-hand end of the top bar. */
  account: ReactNode
  children: ReactNode
}) {
  const [menuOpen, setMenuOpen] = useState(false)
  const home = sectionById(HOME).path

  // Closed on any change of section, not only a click in the sidebar: the back button
  // and a screen's own navigation (a new episode opening Episodes) move it too, and under
  // 800px an open menu pushes the screen they landed on down out of sight.
  useEffect(() => setMenuOpen(false), [section.id])

  const go = (event: MouseEvent<HTMLAnchorElement>, path: string) => {
    // A plain left click stays in the app; a modified click or a middle click is the
    // browser's, so "open in new tab" keeps working on an ordinary link.
    if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return
    event.preventDefault()
    setMenuOpen(false)
    onNavigate(path)
  }

  const item = (entry: Section) => (
    <li key={entry.id}>
      <a
        href={entry.path}
        aria-current={entry.id === section.id ? 'page' : undefined}
        onClick={(event) => go(event, entry.path)}
      >
        {entry.icon}
        <span className="nav-label">{entry.label}</span>
        {entry.id === 'backlog' && badge && badge.count > 0 && (
          <span className={`tab-count${badge.failed ? ' failed' : ''}`}>{badge.count}</span>
        )}
      </a>
    </li>
  )

  return (
    <div className="shell">
      <aside className={`sidebar${menuOpen ? ' open' : ''}`}>
        <div className="brand-row">
          <a className="brand" href={home} onClick={(event) => go(event, home)}>
            Motet
          </a>
          <button
            type="button"
            className="menu-button"
            aria-label="Menu"
            aria-expanded={menuOpen}
            aria-controls="sidebar-nav"
            onClick={() => setMenuOpen((open) => !open)}
          >
            <MenuIcon />
          </button>
        </div>
        <nav id="sidebar-nav" aria-label="Screens">
          <ul className="nav-group">{sections.filter((entry) => entry.group === 'main').map(item)}</ul>
          {sections.some((entry) => entry.group === 'system') && (
            <ul className="nav-group system">
              {sections.filter((entry) => entry.group === 'system').map(item)}
            </ul>
          )}
        </nav>
      </aside>

      <div className="frame">
        <header className="topbar">
          <h1 className="page-title">{section.label}</h1>
          <div className="topbar-right">{account}</div>
        </header>
        <main className={section.layout}>{children}</main>
      </div>
    </div>
  )
}

/**
 * A button that opens a small panel under itself, and closes it on an outside click or
 * Escape. A disclosure, not `role="menu"` or a dialog: the panel holds a text field as
 * well as actions, and nothing here traps or moves focus the way a dialog promises to.
 */
export function Popover({
  label,
  children,
}: {
  label: ReactNode
  children: ReactNode
}) {
  const [open, setOpen] = useState(false)
  const root = useRef<HTMLDivElement>(null)
  const button = useRef<HTMLButtonElement>(null)
  const panel = useId()

  useEffect(() => {
    if (!open) return
    const onPointerDown = (event: PointerEvent) => {
      if (root.current && !root.current.contains(event.target as Node)) setOpen(false)
    }
    // Back to the button, which is where focus came from: the panel it may have been in
    // is about to unmount, and focus would otherwise fall to the page.
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return
      setOpen(false)
      button.current?.focus()
    }
    document.addEventListener('pointerdown', onPointerDown)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('pointerdown', onPointerDown)
      document.removeEventListener('keydown', onKey)
    }
  }, [open])

  return (
    <div className="popover-root" ref={root}>
      <button
        ref={button}
        type="button"
        className="popover-button"
        aria-expanded={open}
        aria-controls={panel}
        onClick={() => setOpen((value) => !value)}
      >
        {label}
        <ChevronIcon />
      </button>
      {open && (
        <div className="popover" id={panel}>
          {children}
        </div>
      )}
    </div>
  )
}
