// One card in the integrations catalog: icon, name, what it pulls in, a status pill and
// a single primary action.
//
// The card is a summary and the panel it opens beneath it is the detail, so a card never
// carries a form or a fact list of its own — it says how many accounts are behind it and
// hands off. The one exception to "one action" is deliberate: a card that is *selected*
// changes its label to "Close", because a Manage button that keeps reading "Manage" while
// the panel it opened is already on screen is a button that looks broken when pressed.

import type { Source } from '../../api/client'
import type { Integration } from './catalog'
import { IntegrationIcon, PillDot } from './icons'
import { type CardStatus, cardAction, cardStatus, cardStatusLabel } from './status'

export function IntegrationCard({
  integration,
  rows,
  selected,
  highlighted = false,
  onSelect,
  onOpenPaste,
}: {
  integration: Integration
  /** The `GET /v1/sources` rows of this integration's kind. */
  rows: Source[]
  /** Whether this card's detail panel is the one open. */
  selected: boolean
  /** The empty state's nudge: the first thing a fresh account should do. */
  highlighted?: boolean
  onSelect: () => void
  onOpenPaste: () => void
}) {
  const status = cardStatus(integration, rows)
  const action = cardAction(status)
  const connectedCount = rows.filter((row) => row.connected).length
  const panelId = `integration-panel-${integration.id}`

  const button = (() => {
    if (action === null) {
      return (
        <button type="button" disabled title={integration.detail}>
          Coming soon
        </button>
      )
    }
    if (action === 'open_paste') {
      return (
        <button type="button" onClick={onOpenPaste}>
          Paste in
        </button>
      )
    }
    const label = selected ? 'Close' : action === 'connect' ? 'Connect' : 'Manage'
    return (
      <button
        type="button"
        className={action === 'connect' && !selected ? 'primary' : ''}
        aria-expanded={selected}
        aria-controls={panelId}
        onClick={onSelect}
      >
        {label}
      </button>
    )
  })()

  return (
    <article
      className={[
        'integration-card',
        `status-${status}`,
        selected ? 'selected' : '',
        highlighted ? 'highlighted' : '',
        action === null ? 'unavailable' : '',
      ]
        .filter(Boolean)
        .join(' ')}
      aria-label={`${integration.name}: ${cardStatusLabel(status, connectedCount)}`}
    >
      <div className="integration-head">
        <IntegrationIcon id={integration.id} />
        <div className="integration-title">
          <h3>{integration.name}</h3>
          <StatusPill status={status} count={connectedCount} />
        </div>
      </div>
      <p className="integration-desc">{integration.description}</p>
      <div className="integration-foot">
        <span className="hint">
          {highlighted && status === 'not_connected' ? 'Start here' : accountsLine(status, rows)}
        </span>
        {button}
      </div>
    </article>
  )
}

export function StatusPill({ status, count = 0 }: { status: CardStatus; count?: number }) {
  return (
    <span className={`pill pill-${status}`}>
      <PillDot />
      {cardStatusLabel(status, count)}
    </span>
  )
}

/** The small line at the foot of a card: who is behind it. */
function accountsLine(status: CardStatus, rows: Source[]): string {
  switch (status) {
    case 'coming_soon':
      return 'Not available yet'
    case 'always_on':
      return 'Built in'
    case 'not_connected':
      return 'No account connected'
    case 'awaiting_consent':
      return 'Consent not finished'
    case 'disconnected':
      return 'Credential forgotten'
    default: {
      const connected = rows.filter((row) => row.connected)
      if (connected.length === 1) return connected[0]!.name
      return `${connected.length} accounts`
    }
  }
}
