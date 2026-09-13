// An app shell, and still no router.
//
// The SPA is the eyes-on backlog surface, not the product — "SPA work still running after
// a week" is a named tripwire in AGENTS.md. A handful of screens do not need a routing
// library, a state manager, or a design system, and adding one would be the first step
// toward building a product instead of a factory.
//
// The shell is a sidebar and a top bar (`shell/`), and the URL says which section is open
// — `/backlog`, `/episodes`, `/sources`, `/paste`, `/admin` — through a ~40-line
// `pushState`/`popstate` hook (`shell/useLocation.ts`) rather than a router. A reload
// keeps its place, which a tab held in component state never did, and that is the
// realistic thing to do while a multi-minute pipeline runs. Which section a path means is
// `shell/sections.tsx`; every screen renders unchanged inside the content area.
//
// OAuth is the one path that was always forced on us, because Google redirects to a URL
// rather than back into a running app. It is handled exactly as before: `location` is
// read once at boot (see oauth.ts) and the callback renders instead of the app, with no
// sidebar. Two flows come back on that one path — signing in, and connecting a mailbox —
// and the `state` says which, because it is the only thing that survives the round trip.
//
// **A browser holding no token sees the door and nothing else.** That is the whole point
// of Google Sign-In here: what used to be "open the disclosure and paste MOTET_API_TOKEN"
// is now a button. The disclosure stays — on the door, and in the account menu once
// inside — because the shared token still works and is still the answer when there is no
// Google account to hand; it has just stopped being the thing a human is expected to
// type into a phone.

import { useCallback, useEffect, useState } from 'react'

import {
  ApiError,
  type Episode,
  type IngestionItem,
  type NewsItem,
  type ProcessingStatus,
  type SessionInfo,
  api,
  getToken,
  setToken,
} from './api/client'
import { forgetCallbackUrl, isLoginState, readCallback } from './oauth'
import { Admin } from './screens/Admin'
import { Backlog } from './screens/Backlog'
import { IN_PROGRESS, EpisodeScreen } from './screens/EpisodeScreen'
import { OAuthCallback } from './screens/OAuthCallback'
import { PasteIn } from './screens/PasteIn'
import { SignIn } from './screens/SignIn'
import { SignInCallback } from './screens/SignInCallback'
import { Sources } from './screens/Sources'
import { Popover, Shell } from './shell/Shell'
import { SECTIONS, sectionFor } from './shell/sections'
import { usePath } from './shell/useLocation'

// How often the backlog re-asks while an item is still being processed. Short enough that
// a paste which integrates in seconds is seen to integrate, and it only runs while
// something is pending.
const POLL_MS = 3_000

