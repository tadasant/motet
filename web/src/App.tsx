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
// sidebar. Three flows come back on that one path — signing in, connecting a mailbox, and
// authorizing an MCP server from Credentials, and an MCP client's authorization of Motet —
// and the `state` says which, because it is the only thing that survives the round trip.
//
// **A browser holding no token sees the door and nothing else** — and since the landing
// page moved to `getmotet.com` (`site/`), the door is a sign-in and nothing else either:
// no hero, no pitch, no second copy of what that site says. That is the whole point of
// Google Sign-In here: what used to be "open the disclosure and paste MOTET_API_TOKEN" is
// now a button. The disclosure stays — on the door, and in the account menu once inside —
// because the shared token still works and is still the answer when there is no Google
// account to hand; it has just stopped being the thing a human is expected to type into a
// phone. **A browser that does hold one never sees the door**: `inShell` is true as soon
// as there is a token (or the deployment is open) and no callback is in the address, and
// it renders the shell.

import { useCallback, useEffect, useRef, useState } from 'react'

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
import { Wordmark } from './brand/Brand'
import {
  consentBegunElsewhere,
  forgetCallbackUrl,
  isConnectorState,
  isLoginState,
  isMcpState,
  readCallback,
} from './oauth'
import { Admin } from './screens/Admin'
import { Backlog } from './screens/Backlog'
import { Credentials } from './screens/Credentials'
import { ConnectorCallback } from './screens/credentials/ConnectorCallback'
import { IN_PROGRESS } from './screens/EpisodeScreen'
import { Episodes, newestFirst } from './screens/Episodes'
import { McpAuthorizeCallback } from './screens/McpAuthorizeCallback'
import { AppConsentHandoff } from './screens/AppConsentHandoff'
import { OAuthCallback, explain as explainDenial } from './screens/OAuthCallback'
import { PasteIn } from './screens/PasteIn'
import { SignIn } from './screens/SignIn'
import { AppHandoff } from './screens/AppHandoff'
import { HANDOFF_PATH, SignInCallback } from './screens/SignInCallback'
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
  // How many polled items are held — extracted, and waiting for a person to press Ingest
  // now or Dismiss (motet#91). Only the count: the Held panel owns the list and polls it
  // itself. Here because the sidebar badge counts them, and the badge has to be right on
  // every section, not only on the one where that panel is mounted.
  const [heldCount, setHeldCount] = useState(0)
  // Whether anything is draining the queues, or null when the question could not be
  // asked. Best-effort in exactly the way `ingestion` is, and for the same reason.
  const [processing, setProcessing] = useState<ProcessingStatus | null>(null)
  // Every episode there is, and which one's detail the Episodes section has open.
  //
  // **The list, because one episode was only ever what happened in this page's lifetime.**
  // Nothing loaded episodes on mount, so a reload — the realistic thing to do while a
  // multi-minute pipeline runs — emptied the tab and left a finished episode reachable
  // only through the RSS feed (motet#44). The section is a shelf of all of them now
  // (motet#89), so the list is the thing it shows.
  //
  // **The open id lives here, not in the section**, because the section unmounts whenever
  // another one is open and "which episode was I looking at" has to survive that. An id
  // rather than a copy of the episode, so there is one copy of each episode to keep
  // current — the refresh below and a position report both write the list, and the detail
  // reads its episode out of it. Null is the shelf.
  const [episodes, setEpisodes] = useState<Episode[]>([])
  const [openEpisodeId, setOpenEpisodeId] = useState<string | null>(null)
  // Whether the Episodes section has been shown yet — see the landing rule below.
  const landed = useRef(false)
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
  // What Google said when it refused, carried from the callback page to the screen the
  // flow started on (motet#98). Cancelling a consent is an answer, and until this existed
  // the only trace of it on Sources was the card's "Consent not finished" and a row notice
  // that cannot tell a Cancel from a closed tab. Cleared when
  // the section changes, so it is said once rather than sitting there for the rest of the
  // session.
  const [consentNotice, setConsentNotice] = useState('')
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
  // The third flow on that path: an MCP client's authorization, which this tab did not
  // start and which ends in a question rather than a result (motet#111).
  const authorizingMcp =
    callback !== null && callback.kind !== 'empty' && isMcpState(callback.state)
  // A mailbox or connector consent this tab did not begin: the iOS app's, finishing in the
  // system sign-in sheet (`consentBegunElsewhere`). It goes back to the app rather than
  // being exchanged here (`AppConsentHandoff`). Decided once, at boot, because the callback
  // screen that would otherwise run takes the remembered state.
  const [consentForApp, setConsentForApp] = useState(
    () =>
      callback !== null &&
      callback.kind !== 'empty' &&
      !isLoginState(callback.state) &&
      !isMcpState(callback.state) &&
      consentBegunElsewhere(),
  )

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

    // Best-effort and on its own, like the episode list below: a held route that fails must
    // not blank the backlog. A failed read keeps the last count rather than zeroing it — a
    // blip is not evidence that the held items went away.
    api
      .heldSourceItems()
      .then((list) => setHeldCount(list.length))
      .catch(() => {})

    // The episode list rides the same refresh, so a Mark listened, a render finishing, or
    // a position reported from another device shows without a reload — and the shelf
    // needs no fetch of its own. Its own promise rather than a fourth member of the one
    // above: a failed episode list must not blank the backlog, nor the reverse.
    //
    // It is the heaviest of the four (every episode, with its claims), and it is polled
    // only while something is in flight, exactly like the rest.
    api
      .episodes()
      .then((list) => {
        // **Merged, and never a change to which episode is open.** Merged so an episode
        // created while this request was in flight is not dropped — `openEpisode` puts it
        // in front, and the server's copy of the list is a moment older than that. The
        // position is kept at the larger of the two copies because the server's is
        // monotonic and a report answered after this request left is newer than it.
        setEpisodes((current) => {
          const mine = new Map(current.map((entry) => [entry.id, entry]))
          const known = new Set(list.map((entry) => entry.id))
          return [
            ...current.filter((entry) => !known.has(entry.id)),
            ...list.map((entry) => {
              const local = mine.get(entry.id)
              return local && local.listened_through_ms > entry.listened_through_ms
                ? { ...entry, listened_through_ms: local.listened_through_ms }
                : entry
            }),
          ]
        })
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
    if (!callback && (token || unlocked)) refresh()
  }, [callback, refresh, token, unlocked])

  // Poll while — and only while — something is actually in flight. Ingestion takes
  // seconds, so an item that resolves has to resolve *on screen*: a status that is only
  // correct until you look away is the same disappearance in slow motion. It stops on its
  // own the moment nothing is pending, so an idle tab makes no requests.
  // An episode mid-pipeline counts too — any of them, not only an open one. The refresh is
  // what moves a shelf row from Working… to ready and what moves the open detail along its
  // stages, and it fetches `processing`, without which the detail's "is anything draining
  // the queues" banner would be computed from a heartbeat frozen at mount — going stale on
  // its own after a few minutes and accusing a worker that is running fine.
  const waiting =
    ingestion.some((item) => item.state === 'pending') ||
    episodes.some((entry) => IN_PROGRESS.has(entry.state))
  // The badge counts what needs you (motet#98): held items, which nothing will
  // move until somebody picks them, and failed ones, which nothing will move at all. An
  // item a worker is still carrying is not counted — it needs nobody, and the Processing
  // panel already says it is on its way. Before motet#91 the held count was in here by
  // accident, as "pending" rows of the ingestion list; this makes it the meaning on
  // purpose. It is Sources' "Waiting for you" tile (held) plus its "Failed" tile, summed
  // across sources. A failure still makes the count loud: "3 to pick" and "3, one of which
  // is never coming back" want different reactions.
  const failedCount = ingestion.filter((item) => item.state === 'failed').length
  const anyFailed = failedCount > 0
  const needsYou = heldCount + failedCount
  useEffect(() => {
    if (!waiting || callback || !(token || unlocked)) return
    const timer = window.setInterval(refresh, POLL_MS)
    return () => window.clearInterval(timer)
  }, [waiting, callback, refresh, token, unlocked])

  // The landing rule, applied the first time the Episodes section is shown with the list
  // in hand (motet#89, question 4). The shelf is the landing — except when an episode is
  // *still* being made at that moment: that is almost always the one somebody just asked
  // for, its Working… copy and its "not moving" banner live on the detail, and a reload
  // mid-render is the realistic way to arrive. Once, and only then: judged at a later visit
  // it would open a render that finished long ago, and judged on a later refresh it would
  // pull the screen out from under somebody.
  useEffect(() => {
    // Not on a failed first answer either: an empty list says nothing about what is being
    // made, and spending the rule on it would mean it never applies once a refresh lands.
    if (landed.current || section.id !== 'episodes' || !episodesLoaded || episodesUnavailable) {
      return
    }
    landed.current = true
    const making = [...episodes].sort(newestFirst).find((entry) => IN_PROGRESS.has(entry.state))
    if (making) setOpenEpisodeId((current) => current ?? making.id)
  }, [episodes, episodesLoaded, episodesUnavailable, section.id])

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
        // A 429 counts as a refusal here, not as an outage. The API throttles only
        // requests that *already failed to authenticate* (`motet_api.throttle`), so a 429
        // on this call means this credential was refused and the process had spent its
        // budget of saying so — a valid one is never throttled. Read as an error instead,
        // it would leave a dead token in storage behind a sentence nobody can act on.
        const refused =
          err instanceof ApiError && (err.status === 401 || err.status === 429)
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
  // The iOS app's https handoff link. Held apart from the shell and the door for the same
  // reason /oauth/callback is: the sheet is watching for this path, and a browser that gets
  // here instead needs one sentence rather than a section.
  const appHandoff = path === HANDOFF_PATH
  const inShell = !callback && !appHandoff && Boolean(token || unlocked)

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

  // The consent notice belongs to the visit it was raised for. Cleared here rather than
  // when Sources unmounts, because StrictMode unmounts and remounts a screen on purpose
  // and a cleanup would take the notice down before it had been read once.
  useEffect(() => {
    if (section.id !== 'sources') setConsentNotice('')
  }, [section.id])

  // Every history entry says which section it is, so the Back button's list is readable.
  useEffect(() => {
    document.title = inShell ? `${section.label} · Motet` : 'Motet'
  }, [inShell, section.label])

  // The third flow on the callback path: an MCP server's consent, begun on Credentials.
  const authorizingConnector =
    callback !== null && callback.kind !== 'empty' && isConnectorState(callback.state)

  const finishCallback = () => {
    if (authorizingConnector) {
      // Its own screen says what happened, including a Cancel; Credentials shows the row.
      setCallback(null)
      navigate('/credentials', { replace: true })
      return
    }
    // Only a mailbox consent has a Sources row to explain; a refused sign-in or MCP
    // authorization said its sentence on the callback page.
    if (!signingIn && !authorizingMcp && callback?.kind === 'denied') {
      // For a Cancel, say the one thing the Sources row cannot know — that it *was* a
      // Cancel — rather than repeating the row's own "nothing was connected" beside it.
      setConsentNotice(
        callback.error === 'access_denied'
          ? 'Google says you did not grant access: that Gmail attempt was cancelled, not left unfinished.'
          : explainDenial(callback.error, callback.description),
      )
    }
    setCallback(null)
    // Back to where the flow started from: a mailbox connection belongs on Sources, and a
    // sign-in belongs at the front of the app the person was trying to reach. `replace`,
    // because `forgetCallbackUrl` has already swapped the callback's entry for `/`, and a
    // second entry would put a spent code's page one Back away. An MCP authorization that
    // did not finish started at an agent, not in this app, so it goes to the front too.
    navigate(signingIn || authorizingMcp ? '/' : '/sources', { replace: true })
  }

  // "Make an episode" on the backlog: straight to the new one's detail, skipping the shelf,
  // because its Working… copy is what somebody who just asked for it wants to see.
  const openEpisode = (next: Episode) => {
    // In front, and de-duplicated: the backlog's button makes a *new* episode, so this is
    // normally an id the list has never seen.
    setEpisodes((list) => [next, ...list.filter((entry) => entry.id !== next.id)])
    setOpenEpisodeId(next.id)
    navigate('/episodes')
  }

  // A position the server has confirmed — from the player, or from Mark listened — moves
  // the shelf at once rather than on the next refresh. Never downwards: it is monotonic.
  const positionReported = useCallback((episodeId: string, listenedThroughMs: number) => {
    setEpisodes((list) =>
      list.map((entry) =>
        entry.id === episodeId && listenedThroughMs > entry.listened_through_ms
          ? { ...entry, listened_through_ms: listenedThroughMs }
          : entry,
      ),
    )
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
  if (appHandoff) {
    return (
      <div className="door">
        <header className="door-bar">
          <Wordmark className="brand" />
        </header>
        <main className="door-main">
          <AppHandoff onDone={() => navigate('/')} />
        </main>
      </div>
    )
  }

  if (!inShell) {
    return (
      <div className="door">
        <header className="door-bar">
          <Wordmark className="brand" />
        </header>
        <main className="door-main">
          {errorLine}
          {/* The app's handoff first: exchanging its code here would spend it without the
              app's session. Then MCP: its state is neither a sign-in's nor a mailbox's, and
              sending it to either route would burn it. */}
          {callback && callback.kind !== 'empty' && consentForApp ? (
            <AppConsentHandoff callback={callback} onFinishHere={() => setConsentForApp(false)} />
          ) : callback && authorizingMcp ? (
            <McpAuthorizeCallback callback={callback} onDone={finishCallback} />
          ) : callback && signingIn ? (
            <SignInCallback callback={callback} onSignedIn={saveToken} onDone={finishCallback} />
          ) : callback && authorizingConnector ? (
            <ConnectorCallback callback={callback} onDone={finishCallback} />
          ) : callback ? (
            <OAuthCallback callback={callback} onDone={finishCallback} />
          ) : (
            <SignIn tokenField={tokenField} />
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
      {who?.how === 'session' ? (
        <>
          <p className="hint">Signed in with Google.</p>
          <button type="button" onClick={signOut}>
            Sign out
          </button>
        </>
      ) : who?.how === 'pat' ? (
        // A PAT carries the address of the session that minted it, so it has an email and
        // is not a sign-in: `/v1/auth/logout` is a no-op for one, and offering Sign out
        // would be a button that does nothing. Revoking is on the Credentials screen,
        // which needs a session — so this says where, rather than offering a dead control.
        <p className="hint">
          Using an access token for {who.email}. Sign in to manage or revoke it.
        </p>
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
      badge={{ count: needsYou, failed: anyFailed }}
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
      {section.id === 'episodes' && (
        <Episodes
          episodes={episodes}
          openId={openEpisodeId}
          loaded={episodesLoaded}
          unavailable={episodesUnavailable}
          processing={processing}
          onOpen={(next) => setOpenEpisodeId(next.id)}
          onBack={() => setOpenEpisodeId(null)}
          onPositionReported={positionReported}
          onChanged={refresh}
        />
      )}
      {section.id === 'sources' && <Sources notice={consentNotice} />}
      {section.id === 'credentials' && <Credentials signedIn={who?.how === 'session'} />}
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
                      : who.how === 'pat'
                        ? 'The admin view needs a signed-in Google account; an access token is not one.'
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
