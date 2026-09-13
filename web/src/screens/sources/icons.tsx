// Monochrome inline SVGs for the integration cards — `currentColor`, stroke only, no icon
// font and no dependency, the same shape `shell/sections.tsx` uses. Not brand marks: a
// mail glyph rather than Google's logo, a bookmark rather than X's. They take
// the ink they sit in; the brand's colour is not theirs to carry.

import type { ReactElement } from 'react'

import type { IntegrationId } from './catalog'

const stroke = {
  fill: 'none',
  stroke: 'currentColor',
  strokeWidth: 1.6,
  strokeLinecap: 'round' as const,
  strokeLinejoin: 'round' as const,
}

function Glyph({ children }: { children: ReactElement | ReactElement[] }) {
  return (
    <svg className="integration-icon" width="28" height="28" viewBox="0 0 24 24" aria-hidden="true" {...stroke}>
      {children}
    </svg>
  )
}

export function IntegrationIcon({ id }: { id: IntegrationId }) {
  switch (id) {
    case 'gmail':
      return (
        <Glyph>
          <rect x="3" y="5" width="18" height="14" rx="2" />
          <path d="M3.5 7l8.5 6 8.5-6" />
        </Glyph>
      )
    case 'paste':
      return (
        <Glyph>
          <rect x="6" y="5" width="12" height="16" rx="1.5" />
          <path d="M9 5V3.5h6V5M9 11h6M9 15h4" />
        </Glyph>
      )
    case 'x':
      return (
        <Glyph>
          <path d="M7 3.5h10a1 1 0 0 1 1 1V20l-6-3.5L6 20V4.5a1 1 0 0 1 1-1z" />
        </Glyph>
      )
    case 'rss':
      return (
        <Glyph>
          <circle cx="6" cy="18" r="1.5" />
          <path d="M5 11a8 8 0 0 1 8 8M5 5a14 14 0 0 1 14 14" />
        </Glyph>
      )
  }
}

/** The small check/warn/dot beside a pill; one glyph family so the pills read as a set. */
export function PillDot() {
  return <span className="pill-dot" aria-hidden="true" />
}
