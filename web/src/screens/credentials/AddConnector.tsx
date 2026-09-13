// The "Add" panel: a kind switch and the fields for whichever kind is chosen.
//
// Two things are deliberate. The password field says out loud that it may be left empty
// — The Information logs in by emailed code, and a form that demanded a password would
// make the one site this was built for impossible to store. And the domain field
// normalizes on blur, so what the user sees before pressing Add is what the API will
// store: an article URL pasted in becomes the domain it belongs to.

import { useState } from 'react'

import { ApiError, api, type Connector } from '../../api/client'
import { normalizeDomain } from './domain'

type Kind = 'site' | 'mcp'
type Status = { kind: 'idle' } | { kind: 'busy' } | { kind: 'error'; message: string }

export function AddConnector({ onAdded }: { onAdded: (connector: Connector) => void }) {
  const [kind, setKind] = useState<Kind>('site')
  const [label, setLabel] = useState('')
  const [domain, setDomain] = useState('')
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [url, setUrl] = useState('')
  const [domains, setDomains] = useState('')
  const [status, setStatus] = useState<Status>({ kind: 'idle' })

  const reset = () => {
    setLabel('')
    setDomain('')
    setUsername('')
    setPassword('')
    setUrl('')
    setDomains('')
  }

  const submit = async (event: React.FormEvent) => {
    event.preventDefault()
    setStatus({ kind: 'busy' })
    try {
      const created =
        kind === 'site'
          ? await api.createConnector({
              kind,
              label: label.trim() || normalizeDomain(domain),
              domain: normalizeDomain(domain),
              username: username.trim(),
              password,
            })
          : await api.createConnector({
              kind,
              label: label.trim() || url.trim(),
              url: url.trim(),
              domains: domains
                .split(/[,\s]+/)
                .map(normalizeDomain)
                .filter(Boolean),
            })
      reset()
      setStatus({ kind: 'idle' })
      onAdded(created)
    } catch (err) {
      setStatus({ kind: 'error', message: err instanceof ApiError ? err.message : String(err) })
    }
  }

  const canSubmit =
    status.kind !== 'busy' &&
    (kind === 'site' ? normalizeDomain(domain).includes('.') && username.trim() !== '' : url.trim() !== '')

  return (
    <section className="integration-panel add-connector" aria-labelledby="add-connector-heading">
      <div className="integration-panel-head">
        <div>
          <h3 id="add-connector-heading">Add a connector</h3>
          <p className="hint">
            Stored sealed. Only the ingestion worker can open it; this screen never sees it again.
          </p>
        </div>
      </div>

      <div className="kind-switch" role="radiogroup" aria-label="Connector type">
        <label className={kind === 'site' ? 'selected' : undefined}>
          <input type="radio" name="kind" value="site" checked={kind === 'site'} onChange={() => setKind('site')} />
          Site login
        </label>
        <label className={kind === 'mcp' ? 'selected' : undefined}>
          <input type="radio" name="kind" value="mcp" checked={kind === 'mcp'} onChange={() => setKind('mcp')} />
          MCP server
        </label>
      </div>

      <form onSubmit={submit} aria-label={kind === 'site' ? 'Add a site login' : 'Add an MCP server'}>
        {kind === 'site' ? (
          <>
            <label htmlFor="cn-domain">Domain</label>
            <input
              id="cn-domain"
              value={domain}
              placeholder="theinformation.com"
              autoComplete="off"
              onChange={(e) => setDomain(e.target.value)}
              onBlur={() => setDomain(normalizeDomain(domain))}
            />
            <p className="hint">
              Articles on this domain get a browser session that logs in with these details.
            </p>
            <label htmlFor="cn-username">Username or email</label>
            <input
              id="cn-username"
              value={username}
              autoComplete="off"
              onChange={(e) => setUsername(e.target.value)}
            />
            <label htmlFor="cn-password">Password</label>
            <input
              id="cn-password"
              type="password"
              value={password}
              autoComplete="new-password"
              onChange={(e) => setPassword(e.target.value)}
            />
            <p className="hint">Leave blank for email-code or magic-link logins.</p>
          </>
        ) : (
          <>
            <label htmlFor="cn-url">Server URL</label>
            <input
              id="cn-url"
              value={url}
              placeholder="https://example.com/mcp"
              autoComplete="off"
              onChange={(e) => setUrl(e.target.value)}
            />
            <p className="hint">
              OAuth only. After adding, press Authorize to sign in with the server. A server
              that does not support dynamic client registration will say so.
            </p>
            <label htmlFor="cn-domains">Applies to domains (optional)</label>
            <input
              id="cn-domains"
              value={domains}
              placeholder="example.com, another.example"
              autoComplete="off"
              onChange={(e) => setDomains(e.target.value)}
            />
            <p className="hint">
              Leave blank to offer this server to the agent for any article.
            </p>
          </>
        )}
        <label htmlFor="cn-label">Label (optional)</label>
        <input
          id="cn-label"
          value={label}
          placeholder={kind === 'site' ? 'The Information' : 'Email (gmail-ro)'}
          autoComplete="off"
          onChange={(e) => setLabel(e.target.value)}
        />
        <button type="submit" className="primary" disabled={!canSubmit}>
          {status.kind === 'busy' ? 'Adding…' : 'Add'}
        </button>
        {status.kind === 'error' && (
          <p className="error" role="alert">
            {status.message}
          </p>
        )}
      </form>
    </section>
  )
}
