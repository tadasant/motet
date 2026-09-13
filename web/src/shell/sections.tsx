// The sections of the app: one row per sidebar item, and the path each one owns.
//
// Icons are inline SVGs on `currentColor`, 18px, stroke only — no icon font, no
// dependency. Structure, not brand: AGENTS.md defers colour and typography to Phase 3.

import type { ReactElement } from 'react'

export type SectionId = 'backlog' | 'episodes' | 'sources' | 'paste' | 'admin'

export type Section = {
  id: SectionId
  path: string
  label: string
  icon: ReactElement
  /** The bottom group of the sidebar, separated from the everyday items. */
  group: 'main' | 'system'
  /** Whether the content area drops its reading-width cap — the admin tables are wide. */
  wide?: boolean
}

const stroke = {
  fill: 'none',
  stroke: 'currentColor',
  strokeWidth: 1.75,
  strokeLinecap: 'round' as const,
  strokeLinejoin: 'round' as const,
}

function Icon({ children }: { children: ReactElement | ReactElement[] }) {
  return (
    <svg className="icon" width="18" height="18" viewBox="0 0 24 24" aria-hidden="true" {...stroke}>
      {children}
    </svg>
  )
}

export const SECTIONS: Section[] = [
  {
    id: 'backlog',
    path: '/backlog',
    label: 'Backlog',
    group: 'main',
    icon: (
      <Icon>
        <path d="M4 6h16M4 12h16M4 18h10" />
      </Icon>
    ),
  },
  {
    id: 'episodes',
    path: '/episodes',
    label: 'Episodes',
    group: 'main',
    icon: (
      <Icon>
        <path d="M4 14v-2a8 8 0 0 1 16 0v2" />
        <rect x="3" y="14" width="4" height="6" rx="1" />
        <rect x="17" y="14" width="4" height="6" rx="1" />
      </Icon>
    ),
  },
  {
    id: 'sources',
    path: '/sources',
    label: 'Sources',
    group: 'main',
    icon: (
      <Icon>
        <path d="M4 13l2.5-8h11L20 13" />
        <path d="M4 13v6h16v-6h-5l-1.5 2h-3L9 13z" />
      </Icon>
    ),
  },
  {
    id: 'paste',
    path: '/paste',
    label: 'Paste in',
    group: 'main',
    icon: (
      <Icon>
        <rect x="6" y="5" width="12" height="16" rx="1.5" />
        <path d="M9 5V3.5h6V5M9 11h6M9 15h4" />
      </Icon>
    ),
  },
  {
    id: 'admin',
    path: '/admin',
    label: 'Admin',
    group: 'system',
    wide: true,
    icon: (
      <Icon>
        <path d="M4 7h10M18 7h2M4 17h4M12 17h8" />
        <circle cx="16" cy="7" r="2" />
        <circle cx="10" cy="17" r="2" />
      </Icon>
    ),
  },
]

export const HOME: SectionId = 'paste'

/**
 * Which section a path means. `/` is the paste-in screen, which is where the tab strip
 * used to start; an unknown path lands there too rather than on a 404, because there is
 * nothing to 404 — every path this app has is a section.
 */
export function sectionFor(path: string): Section {
  return SECTIONS.find((section) => section.path === path) ?? sectionById(HOME)
}

export function sectionById(id: SectionId): Section {
  const found = SECTIONS.find((section) => section.id === id)
  if (!found) throw new Error(`unknown section ${id}`)
  return found
}

export function MenuIcon() {
  return (
    <Icon>
      <path d="M4 7h16M4 12h16M4 17h16" />
    </Icon>
  )
}

export function ChevronIcon() {
  return (
    <svg className="icon chevron" width="14" height="14" viewBox="0 0 24 24" aria-hidden="true" {...stroke}>
      <path d="M6 9l6 6 6-6" />
    </svg>
  )
}
