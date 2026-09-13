# Motet brand guidelines — "Polyphony"

**Decided by Tadas, 2026-09-12**, at the end of a local brand exploration. Five directions and
three variants of the winner were built as HTML/CSS landing pages and reviewed side by side;
the pick is direction 1 "Polyphony", variant A "Waveform score". The rejected ones are kept
in [`explorations/`](explorations/README.md) for context.

**The reference is [`polyphony/index.html`](polyphony/index.html)**, a single HTML+CSS file
with no JavaScript. Where this document and that page disagree on a value, the page wins;
this document explains the intent. `brand/serve.sh` serves it on `localhost:7101`.

---

## Positioning

> **Motet turns content you trust into an interactive podcast you can listen to on the go.**

Two value props, in this order, and every surface should be able to point at both:

1. **Learn on the go, from the sources you already trust.** Newsletters, bookmarks, threads
   the reader chose. Trust comes from the reader having picked the source, not from us
   verifying anything.
2. **Drill down interactively.** Say something out loud mid-episode; it goes deeper on that
   story, then picks the episode back up.

**Grounding and citations are a property of the product, not the pitch.** They stay in the
product (invariant 3 is untouched) but marketing and UI copy do not lead with them.

## The idea

A motet is a choral form where several texts are sung at once and layered into one coherent
piece. Motet's four voices are the reader's sources; they resolve into one line, the podcast.
Everything visual is a restatement of that: many coloured voices on the left, one ink line on
the right.

The tone is **editorial and warm**: a score, not a dashboard and not a newspaper. Cultured
but modern. Whitespace is generous; hierarchy comes from type size and weight rather than
from boxes and borders.

## Colour

| Token | Hex | Role |
|---|---|---|
| `--parchment` | `#F4EFE6` | page ground |
| `--surface` | `#EAE3D6` | secondary surfaces: tracks, pills, tinted cards |
| `--ink` | `#1B1A2E` | text, primary buttons, the resolved "one podcast" line |
| `--ink-soft` | `rgba(27,26,46,.66)` | secondary text |
| `--ink-mute` | `rgba(27,26,46,.42)` | labels, captions, timestamps |
| `--rule` | `rgba(27,26,46,.14)` | hairlines and card borders |
| `--vermilion` | `#D64B2A` | voice 1 |
| `--ochre` | `#D9A441` | voice 2 |
| `--teal` | `#2A7F86` | voice 3 |
| `--plum` | `#6B3E86` | voice 4 |

**The four voice hues are the signature and are used sparingly.** They appear together, in
that order, and they mean *which source is singing*: the eyebrow dots, the motif, the
hairline under the primary button, the played portion of a scrubber (as a
vermilion→ochre→teal→plum gradient), a chapter's source dot. They are not fill colours for
panels and they are not a rainbow for its own sake. Where voices cross, use
`mix-blend-mode: multiply` so overlaps darken like ink.

**Semantic states are ink at opacity, not the voice hues.** Pending, done, disabled, empty:
all `--ink-soft` / `--ink-mute` / `--rule`. The one exception is vermilion, which may double
as destructive/error because it is the natural red; ochre, teal and plum stay off status
duty so that a source colour never reads as a warning.

**Dark mode is not designed yet.** If a surface needs one before it is, derive it by swapping
ground and ink (ink ground, parchment text) and keep the four hues as they are; do not
invent a fifth palette.

## Typography

| Role | Face | Notes |
|---|---|---|
| Display, headings, wordmark, italic asides | **Fraunces** | variable; `font-variation-settings: "SOFT" 100`, `opsz` matched to size (144 for hero, 72 for the wordmark, 36 for section leads, 14 for small italic captions). Weight 400. Italic for emphasis inside a heading ("One thing *worth hearing*"). |
| UI, body, labels, numerals | **Instrument Sans** | 400–600. Body 17px / 1.6 desktop, 16px on phones. Labels 11–13px, 600, uppercase, `letter-spacing: .14em`. Times and counts use `font-variant-numeric: tabular-nums`. |