export default function App() {
  // The path is state, and the section is a function of it. Read in an initializer like
  // the callback below; changed only through `navigate` and the back button.
  const [path, navigate] = usePath()
  const section = sectionFor(path)
  const [items, setItems] = useState<NewsItem[]>([])
  // What has been pasted and is not a news item yet. Held here rather than in the backlog
  // screen because the sidebar counts it too: the person who needs to see it is on the
  // *paste* screen, having just pasted, and would otherwise have no reason to go looking.
  const [ingestion, setIngestion] = useState<IngestionItem[]>([])
  // Whether the last attempt to ask actually got an answer. Kept apart from an empty list
  // because "nothing is being processed" and "I could not find out" are different claims.
  const [ingestionUnavailable, setIngestionUnavailable] = useState(false)
  // Whether anything is draining the queues, or null when the question could not be
  // asked. Best-effort in exactly the way `ingestion` is, and for the same reason.
  const [processing, setProcessing] = useState<ProcessingStatus | null>(null)
  // The episode on screen, and every episode there is.
  //
  // **Both, because `episode` alone was only ever what happened in this page's lifetime.**
  // Nothing loaded it on mount, so a reload — the realistic thing to do while a
  // multi-minute pipeline runs — emptied the tab and left a finished episode reachable
  // only through the RSS feed (motet#44). The list is what makes the second-newest one
  // reachable too, since "make an episode" is the only other way in and it always makes a
  // new one.
  const [episode, setEpisode] = useState<Episode | null>(null)
  const [episodes, setEpisodes] = useState<Episode[]>([])
  // Three states, not two, for the same reason `ingestionUnavailable` exists: "you have no
  // episodes", "I have not looked yet" and "I could not find out" are different claims,
  // and showing the first for either of the others is the disappearance motet#44 is about
  // wearing a different hat.
  const [episodesLoaded, setEpisodesLoaded] = useState(false)
  const [episodesUnavailable, setEpisodesUnavailable] = useState(false)
  const [token, setTokenState] = useState(getToken())
  const [error, setError] = useState('')
  // Read once, in an initializer, so every later render works from state rather than
  // from an address bar the callback is about to rewrite.
  const [callback, setCallback] = useState(readCallback)
  // Who the *server* says this browser is, or null when it says nobody. Best-effort: an
  // older API with no /v1/auth answers 404 and this stays null.
  const [who, setWho] = useState<SessionInfo | null>(null)
  // The token that answer was given for. `who` survives a token change until the next
  // answer lands, so "has the server answered *for this token*" is the question the admin
  // section needs — "checking" and "not an admin" are different sentences, and an admin
  // answer given to the previous token is not one about this one.
  const [whoFor, setWhoFor] = useState<string | null>(null)
  const whoKnown = whoFor === token
  // Why the question could not be answered, when that was not a 401. "Sign in" is the
  // wrong advice to an admin whose API is down.
  const [sessionError, setSessionError] = useState('')
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
    // else entirely.
    //
    // Failing softly is NOT the same as keeping what was on screen. A stale "Queued" that
    // never resolves is the disappearance this panel exists to prevent, wearing a
    // different hat — so a failure clears the list and says so, which also stops the poll
    // below rather than hammering a broken route every three seconds.
    Promise.all([
      api.newsItems(),
      api.ingestion().catch(() => null),
      api.processing().catch(() => null),
    ])
      .then(([nextItems, nextIngestion, nextProcessing]) => {
        setItems(nextItems)
        setIngestion(nextIngestion ?? [])
        setIngestionUnavailable(nextIngestion === null)
        setProcessing(nextProcessing)
        setError('')
      })
      .catch((err) => setError(err instanceof ApiError ? err.message : String(err)))
  }, [])

  // The episode list, loaded once the app has a way in. Separate from `refresh` because
  // it seeds `episode`, and seeding on every three-second poll would drag the screen back
  // to the newest episode while somebody was reading an older one.
  const loadEpisodes = useCallback(() => {
    api
      .episodes()
      .then((list) => {
        // Merged rather than assigned, so an episode created while this request was in
        // flight is not dropped from the picker — `openEpisode` puts it in front, and the
        // server's copy of the list is a moment older than that.
        setEpisodes((current) => {
          const known = new Set(list.map((entry) => entry.id))
          return [...current.filter((entry) => !known.has(entry.id)), ...list]
        })
        // `current ?? list[0]` and never a plain assignment: this runs after an episode may
        // already have been opened from the backlog, and the newest episode is a
        // starting point rather than an override.
        setEpisode((current) => current ?? list[0] ?? null)
        setEpisodesUnavailable(false)
      })
      .catch(() => setEpisodesUnavailable(true))
      .finally(() => setEpisodesLoaded(true))
  }, [])

  // Not while the callback is on screen, and not before there is a token: each has its
  // own thing to say, and a backlog fetch that 401s behind it would put an unrelated
  // error above the answer the user is actually waiting for — "GET /v1/news-items failed:
  // 401" over the top of a sign-in button being the silliest version of that.
  useEffect(() => {
    if (!callback && (token || unlocked)) {
      refresh()
      loadEpisodes()
    }
  }, [callback, loadEpisodes, refresh, token, unlocked])

  // Poll while — and only while — something is actually in flight. Ingestion takes
  // seconds, so an item that resolves has to resolve *on screen*: a status that is only
  // correct until you look away is the same disappearance in slow motion. It stops on its
  // own the moment nothing is pending, so an idle tab makes no requests.
  // An episode mid-pipeline counts too, and not only for the badge: `processing` is
  // fetched by `refresh`, and the episode screen's own "is anything draining the queues"
  // banner would otherwise be computed from a heartbeat frozen at mount — going stale on
  // its own after a few minutes and accusing a worker that is running fine.
  const waiting =
    ingestion.some((item) => item.state === 'pending') ||
    (episode !== null && IN_PROGRESS.has(episode.state))
  // A stuck item gets a louder count than a busy one. "3 in flight" and "3, one of which
  // is never coming back" want different reactions. Settled items are not counted at all:
  // a badge that stays at 3 for ten minutes after everything landed means nothing.
  const anyFailed = ingestion.some((item) => item.state === 'failed')
  const unsettled = ingestion.filter((item) => item.state !== 'integrated').length
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
  // storage, show a sidebar whose every screen 401s, and offer no way back to the door
  // except realising that emptying the *API token* field is what signs you out.
  //
  // An answer for a token that has since changed is dropped rather than applied: it is
  // about a credential this browser no longer holds.
  useEffect(() => {
    let current = true
    api
      .session()
      .then((next) => {
        if (!current) return
        setWho(next)
        setSessionError('')
      })
      .catch((err: unknown) => {
        if (!current) return
        setWho(null)
        const refused = err instanceof ApiError && err.status === 401
        setSessionError(refused ? '' : err instanceof Error ? err.message : String(err))
        if (refused && token) saveToken('')
      })
      .finally(() => {
        if (current) setWhoFor(token)
      })
    return () => {
      current = false
    }
  }, [token, saveToken])

  // The shell is on screen: not the callback, and not the door.
  const inShell = !callback && Boolean(token || unlocked)

  // `/`, an unknown path and a trailing slash render a section; the address then says which
  // too, so the sidebar, the address bar and a reload all agree. `replace`, because the
  // path the browser arrived on was never a place of its own and Back should not return to
  // it. Not on the door or the callback: the callback's address is `forgetCallbackUrl`'s,
  // and a deep link held behind the door survives a pasted token (a Google sign-in comes
  // back through the callback, and lands on HOME).
  useEffect(() => {
    if (inShell && window.location.pathname !== section.path) {
      navigate(section.path, { replace: true })
    }
  }, [inShell, navigate, path, section.path])

  // Every history entry says which section it is, so the Back button's list is readable.
  useEffect(() => {
    document.title = inShell ? `${section.label} · Motet` : 'Motet'
  }, [inShell, section.label])

  const finishCallback = () => {
    setCallback(null)
    // Back to where the flow started from: a mailbox connection belongs on Sources, and a
    // sign-in belongs at the front of the app the person was trying to reach. `replace`,
    // because `forgetCallbackUrl` has already swapped the callback's entry for `/`, and a
    // second entry would put a spent code's page one Back away.
    navigate(signingIn ? '/' : '/sources', { replace: true })
  }

  const openEpisode = (next: Episode) => {
    setEpisode(next)
    // In front, and de-duplicated: the backlog's button makes a *new* episode, so this is
    // normally an id the list has never seen.
    setEpisodes((list) => [next, ...list.filter((entry) => entry.id !== next.id)])
    navigate('/episodes')
  }

  // The polling episode screen reports every state change. The list has to hear it too,
  // or the picker keeps saying "pending" about an episode that finished ten minutes ago.
  const episodeChanged = useCallback((next: Episode) => {
    setEpisode(next)
    setEpisodes((list) => list.map((entry) => (entry.id === next.id ? next : entry)))
  }, [])

  const signOut = () => {
    // Fire and forget the revoke, then drop the token locally whatever the server said —
    // a browser that has decided to sign out must not stay signed in because a request
    // failed. The row expires on its own if the call never lands.
    api.logout().catch(() => undefined)
    saveToken('')
    setWho(null)
  }

  // The API token disclosure: on the door inline, because SignIn points at it; inside the
  // app, in the account menu.
  const tokenField = <TokenField token={token} onSave={saveToken} />

  const errorLine = error && (
    <p className="error" role="alert">
      {error}
    </p>
  )

  // The callback and the door render without the sidebar: there is one thing to do on
  // either screen, and the screen offers it. Nothing to navigate to yet, either.
  if (!inShell) {
    return (
      <div className="door">
        <header className="door-bar">
          <h1 className="brand">Motet</h1>
        </header>
        <main className="door-main">
          {errorLine}
          {callback && signingIn ? (
            <SignInCallback callback={callback} onSignedIn={saveToken} onDone={finishCallback} />
          ) : callback ? (
            <OAuthCallback callback={callback} onDone={finishCallback} />
          ) : (
            <>
              {tokenField}
              <SignIn />
            </>
          )}
        </main>
      </div>
    )
  }

  // The address alone as the button, not "signed in as …": the menu it opens says what
  // state this is. With no address — the shared token, or an open deployment — the button
  // says "Account" and the menu says which.
  const account = (
    <Popover label={who?.email ?? 'Account'}>
      {who?.email ? (
        <>
          <p className="hint">Signed in with Google.</p>
          <button type="button" onClick={signOut}>
            Sign out
          </button>
        </>
      ) : who?.how === 'token' ? (
        <p className="hint">Using the shared API token.</p>
      ) : unlocked ? (
        <p className="hint">This deployment has no lock on it. Everything answers.</p>
      ) : null}
      {tokenField}
    </Popover>
  )

  // The Admin item only for a caller the server says is an admin, so nobody is offered a
  // screen the API would refuse them. The path itself is still a section: typed by
  // anybody else, it says why rather than asking for everybody's data.
  const isAdmin = whoKnown && who?.admin === true
  const offered = SECTIONS.filter((entry) => entry.id !== 'admin' || isAdmin)

  return (
    <Shell
      section={section}
      sections={offered}
      onNavigate={navigate}
      badge={{ count: unsettled, failed: anyFailed }}
      account={account}
    >
      {errorLine}
      {section.id === 'paste' && <PasteIn onIngested={refresh} />}
      {section.id === 'backlog' && (
        <Backlog
          items={items}
          ingestion={ingestion}
          ingestionUnavailable={ingestionUnavailable}
          processing={processing}
          onChanged={refresh}
          onOpenEpisode={openEpisode}
        />
      )}
      {section.id === 'episodes' &&
        (episode ? (
          <EpisodeScreen
            episode={episode}
            episodes={episodes}
            processing={processing}
            onEpisodeChanged={episodeChanged}
            onSelectEpisode={setEpisode}
            onBacklogChanged={refresh}
          />
        ) : (
          <section aria-label="Episode">
            <p className="hint">
              {!episodesLoaded
                ? 'Looking for your episodes…'
                : episodesUnavailable
                  ? 'Could not load your episodes. This is not the same as having none.'
                  : 'Make one from the backlog.'}
            </p>
          </section>
        ))}
      {section.id === 'sources' && <Sources />}
      {section.id === 'admin' &&
        (isAdmin ? (
          <Admin />
        ) : (
          // Asked of the session before the overview, so somebody who is not an admin
          // gets a sentence rather than a failed request for every user's data. That is
          // presentation: the API refuses `/v1/admin/*` to them regardless.
          <section aria-label="Admin">
            {!whoKnown ? (
              <p className="hint">Checking whether this account is an admin…</p>
            ) : (
              <p role="alert">
                {who === null
                  ? sessionError || 'Sign in first — the admin view needs a signed-in account.'
                  : who.how === 'session'
                    ? `${who.email ?? 'This account'} is not an admin on this deployment.`
                    : who.how === 'token'
                      ? 'The admin view needs a signed-in Google account; the shared API token is not one.'
                      : 'The admin view needs a signed-in Google account, and this deployment has no sign-in lock at all.'}
              </p>
            )}
          </section>
        ))}
    </Shell>
  )
}

