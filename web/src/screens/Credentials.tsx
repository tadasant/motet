// Credentials: the sites and MCP servers agentic enrichment may use (motet#102).
//
// Two lists and an Add panel. A *site* is a domain the owner has added, and adding it is
// the opt-in: nothing is fetched from anywhere else. Its login is optional. An *MCP server*
// is a tool source the agent is handed once the owner has authorized it over OAuth, and it
// is added only after the owner has read what that risks. Neither list ever shows a
// secret, because the API never sends one.
//
// Authorize is the one flow that leaves the page. It asks the API for a consent URL — the
// API does discovery and registration — remembers the `state`, and hands the browser to the
// server, exactly as connecting a mailbox does. The server sends the browser back to
// /oauth/callback with a `connector.` state, and App.tsx routes that to ConnectorCallback.

import { useCallback, useEffect, useState } from 'react'

import { ApiError, api, type Connector } from '../api/client'
import { beginConsent, redirectUri, rememberState } from '../oauth'
import { AddConnector } from './credentials/AddConnector'
import { ConnectorRow } from './credentials/ConnectorRow'

type Load =
  | { kind: 'loading' }
  | { kind: 'loaded'; connectors: Connector[] }
  | { kind: 'failed'; message: string }

export function Credentials({
  navigate = beginConsent,
}: {
  /** Overridden only by tests: jsdom cannot navigate. */
  navigate?: (url: string) => void
}) {
  const [load, setLoad] = useState<Load>({ kind: 'loading' })
  const [busyId, setBusyId] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  const refresh = useCallback(async () => {
    try {
      setLoad({ kind: 'loaded', connectors: await api.connectors() })
    } catch (err) {
      setLoad({ kind: 'failed', message: err instanceof ApiError ? err.message : String(err) })
    }
  }, [])

  useEffect(() => {
    void refresh()
  }, [refresh])

  const connectors = load.kind === 'loaded' ? load.connectors : []
  const sites = connectors.filter((c) => c.kind === 'site')
  const servers = connectors.filter((c) => c.kind === 'mcp')

  const added = (connector: Connector) => {
    setError(null)
    setLoad({ kind: 'loaded', connectors: [...connectors, connector] })
  }

  const remove = async (connector: Connector) => {
    setBusyId(connector.id)
    setError(null)
    try {
      await api.deleteConnector(connector.id)
      setLoad({ kind: 'loaded', connectors: connectors.filter((c) => c.id !== connector.id) })
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err))
    } finally {
      setBusyId(null)
    }
  }

  const authorize = async (connector: Connector) => {
    setBusyId(connector.id)
    setError(null)
    try {
      const started = await api.authorizeConnector(connector.id, redirectUri())
      // Remembered before the redirect: once `navigate` runs nothing else here executes.
      rememberState(started.state)
      navigate(started.authorization_url)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err))
      // The API wrote the reason onto the row (a 409 for no registration, a 502 for a
      // server that would not answer), so the pill and its note are worth re-reading.
      await refresh()
    } finally {
      setBusyId(null)
    }
  }

  return (
    <section className="credentials" aria-label="Credentials">
      <p className="lead">
        The sites Motet may fetch full articles from, when a newsletter is only a preview of
        one — and the logins and tools the fetching agent may use there. Nothing is fetched
        until enrichment is switched on for this deployment.
      </p>

      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
      {load.kind === 'failed' && (
        <p className="error" role="alert">
          Could not load your credentials: {load.message}. This is not the same as having none.
        </p>
      )}

      <h3>Sites</h3>
      {load.kind === 'loaded' && sites.length === 0 ? (
        <p className="hint empty" role="status">
          No sites yet. Add one whose newsletters link to articles you want in full. A login is
          optional: many links open without one, and a site that emails a code needs only the
          address.
        </p>
      ) : (
        <ul className="connectors" aria-label="Sites">
          {sites.map((c) => (
            <ConnectorRow key={c.id} connector={c} busy={busyId === c.id} onRemove={remove} />
          ))}
        </ul>
      )}

      <h3>MCP servers</h3>
      {load.kind === 'loaded' && servers.length === 0 ? (
        <p className="hint empty" role="status">
          No MCP servers yet. A server is a tool the agent may call while fetching — a read-only
          mailbox, to read a site&rsquo;s sign-in email. Only OAuth-secured servers are
          supported.
        </p>
      ) : (
        <ul className="connectors" aria-label="MCP servers">
          {servers.map((c) => (
            <ConnectorRow
              key={c.id}
              connector={c}
              busy={busyId === c.id}
              onAuthorize={authorize}
              onRemove={remove}
            />
          ))}
        </ul>
      )}

      <AddConnector onAdded={added} />
    </section>
  )
}
