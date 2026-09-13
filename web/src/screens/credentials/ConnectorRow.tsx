// One connector: what it is for, whether it can be used, and what you can do to it. Never
// shows a secret — the API never sends one; `has_secret` is the whole of what this knows.

import type { Connector } from '../../api/client'

export type ConnectorPill = 'ready' | 'needs_auth' | 'error'

/** The pill: whether the row can be used, folded together with what a site's login is. */
export function pillFor(connector: Connector): { status: ConnectorPill; text: string } {
  if (connector.status === 'error') return { status: 'error', text: 'Error' }
  if (connector.status === 'needs_auth') {
    return {
      status: 'needs_auth',
      text: connector.oauth_registered ? 'Not authorized' : 'Needs authorization',
    }
  }
  if (connector.kind === 'site') {
    if (!connector.username) return { status: 'ready', text: 'Ready · no login' }
    if (!connector.has_secret) return { status: 'ready', text: 'Ready · passwordless login' }
    return { status: 'ready', text: 'Ready · login saved' }
  }
  return { status: 'ready', text: 'Authorized' }
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
          {connector.kind === 'site' && connector.username && <> · {connector.username}</>}
          {connector.kind === 'mcp' && (
            <>
              {' · '}
              {connector.domains.length > 0
                ? `for ${connector.domains.join(', ')}`
                : 'for every site you added'}
            </>
          )}
        </p>
        {connector.kind === 'mcp' && connector.status !== 'ready' && (
          <p className="hint">
            Authorizing lets the enrichment agent use this server with your account, on the
            terms you accepted when you added it.
          </p>
        )}
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
            className={connector.status === 'ready' ? undefined : 'primary'}
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