/**
 * The API token field. One shared token for the single Phase 1 account, the same one the
 * RSS feed and any script use; signing in with Google puts a session token in this same
 * slot, so this field is the fallback rather than the way in.
 *
 * **Saved on submit, not on every keystroke.** The door and the shell are different trees,
 * so a field that saved as it was typed swapped the door for the app on the first
 * character and threw the half-typed token away with the door — and every partial token
 * it did save was a `/v1/auth/session` call answered 401.
 */
function TokenField({ token, onSave }: { token: string; onSave: (value: string) => void }) {
  const [draft, setDraft] = useState(token)
  return (
    <details className="token">
      <summary>API token</summary>
      <p className="hint">
        One shared token for the single Phase 1 account — the same one the RSS feed and
        any script use. Signing in with Google puts a session token in this same slot,
        so this field is the fallback rather than the way in. Stored in this browser
        only. (Connecting a mailbox under Sources is a different thing again: that is
        Google&rsquo;s consent, and its token never comes back here.)
      </p>
      <form
        onSubmit={(event) => {
          event.preventDefault()
          onSave(draft.trim())
        }}
      >
        <input
          aria-label="API token"
          type="password"
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
          placeholder="MOTET_API_TOKEN"
        />
        <button type="submit">Use this token</button>
      </form>
    </details>
  )
}
