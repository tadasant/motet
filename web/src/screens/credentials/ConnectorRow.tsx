// One connector: what it is for, whether it can be used, and the one or two things you
// can do to it. Never shows a secret — the API never sends one.

import type { Connector } from '../../api/client'

export type ConnectorPill = 'ready' | 'needs_auth' | 'error'

/** The pill's text. `has_secret` is folded in so a site with no password still reads as usable. */
export function pillFor(connector: Connector): { status: ConnectorPill; text: string } {
  if (connector.status === 'error') return { status: 'error', text: 'Error' }
  if (connector.status === 'needs_auth') {
    return {
      status: 'needs_auth',
      text: connector.oauth_registered ? 'Not authorized' : 'Needs authorization',
    }
  }
  if (connector.kind === 'site' && !connector.has_secret) {
    return { status: 'ready', text: 'Ready · no password' }
  }
  return { status: 'ready', text: 'Ready' }
}

export function ConnectorRow({
  connector,
  busy,
  onAuthorize,
  onRemove,
}: {
  connector: Connector
  busy: boolean
  onAuthorize?: (connector: Connector) => void
  onRemove: (connector: Connector) => void
}) {
  const pill = pillFor(connector)
  const where = connector.kind === 'site' ? connector.domain : connector.url
  return (
    <li className="connector" data-kind={connector.kind}>
      <div className="connector-main">
        <div className="connector-title">
          <strong>{connector.label}</strong>
          <span className={`pill pill-${pill.status}`}>
            <span className="pill-dot" aria-hidden="true" />
            {pill.text}
          </span>
        </div>
        <p className="hint connector-where">
          <code>{where}</code>
          {connector.kind === 'site' && connector.username && (
            <>
              {' · '}
              <span>{connector.username}</span>
            </>
          )}
          {connector.kind === 'mcp' && connector.domains.length > 0 && (
            <>
              {' · for '}
              <span>{connector.domains.join(', ')}</span>
            </>
          )}
        </p>
        {connector.last_error && (
          <p className="reason" role="note">
            {connector.last_error}
          </p>
        )}
      </div>
      <div className="connector-actions">
        {connector.kind === 'mcp' && onAuthorize && (
          <button
            type="button"
            className={connector.status === 'needs_auth' ? 'primary' : undefined}
            disabled={busy}
            onClick={() => onAuthorize(connector)}
          >
            {connector.status === 'ready' ? 'Re-authorize' : 'Authorize'}
          </button>
        )}
        <button type="button" className="danger" disabled={busy} onClick={() => onRemove(connector)}>
          Remove
        </button>
      </div>
    </li>
  )
}
