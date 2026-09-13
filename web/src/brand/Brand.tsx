// The Polyphony brand's drawn pieces (brand/GUIDELINES.md): the mark, the wordmark, the
// eyebrow's four voices, the mic glyph, and the waveform score. Inline SVG on purpose — no
// icon package, no image request — and every value is the reference page's.

import { type ReactNode, useId } from 'react'

import { ONE, V1, V2, V3, V4 } from './scoreData'

/** The mark: four voices converging. Sits left of the wordmark. */
export function Glyph({ size = 26 }: { size?: number }) {
  return (
    <svg className="glyph" width={size} height={(size * 22) / 26} viewBox="0 0 26 22" aria-hidden="true">
      <path d="M1 3 C8 3 10 11 25 11" fill="none" stroke="#D64B2A" strokeWidth="2.4" strokeLinecap="round" />
      <path d="M1 8 C8 8 12 11 25 11" fill="none" stroke="#D9A441" strokeWidth="2.4" strokeLinecap="round" />
      <path d="M1 14 C8 14 12 11 25 11" fill="none" stroke="#2A7F86" strokeWidth="2.4" strokeLinecap="round" />
      <path d="M1 19 C8 19 10 11 25 11" fill="none" stroke="#6B3E86" strokeWidth="2.4" strokeLinecap="round" />
    </svg>
  )
}

/**
 * `motet`, lowercase, Fraunces italic, with the mark. Never uppercase, never the sans.
 * The accessible name is the product's proper name, since a screen reader would otherwise
 * read the lowercase word as a common noun.
 */
export function Wordmark({ className = '' }: { className?: string }) {
  return (
    <span className={`wordmark ${className}`.trim()} role="img" aria-label="Motet">
      <Glyph />
      <span aria-hidden="true">motet</span>
    </span>
  )
}

/** The eyebrow's four voice dots, always together and in order. */
export function Voices() {
  return (
    <span className="voices" aria-hidden="true">
      <i />
      <i />
      <i />
      <i />
    </span>
  )
}

export function MicGlyph() {
  return (
    <svg width="12" height="14" viewBox="0 0 12 14" aria-hidden="true">
      <rect x="3.5" y="0.75" width="5" height="8" rx="2.5" fill="currentColor" />
      <path
        d="M1.5 6.5a4.5 4.5 0 0 0 9 0M6 11v2.25M3.75 13.25h4.5"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.4"
        strokeLinecap="round"
      />
    </svg>
  )
}

const STAVES = [70, 212, 354, 496]

/**
 * The motif: four waveforms, each on its own stave, converging into one ink waveform under
 * "One podcast". A hero and empty-state element, never wallpaper.
 */
export function Motif({
  quiet = false,
  caption,
  children,
}: {
  /** The empty-state size: smaller, no glow, no player under it. */
  quiet?: boolean
  caption: ReactNode
  /** Whatever sits under the score — on the landing, a picture of the transport. */
  children?: ReactNode
}) {
  // Per instance, so two scores on one page never share a gradient id.
  const fade = `staff-fade-${useId().replace(/:/g, '')}`
  return (
    <figure
      className={`motif${quiet ? ' quiet' : ''}`}
      aria-label="Four coloured audio waveforms, each on its own stave, converging into a single ink waveform."
    >
      <div className="score">
        <svg viewBox="0 0 1000 620" preserveAspectRatio="xMidYMid meet" aria-hidden="true">
          <defs>
            <linearGradient id={fade} x1="0" x2="1" y1="0" y2="0">
              <stop offset="0" stopColor="#1B1A2E" stopOpacity="1" />
              <stop offset="0.55" stopColor="#1B1A2E" stopOpacity="1" />
              <stop offset="0.85" stopColor="#1B1A2E" stopOpacity="0" />
            </linearGradient>
          </defs>
          <g stroke={`url(#${fade})`} strokeWidth="1" opacity=".22">
            {STAVES.flatMap((top) =>
              [0, 12, 24, 36, 48].map((step) => (
                <line key={top + step} x1="0" y1={top + step} x2="1000" y2={top + step} />
              )),
            )}
          </g>
          <line x1="0.75" y1="70" x2="0.75" y2="544" stroke="#1B1A2E" strokeWidth="1.5" opacity=".28" />
          <line x1="880" y1="310" x2="1000" y2="310" stroke="#1B1A2E" strokeWidth="1" opacity=".18" />
          <text className="score-label" x="14" y="52">
            Newsletters
          </text>
          <text className="score-label" x="14" y="194">
            Bookmarks
          </text>
          <text className="score-label" x="14" y="336">
            Longreads
          </text>
          <text className="score-label" x="14" y="478">
            Threads
          </text>
          <path className="voice v1" d={V1} />
          <path className="voice v2" d={V2} />
          <path className="voice v3" d={V3} />
          <path className="voice v4" d={V4} />
          <path className="one" d={ONE} />
          <text className="score-label ink" x="1000" y="248" textAnchor="end">
            One podcast
          </text>
        </svg>
      </div>
      {children}
      <figcaption className="motif-caption">{caption}</figcaption>
    </figure>
  )
}
