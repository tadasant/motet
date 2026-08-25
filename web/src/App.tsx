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
// Two flows come back on that one path — signing in, and connecting a mailbox — and the
// `state` says which, because it is the only thing that survives the round trip.
//
// **A browser holding no token sees the door and nothing else.** That is the whole point
// of Google Sign-In here: what used to be "open the disclosure and paste MOTET_API_TOKEN"
// is now a button. The disclosure stays, because the shared token still works and is
// still the answer when there is no Google account to hand — it has just stopped being
// the thing a human is expected to type into a phone.

import { useCallback, useEffect, useState } from 'react'

import {
  ApiError,
  type Episode,
  type IngestionItem,
  type NewsItem,
  type SessionInfo,
  api,
  getToken,
  setToken,
} from './api/client'
import { forgetCallbackUrl, isLoginState, readCallback } from './oauth'
import { Backlog } from './screens/Backlog'
import { EpisodeScreen } from './screens/EpisodeScreen'
import { OAuthCallback } from './screens/OAuthCallback'
import { PasteIn } from './screens/PasteIn'
import { SignIn } from './screens/SignIn'
import { SignInCallback } from './screens/SignInCallback'
import { Sources } from './screens/Sources'

type Tab = 'paste' | 'backlog' | 'episode' | 'sources'

// How often the backlog re-asks while an item is still being processed. Short enough that
// a paste which integrates in seconds is seen to integrate, and it only runs while
// something is pending.
const POLL_MS = 3_000

const TABS: { id: Tab; label: string }[] = [
  { id: 'paste', label: 'Paste in' },
  { id: 'backlog', label: 'Backlog' },
  { id: 'episode', label: 'Episode' },
  { id: 'sources', label: 'Sources' },
]

