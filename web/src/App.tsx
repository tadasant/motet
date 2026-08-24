// A tab strip, and no router.
//
// The SPA is the eyes-on backlog surface, not the product — "SPA work still running after
// a week" is a named tripwire in AGENTS.md. A handful of screens do not need a routing
// library, a state manager, or a design system, and adding one would be the first step
// toward building a product instead of a factory.
//
// OAuth is the one thing that forces a path on us, because Google redirects to a URL
// rather than back into a running app. It is handled by reading `location` once at boot
// (see oauth.ts) and rendering the callback instead of the tabs — a few lines, against a
// dependency that would then be available for every future "shouldn't this be a route?".

import { useCallback, useEffect, useState } from 'react'

import { ApiError, type Episode, type NewsItem, api, getToken, setToken } from './api/client'
import { forgetCallbackUrl, readCallback } from './oauth'
import { Backlog } from './screens/Backlog'
import { EpisodeScreen } from './screens/EpisodeScreen'
import { OAuthCallback } from './screens/OAuthCallback'
import { PasteIn } from './screens/PasteIn'
import { Sources } from './screens/Sources'

type Tab = 'paste' | 'backlog' | 'episode' | 'sources'

const TABS: { id: Tab; label: string }[] = [
  { id: 'paste', label: 'Paste in' },
  { id: 'backlog', label: 'Backlog' },
  { id: 'episode', label: 'Episode' },
  { id: 'sources', label: 'Sources' },
]

export default function App() {
  const [tab, setTab] = useState<Tab>('paste')
  const [items, setItems] = useState<NewsItem[]>([])
  const [episode, setEpisode] = useState<Episode | null>(null)
  const [token, setTokenState] = useState(getToken())
  const [error, setError] = useState('')
  // Read once, in an initializer, so every later render works from state rather than
  // from an address bar the callback is about to rewrite.
  const [callback, setCallback] = useState(readCallback)

  const refresh = useCallback(() => {
    api
      .newsItems()
      .then((next) => {
        setItems(next)
        setError('')
      })
      .catch((err) => setError(err instanceof ApiError ? err.message : String(err)))
  }, [])

  // Not while the callback is on screen: it has its own request to make, and a backlog
  // fetch that 401s behind it would put an unrelated error above the answer the user is
  // waiting for.
  useEffect(() => {
    if (!callback) refresh()
  }, [callback, refresh, token])

  // Take the code out of the address bar as soon as it has been read into state. A reload
  // would otherwise re-POST a code the API has already consumed and report a flow that
  // worked as one that failed.
  useEffect(() => {
    if (callback) forgetCallbackUrl()
  }, [callback])

  const finishCallback = () => {
    setCallback(null)
    setTab('sources')
  }

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
        {/* Hidden during the callback: there is one thing to do there, and the screen
            offers it. */}
        {!callback && (
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
        )}
      </header>

      <details className="token">
        <summary>API token</summary>
        <p className="hint">
          One shared token for the single Phase 1 account — no signup, no login. Stored in
          this browser only. (Connecting a mailbox under Sources is a different thing: that
          is Google&rsquo;s consent, and its token never comes back here.)
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

      {callback ? (
        <OAuthCallback callback={callback} onDone={finishCallback} />
      ) : (
        <>
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
          {tab === 'sources' && <Sources />}
        </>
      )}
    </main>
  )
}
