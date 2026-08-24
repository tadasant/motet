// Three screens, one tab strip, no router.
//
// The SPA is the eyes-on backlog surface, not the product — "SPA work still running after
// a week" is a named tripwire in AGENTS.md. Three screens do not need a routing library, a
// state manager, or a design system, and adding one would be the first step toward
// building a product instead of a factory.

import { useCallback, useEffect, useState } from 'react'

import { ApiError, type Episode, type NewsItem, api, getToken, setToken } from './api/client'
import { Backlog } from './screens/Backlog'
import { EpisodeScreen } from './screens/EpisodeScreen'
import { PasteIn } from './screens/PasteIn'

type Tab = 'paste' | 'backlog' | 'episode'

const TABS: { id: Tab; label: string }[] = [
  { id: 'paste', label: 'Paste in' },
  { id: 'backlog', label: 'Backlog' },
  { id: 'episode', label: 'Episode' },
]

export default function App() {
  const [tab, setTab] = useState<Tab>('paste')
  const [items, setItems] = useState<NewsItem[]>([])
  const [episode, setEpisode] = useState<Episode | null>(null)
  const [token, setTokenState] = useState(getToken())
  const [error, setError] = useState('')

  const refresh = useCallback(() => {
    api
      .newsItems()
      .then((next) => {
        setItems(next)
        setError('')
      })
      .catch((err) => setError(err instanceof ApiError ? err.message : String(err)))
  }, [])

  useEffect(refresh, [refresh, token])

  const openEpisode = (next: Episode) => {
    setEpisode(next)
    setTab('episode')
  }

  const saveToken = (value: string) => {
    setToken(value)
    setTokenState(value)
  }

  return (
    <main>
      <header>
        <h1>Motet</h1>
        <nav aria-label="Screens">
          {TABS.map((entry) => (
            <button
              key={entry.id}
              type="button"
              aria-current={tab === entry.id ? 'page' : undefined}
              onClick={() => setTab(entry.id)}
            >
              {entry.label}
            </button>
          ))}
        </nav>
      </header>

      <details className="token">
        <summary>API token</summary>
        <p className="hint">
          One shared token for the single Phase 1 account — no signup, no OAuth. Stored in
          this browser only.
        </p>
        <input
          aria-label="API token"
          type="password"
          value={token}
          onChange={(e) => saveToken(e.target.value)}
          placeholder="MOTET_API_TOKEN"
        />
      </details>

      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}

      {tab === 'paste' && <PasteIn onIngested={refresh} />}
      {tab === 'backlog' && (
        <Backlog items={items} onChanged={refresh} onOpenEpisode={openEpisode} />
      )}
      {tab === 'episode' &&
        (episode ? (
          <EpisodeScreen
            episode={episode}
            onEpisodeChanged={setEpisode}
            onBacklogChanged={refresh}
          />
        ) : (
          <section aria-labelledby="episode-heading">
            <h2 id="episode-heading">Episode</h2>
            <p className="hint">Make one from the backlog.</p>
          </section>
        ))}
    </main>
  )
}