export default function App() {
  const [tab, setTab] = useState<Tab>('paste')
  const [items, setItems] = useState<NewsItem[]>([])
  // What has been pasted and is not a news item yet. Held here rather than in the backlog
  // screen because the tab strip labels it too: the person who needs to see it is on the
  // *paste* screen, having just pasted, and would otherwise have no reason to go looking.
  const [ingestion, setIngestion] = useState<IngestionItem[]>([])
  const [episode, setEpisode] = useState<Episode | null>(null)
  const [token, setTokenState] = useState(getToken())
  const [error, setError] = useState('')
  // Read once, in an initializer, so every later render works from state rather than
  // from an address bar the callback is about to rewrite.
  const [callback, setCallback] = useState(readCallback)
  // Who the *server* says this browser is, or null when it says nobody. Best-effort: an
  // older API with no /v1/auth answers 404 and this stays null.
  const [who, setWho] = useState<SessionInfo | null>(null)
  // A deployment with MOTET_API_TOKEN unset has no lock on it at all — the documented
  // local setup. Showing a sign-in door in front of an API that is already answering
  // would be a dead end, and clicking the button there 503s because a laptop has no
  // allowlist either.
  const unlocked = who?.how === 'open'

  // A sign-in and a mailbox connection come back on the same path. Only `state` can tell
  // them apart, because it is the one value Google echoes back verbatim.
  const signingIn = callback !== null && callback.kind !== 'empty' && isLoginState(callback.state)

  const saveToken = useCallback((value: string) => {
    setToken(value)
    setTokenState(value)
  }, [])

  const refresh = useCallback(() => {
    // Both together: the backlog and the queue in front of it are two halves of one
    // answer, and fetching them from two places is how they end up disagreeing about an
    // item that integrated between the two requests.
    //
    // The ingestion half is best-effort, and the asymmetry is deliberate. It is the
    // *secondary* list, and the API it comes from is a separate service that rolls on its
    // own schedule — so an SPA that has this route while the API it is talking to does not
    // would, without the catch, answer "where is my backlog" with a 404 about something
    // else entirely. Failing softly leaves whatever was last known on screen; failing
    // loudly would take the backlog down with it.
    Promise.all([api.newsItems(), api.ingestion().catch(() => null)])
      .then(([nextItems, nextIngestion]) => {
        setItems(nextItems)
        if (nextIngestion) setIngestion(nextIngestion)
        setError('')
      })
      .catch((err) => setError(err instanceof ApiError ? err.message : String(err)))
  }, [])

  // Not while the callback is on screen, and not before there is a token: each has its
  // own thing to say, and a backlog fetch that 401s behind it would put an unrelated
  // error above the answer the user is actually waiting for — "GET /v1/news-items failed:
  // 401" over the top of a sign-in button being the silliest version of that.
  useEffect(() => {
    if (!callback && (token || unlocked)) refresh()
  }, [callback, refresh, token, unlocked])

  // Poll while — and only while — something is actually in flight. Ingestion takes
  // seconds, so an item that resolves has to resolve *on screen*: a status that is only
  // correct until you look away is the same disappearance in slow motion. It stops on its
  // own the moment nothing is pending, so an idle tab makes no requests.
  const waiting = ingestion.some((item) => item.state === 'pending')
  // A stuck item gets a louder count than a busy one. "3 in flight" and "3, one of which
  // is never coming back" want different reactions.
  const anyFailed = ingestion.some((item) => item.state === 'failed')
  useEffect(() => {
    if (!waiting || callback || !(token || unlocked)) return
    const timer = window.setInterval(refresh, POLL_MS)
    return () => window.clearInterval(timer)
  }, [waiting, callback, refresh, token, unlocked])

  // Take the code out of the address bar as soon as it has been read into state. A reload
  // would otherwise re-POST a code the API has already consumed and report a flow that
  // worked as one that failed.
  useEffect(() => {
    if (callback) forgetCallbackUrl()
  }, [callback])

  // Asked unconditionally, including with no token at all: that is how an *unlocked*
  // deployment is recognised, and it is the only way to recognise one — a browser cannot
  // tell "no credential" apart from "no credential needed" without asking.
  //
  // A 401 clears the token as well as `who`. A session expires after 30 days and can be
  // revoked from another device, and without this the SPA would keep a dead string in
  // storage, show a tab strip whose every screen 401s, and offer no way back to the door
  // except realising that emptying the *API token* field is what signs you out.
  useEffect(() => {
    api
      .session()
      .then(setWho)
      .catch((err) => {
        setWho(null)
        if (err instanceof ApiError && err.status === 401 && token) saveToken('')
      })
  }, [token, saveToken])

  const finishCallback = () => {
    setCallback(null)
    // Back to where the flow started from: a mailbox connection belongs on Sources, and a
    // sign-in belongs at the front of the app the person was trying to reach.
    setTab(signingIn ? 'paste' : 'sources')
  }

  const openEpisode = (next: Episode) => {
    setEpisode(next)
    setTab('episode')
  }

  const signOut = () => {
    // Fire and forget the revoke, then drop the token locally whatever the server said —
    // a browser that has decided to sign out must not stay signed in because a request
    // failed. The row expires on its own if the call never lands.
    api.logout().catch(() => undefined)
    saveToken('')
    setWho(null)
  }

  return (
    <main>
      <header>
        <h1>Motet</h1>
        {/* The address alone, not "signed in as …": the button beside it already says
            what state this is, and the callback screen is the place that spells it out. */}
        {who?.email && (
          <p className="hint">
            {who.email}{' '}
            <button type="button" onClick={signOut}>
              Sign out
            </button>
          </p>
        )}
        {/* Hidden during the callback, and before there is anything to navigate: there is
            one thing to do on either screen, and the screen offers it. */}
        {!callback && (token || unlocked) && (
          <nav aria-label="Screens">
            {TABS.map((entry) => (
              <button
                key={entry.id}
                type="button"
                aria-current={tab === entry.id ? 'page' : undefined}
                onClick={() => setTab(entry.id)}
              >
                {entry.label}
                {entry.id === 'backlog' && ingestion.length > 0 && (
                  <span className={`tab-count${anyFailed ? ' failed' : ''}`}>
                    {ingestion.length}
                  </span>
                )}
              </button>
            ))}
          </nav>
        )}
      </header>

      <details className="token">
        <summary>API token</summary>
        <p className="hint">
          One shared token for the single Phase 1 account — the same one the RSS feed and
          any script use. Signing in with Google puts a session token in this same slot,
          so this field is the fallback rather than the way in. Stored in this browser
          only. (Connecting a mailbox under Sources is a different thing again: that is
          Google&rsquo;s consent, and its token never comes back here.)
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

      {callback && signingIn ? (
        <SignInCallback callback={callback} onSignedIn={saveToken} onDone={finishCallback} />
      ) : callback ? (
        <OAuthCallback callback={callback} onDone={finishCallback} />
      ) : !token && !unlocked ? (
        <SignIn />
      ) : (
        <>
          {tab === 'paste' && <PasteIn onIngested={refresh} />}
          {tab === 'backlog' && (
            <Backlog
              items={items}
              ingestion={ingestion}
              onChanged={refresh}
              onOpenEpisode={openEpisode}
            />
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
