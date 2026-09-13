// PROTOTYPE — Credentials: the connectors the ingestion agent logs in with.
//
// Two lists and an add panel. A *site* login is what turns an article behind a
// newsletter preview into something the agent can read — it is the trigger for a browser
// session on that domain. An *MCP server* is a tool source the agent is handed, once the
// user has authorized it over OAuth. Neither list ever shows a secret, because the API
// never sends one: `has_secret` is the whole of what this screen knows.
//
// Authorize is the one flow that leaves the page. It asks the API for a consent URL — the
// API does the OAuth discovery and registration — remembers the `state`, and hands the
// browser to the server, exactly the way connecting a mailbox does. The server sends the
// browser back to /oauth/callback with a `connector.` state, and App.tsx routes that here.

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
    <section className="credentials" aria-labelledby="credentials-heading">
      <h2 id="credentials-heading">Credentials</h2>
      <p className="lead">
        Used by the ingestion agent when an article behind a newsletter preview needs a login.
        A site login starts a browser session on that domain; an MCP server is handed to the
        agent as a tool.
      </p>

      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
      {load.kind === 'failed' && (
        <p className="error" role="alert">
          Could not load your connectors: {load.message}. This is not the same as having none.
        </p>
      )}

      <h3>Sites</h3>
      {load.kind === 'loaded' && sites.length === 0 ? (
        <p className="hint empty" role="status">
          No site logins yet. Add one for a paywalled site whose newsletters link to full
          articles — the agent will sign in there to read them. A password is optional:
          sites that email a code need only the address.
        </p>
      ) : (
        <ul className="connectors" aria-label="Site logins">
          {sites.map((c) => (
            <ConnectorRow key={c.id} connector={c} busy={busyId === c.id} onRemove={remove} />
          ))}
        </ul>
      )}

      <h3>MCP servers</h3>
      {load.kind === 'loaded' && servers.length === 0 ? (
        <p className="hint empty" role="status">
          No MCP servers yet. Add a remote server the agent may call while enriching an
          article — a mailbox, a notes tool, a search — and authorize it with OAuth. Only
          OAuth-secured servers are supported.
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