Scale, from the reference page: hero `clamp(46px, 5.4vw, 78px)` at line-height ~1.0 and
`letter-spacing: -0.015em`; section titles `clamp(34px, 3.4vw, 48px)`; lead paragraphs
`clamp(17px, 1.35vw, 20px)` in `--ink-soft`; captions 13–15px, italic Fraunces where they
are prose and small-caps Instrument Sans where they are labels.

Both faces are on Google Fonts under the OFL. The reference loads them with a `<link>` and
`display=swap`; the SPA and iOS app may self-host instead. Whatever the mechanism, text must
render in the fallback stack (`Iowan Old Style, Palatino, Georgia, serif` and
`Helvetica Neue, Arial, sans-serif`) before the webfont arrives.

## Wordmark and mark

- The wordmark is **`motet`, lowercase, Fraunces italic**, `SOFT 100`, `opsz 72`,
  `letter-spacing: -0.03em`. Never uppercase, never in the sans.
- The mark is the **four converging lines** at small size: four coloured strokes resolving
  into one ink stroke, as an SVG. It sits left of the wordmark in the nav and stands alone as
  an app icon or podcast artwork. At favicon sizes the waveform treatment does not survive;
  use the smooth-curve version of the mark there (see `explorations/01-polyphony`).
- Lockups exist on parchment and on ink; the identity card at the bottom of the reference
  page shows both.

## The motif

Four **audio waveforms**, one per voice hue, each starting on its own stave at the left and
converging at the right into a single ink waveform under a small-caps label "ONE PODCAST".
The staves and the tie line are faint `--rule` strokes. This is a hero and marketing element,
and it is the podcast artwork; it is not wallpaper. In-app it belongs on the landing/sign-in
screen, on an empty backlog, and behind or beside a player, not on every list row.

## Components, as the reference page draws them

- **Primary button**: ink pill, parchment text, Instrument Sans 600 16px, `border-radius:
  999px`, a 22px play glyph in a parchment circle at the left, and a four-hue hairline
  along its bottom edge. Hover lifts 1px and deepens the shadow.
- **Secondary action**: a text link in ink, 500 weight, with an arrow and a `--rule`
  underline that darkens to ink on hover. No outlined buttons.
- **Pills** (speed, "hold to ask", sign in): `--surface` fill or a `--rule` border, 13–14px,
  fully rounded.
- **Transport**: 46px ink play circle; a 6px `--surface` track with the played portion in
  the four-hue gradient and a 16px ink knob; tabular times in `--ink-soft`; a speed pill and
  a mic pill reading "hold to ask". This is the reference for any player UI, web or iOS.
- **Cards**: parchment or `--surface`, a 1px `--rule` border, `border-radius: 20px` for
  large cards and 12px for small ones, no heavy shadows.
- **Nav**: wordmark and mark left, links centred in `--ink-soft` 15px 500, a "Sign in" pill
  right. 88px tall, 72px on phones.
- **Eyebrow**: four voice dots then an uppercase label in `--ink-mute`.

## Voice and copy

- The product noun is **"podcast"** and a day's output is an **"episode"**. "Interactive
  podcast" in full when it is the first mention. Not "briefing", not "digest", not "feed".
- The reader's inputs are **"the content you trust"**, or concretely "newsletters, bookmarks,
  threads". Not "feeds", not "your data".
- Interactivity is phrased as **just ask · go deeper · picks back up**. Not "query", not
  "chat".
- Sentences are short and declarative; headings can carry one italic emphasis. Copy leads
  with the two value props and never with citations, models, or vendors.
- Reference lines: "Many voices. One thing *worth hearing*." (headline); "A motet: many
  voices, one piece." (eyebrow); "Four sources you trust, sung as one podcast." (caption).

## What this applies to

Every surface a person sees: the web SPA and its sign-in and landing, the iOS app including
CarPlay, podcast artwork and show-notes styling in the RSS feed, and any marketing page.
Internal and operator surfaces (the admin screen) follow the same tokens and type but need
no motif.

## What this does not decide

Screen layouts, information architecture, and the app's interaction patterns are unchanged
by this document; it is a restyle, not a redesign. Nothing here adds a dependency beyond two
webfonts, a datastore, a service, or a vendor. See the tripwire in `AGENTS.md`: the SPA is
not the product, so applying this should be a bounded piece of work rather than a rebuild.
