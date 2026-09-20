// How far back a mailbox's first sync reaches — the one number a person has to be shown.
//
// motet#139: the first production connect searched a week, found 55 of the ~200 messages
// in the label, paged through them correctly and said "caught up". Nothing on any screen
// said "a week", so the cap read as a bug. These are the choices the connect form offers
// and the ones "Sync further back" re-offers, and the labels are what appear beside a
// source afterwards.

/** `motet_sources.gmail.DEFAULT_FIRST_SYNC_DAYS` — what a connect form starts on. */
export const DEFAULT_FIRST_SYNC_DAYS = 30

/**
 * `motet_sources.gmail.MAX_FIRST_SYNC_DAYS` — ten years, offered as "Everything".
 *
 * Past the age of any mailbox this product is for, so it is "everything" without being
 * unbounded. The API refuses anything larger; the worker clamps whatever it reads.
 */
export const MAX_FIRST_SYNC_DAYS = 3650

export const FIRST_SYNC_CHOICES: ReadonlyArray<{ days: number; label: string }> = [
  { days: 7, label: 'Last 7 days' },
  { days: DEFAULT_FIRST_SYNC_DAYS, label: 'Last 30 days' },
  { days: 90, label: 'Last 90 days' },
  { days: 365, label: 'Last year' },
  { days: MAX_FIRST_SYNC_DAYS, label: 'Everything' },
]

/**
 * A window as a phrase, for a source that already has one.
 *
 * `null` is a source connected before the window was a choice — and the API deliberately
 * does not guess what the deployment's fallback is, because that variable is the worker's
 * and this service cannot read it. So the phrase says exactly that rather than a number
 * nobody can stand behind.
 */
export function windowLabel(days: number | null | undefined): string {
  if (days === null || days === undefined) return 'the deployment default'
  const known = FIRST_SYNC_CHOICES.find((choice) => choice.days === days)
  if (known) return known.label.toLowerCase().replace(/^last /, 'the last ')
  return `the last ${days} days`
}
