// Access tokens: mint one, see the ones that exist, revoke one.
//
// The whole screen is built around the one fact that cannot be undone — **a token is
// shown once**. So the minted value gets a panel of its own rather than a row in the
// list, it is not dismissed by anything accidental, and the list beside it never shows a
// secret because the API has none to send.
//
// Everything here needs a signed-in session. A token cannot manage tokens and neither can
// the shared API token (the API answers 403), so this panel is only ever rendered for a
// caller who could use it — `Credentials.tsx` asks `/v1/auth/session` and hides it
// otherwise, which is the sidebar-and-403 split the Admin screen already keeps.

import { useCallback, useEffect, useState } from 'react'

import { ApiError, api, type ApiToken } from '../../api/client'

type Load =
  | { kind: 'loading' }
  | { kind: 'loaded'; tokens: ApiToken[] }
  | { kind: 'failed'; message: string }

/** What a lifetime dropdown offers. Null is the default, and is why it is first. */
const LIFETIMES: { label: string; days: number | null }[] = [
  { label: 'Until revoked', days: null },
  { label: '30 days', days: 30 },
  { label: '90 days', days: 90 },
  { label: '1 year', days: 365 },
]

function when(value: string | null): string {
  return value ? new Date(value).toLocaleDateString() : '—'
}

/** Live, expired or revoked — derived, because only `revoked_at` is stored. */
function state(token: ApiToken): 'Live' | 'Revoked' | 'Expired' {
  if (token.revoked_at) return 'Revoked'
  if (token.expires_at && new Date(token.expires_at) <= new Date()) return 'Expired'
  return 'Live'
}

export function AccessTokens() {
  const [load, setLoad] = useState<Load>({ kind: 'loading' })
  const [label, setLabel] = useState('')
  const [days, setDays] = useState<number | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  // The one time a secret exists in this app. Held in component state and nowhere else:
  // never in localStorage, never in the URL, gone on the next navigation.
  const [minted, setMinted] = useState<{ token: string; prefix: string } | null>(null)
  const [copied, setCopied] = useState(false)

  const refresh = useCallback(async () => {
    try {
      setLoad({ kind: 'loaded', tokens: await api.apiTokens() })
    } catch (err) {
      setLoad({ kind: 'failed', message: err instanceof ApiError ? err.message : String(err) })
    }
  }, [])

  useEffect(() => {
    void refresh()
  }, [refresh])

  const mint = async (event: React.FormEvent) => {
    event.preventDefault()
    setBusy(true)
    setError(null)
    setCopied(false)
    try {
      const created = await api.mintApiToken(label.trim(), days)
      setMinted({ token: created.token, prefix: created.created.prefix })
      setLabel('')
      await refresh()
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }

  const revoke = async (token: ApiToken) => {
    setBusy(true)
    setError(null)
    try {
      const revoked = await api.revokeApiToken(token.id)
      // Functional, not `load` off the render closure: the refresh that runs after a mint
      // can land while this request is in flight, and reading the closure would put the
      // list back to whatever it held when this click rendered.
      setLoad((current) =>
        current.kind === 'loaded'
          ? {
              kind: 'loaded',
              tokens: current.tokens.map((t) => (t.id === revoked.id ? revoked : t)),
            }
          : current,
      )
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }

  const tokens = load.kind === 'loaded' ? load.tokens : []

  return (
    <div className="access-tokens">
      <h3>Access tokens</h3>
      <p className="hint">
        A token signs in for a script or an agent, without a browser. It can do everything
        you can except manage tokens and open Admin. It is shown once, stored only as a
        hash, and works until you revoke it.
      </p>

      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
      {load.kind === 'failed' && (
        <p className="error" role="alert">
          Could not load your tokens: {load.message}. This is not the same as having none.
        </p>
      )}

      {minted && (
        <div className="minted-token" role="alert">
          <p>
            <strong>Copy this now.</strong> It will not be shown again — if you lose it,
            revoke it and mint another.
          </p>
          <code className="token-value">{minted.token}</code>
          <div className="row">
            <button
              type="button"
              onClick={() => {
                void navigator.clipboard?.writeText(minted.token).then(() => setCopied(true))
              }}
            >
              {copied ? 'Copied' : 'Copy'}
            </button>
            <button type="button" className="secondary" onClick={() => setMinted(null)}>
              Done
            </button>
          </div>
        </div>
      )}

      <form className="mint-token" onSubmit={mint}>
        <label>
          What is it for?
          <input
            value={label}
            onChange={(event) => setLabel(event.target.value)}
            placeholder="staging agent"
            maxLength={80}
            required
          />
        </label>
        <label>
          Expires
          <select
            value={String(days)}
            onChange={(event) =>
              setDays(event.target.value === 'null' ? null : Number(event.target.value))
            }
          >
            {LIFETIMES.map((lifetime) => (
              <option key={lifetime.label} value={String(lifetime.days)}>
                {lifetime.label}
              </option>
            ))}
          </select>
        </label>
        <button type="submit" disabled={busy || !label.trim()}>
          {busy ? 'Working…' : 'Create token'}
        </button>
      </form>

      {load.kind === 'loaded' && tokens.length === 0 ? (
        <p className="hint empty" role="status">
          No tokens yet.
        </p>
      ) : (
        <ul className="connectors" aria-label="Access tokens">
          {tokens.map((token) => (
            <li key={token.id} className="connector">
              <div className="connector-main">
                <div className="connector-title">
                  <strong>{token.label}</strong>
                  <code className="token-prefix">{token.prefix}…</code>
                  <span className={`pill pill-${state(token).toLowerCase()}`}>
                    <span className="pill-dot" />
                    {state(token)}
                  </span>
                </div>
                <p className="hint">
                  Created {when(token.created_at)} · Last used {when(token.last_used_at)} ·{' '}
                  {token.revoked_at
                    ? `Revoked ${when(token.revoked_at)}`
                    : token.expires_at
                      ? `Expires ${when(token.expires_at)}`
                      : 'No expiry'}
                </p>
              </div>
              <div className="connector-actions">
                {!token.revoked_at && (
                  <button
                    type="button"
                    className="secondary"
                    disabled={busy}
                    onClick={() => void revoke(token)}
                  >
                    Revoke
                  </button>
                )}
              </div>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}
