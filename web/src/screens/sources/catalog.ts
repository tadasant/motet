// The catalog of integrations: what Motet can pull a reading backlog from, whether or not
// this deployment can do it yet.
//
// Static on purpose. The API's `GET /v1/sources` lists *accounts* — rows — and a card
// for something not yet connected has no row to be rendered from, so the list of things
// one *could* connect has to live somewhere, and this is it. Each entry names the
// `Source.kind` it corresponds to so the screen can join rows onto cards.
//
// Two of the four are honestly labelled "Coming soon" rather than offered: the API
// answers 400 for any provider but `gmail`, X bookmarks wait on an API-tier spend
// decision (AGENTS.md, Phase 2 "out"), and RSS has no adapter at all. A card that says so
// is better than a missing card — the person connecting a mailbox is also the person
// wondering what else this reads — but a *button* for either would be a promise the
// backend refuses to keep, so their action is disabled and says why.

export type IntegrationId = 'gmail' | 'paste' | 'x' | 'rss'

export type Availability =
  /** Connectable through `/v1/sources/connect`. */
  | 'available'
  /** Always present and never connected: the `src_paste` row migration 0002 seeds. */
  | 'builtin'
  /** Not built. Shown so the catalog is honest about its edges. */
  | 'coming_soon'

export type Integration = {
  id: IntegrationId
  name: string
  /** One line: what it pulls in. */
  description: string
  /** The `Source.kind` rows of this integration carry, or null when nothing can be a row. */
  kind: string | null
  availability: Availability
  /** What the deployment does with what it pulls in — the sentence under the fold. */
  detail: string
}

export const CATALOG: Integration[] = [
  {
    id: 'gmail',
    name: 'Gmail',
    description: 'Newsletters from a mailbox, matched by a Gmail search you choose.',
    kind: 'gmail',
    availability: 'available',
    detail:
      'Motet polls the mailbox and extracts the article from each matching message.',
  },
  {
    id: 'paste',
    name: 'Paste',
    description: 'Text you paste in yourself. Always on; nothing to connect.',
    kind: 'paste',
    availability: 'builtin',
    detail: 'Pasting is asking, so a paste is processed at once rather than held.',
  },
  {
    id: 'x',
    name: 'X bookmarks',
    description: 'Posts you bookmark on X, pulled in as source items.',
    kind: null,
    availability: 'coming_soon',
    detail: 'Waits on a decision about the X API tier. Not built.',
  },
  {
    id: 'rss',
    name: 'RSS',
    description: 'Feeds you subscribe to, polled like a mailbox.',
    kind: null,
    availability: 'coming_soon',
    detail: 'No adapter yet.',
  },
]

export function integrationById(id: IntegrationId): Integration {
  const found = CATALOG.find((entry) => entry.id === id)
  if (!found) throw new Error(`unknown integration ${id}`)
  return found
}
