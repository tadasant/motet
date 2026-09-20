// The Add panel: a kind switch, and the fields for whichever kind is chosen.
//
// Three things are deliberate. A site needs only its domain — adding one is the owner's
// opt-in to fetching articles from it, and the login fields say out loud that they are for
// the sites that need one. The domain field normalizes on blur, so what the owner sees
// before pressing Add is what the API will store. And an MCP server cannot be added until
// the risk box is ticked (motet#102, option E1: kept "with the risk made clear when
// connecting") — the API refuses the request without that acknowledgement too, so this box
// is the explanation rather than the control.

import { useState } from 'react'

import { ApiError, api, type Connector } from '../../api/client'
import { looksLikeDomain, normalizeDomain } from './domain'

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
  const [acknowledged, setAcknowledged] = useState(false)
  const [status, setStatus] = useState<Status>({ kind: 'idle' })

  const reset = () => {
    setLabel('')
    setDomain('')
    setUsername('')
    setPassword('')
    setUrl('')
    setDomains('')
    setAcknowledged(false)
  }

  const submit = async (event: React.FormEvent) => {
    event.preventDefault()
    setStatus({ kind: 'busy' })
    try {
      const created =
        kind === 'site'
          ? await api.createConnector({
              kind,
              label: label.trim(),
              domain: normalizeDomain(domain),
              username: username.trim() || null,
              password: password || null,
              // A site carries no risk to acknowledge; the generated type still wants the key.
              acknowledge_risk: false,
            })
          : await api.createConnector({
              kind,
              label: label.trim(),
              url: url.trim(),
              domains: domains.split(/[,\s]+/).map(normalizeDomain).filter(Boolean),
              acknowledge_risk: acknowledged,
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
    (kind === 'site'
      ? looksLikeDomain(normalizeDomain(domain)) && (password === '' || username.trim() !== '')
      : url.trim().startsWith('https://') && acknowledged)

  return (
    <section className="integration-panel add-connector" aria-labelledby="add-connector-heading">
      <div className="integration-panel-head">
        <div>
          <h3 id="add-connector-heading">Add</h3>
          <p className="hint">
            A password or a token is sealed: only the ingestion worker can open it.
          </p>
        </div>
      </div>

      <div className="kind-switch" role="radiogroup" aria-label="What to add">
        <label className={kind === 'site' ? 'selected' : undefined}>
          <input type="radio" name="kind" value="site" checked={kind === 'site'} onChange={() => setKind('site')} />
          Site
        </label>
        <label className={kind === 'mcp' ? 'selected' : undefined}>
          <input type="radio" name="kind" value="mcp" checked={kind === 'mcp'} onChange={() => setKind('mcp')} />
          MCP server
        </label>
      </div>

      <form onSubmit={submit} aria-label={kind === 'site' ? 'Add a site' : 'Add an MCP server'}>
        {kind === 'site' ? (
          <>
            <label htmlFor="cn-domain">Domain</label>
            <input
              id="cn-domain"
              value={domain}
              placeholder="example.com"
              autoComplete="off"
              onChange={(e) => setDomain(e.target.value)}
              onBlur={() => setDomain(normalizeDomain(domain))}
            />
            <p className="hint">
              Newsletter links to this site and its subdomains get the full article fetched.
              Nothing else is.
            </p>
            <label htmlFor="cn-username">Username or email (optional)</label>
            <input
              id="cn-username"
              value={username}
              autoComplete="off"
              onChange={(e) => setUsername(e.target.value)}
            />
            <label htmlFor="cn-password">Password (optional)</label>
            <input
              id="cn-password"
              type="password"
              value={password}
              autoComplete="new-password"
              onChange={(e) => setPassword(e.target.value)}
            />
            <p className="hint">
              Leave both blank for a site the newsletter&rsquo;s own link opens. Leave the
              password blank for a site that emails a code or a magic link.
            </p>
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
              OAuth only. Press Authorize after adding; a server without dynamic client
              registration will say so.
            </p>
            <label htmlFor="cn-domains">Only for these sites (optional)</label>
            <input
              id="cn-domains"
              value={domains}
              placeholder="example.com, another.example"
              autoComplete="off"
              onChange={(e) => setDomains(e.target.value)}
            />
            <p className="hint">Leave blank to hand this server to the agent for every site you added.</p>

            <div className="risk" role="note" aria-labelledby="mcp-risk-heading">
              <strong id="mcp-risk-heading">Before you connect a server</strong>
              <p>
                The enrichment agent that is handed this server also reads web pages nobody at
                Motet wrote. A hostile page can instruct it to use this server with your
                account — to read what the server can see and carry it somewhere else.
              </p>
              <p>
                Enrichment narrows that — its browser is locked to the article&rsquo;s site,
                it holds no database or keys, and its transcript keeps no result from a
                server like this one — but it does not close it. Connect only a server whose
                worst case you would accept: a read-only mailbox, not one that can send.
              </p>
              <label className="check">
                <input
                  type="checkbox"
                  checked={acknowledged}
                  onChange={(e) => setAcknowledged(e.target.checked)}
                />
                I understand that a web page the agent reads can steer it into using this server
                with my account.
              </label>
            </div>
          </>
        )}
        <label htmlFor="cn-label">Label (optional)</label>
        <input
          id="cn-label"
          value={label}
          placeholder={kind === 'site' ? 'Example News' : 'Mail (read-only)'}
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
