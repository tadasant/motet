// The SPA's entire routing, which is one path and two query parameters.
//
// Worth testing on its own because it is the piece that only breaks in a browser: every
// other screen is reached by pressing a button, this one is reached by Google navigating
// to a URL, and getting it wrong means a user who granted consent lands on the paste-in
// screen with no idea whether it worked.

import { afterEach, describe, expect, it } from 'vitest'

import {
  forgetCallbackUrl,
  isLoginState,
  readCallback,
  redirectUri,
  stateMatches,
  takeState,
} from './oauth'

/** jsdom has a real History, so this is how the tests put the app on a path. */
function visit(url: string) {
  window.history.replaceState({}, '', url)
}

afterEach(() => {
  visit('/')
  window.sessionStorage.clear()
})

describe('isLoginState', () => {
  it('tells a sign-in callback from a mailbox one', () => {
    // Both flows land on the one /oauth/callback path, and `state` is the only value
    // guaranteed to survive the round trip through the provider — so it is the only
    // thing that can say which finished.
    expect(isLoginState('login.abc123')).toBe(true)
    expect(isLoginState('abc123')).toBe(false)
  })

  it('uses a marker the API cannot mint by accident', () => {
    // The API's own copy of this prefix lives in `motet_api.auth.registry`, pinned by a
    // test there for the same reason. A dot is safe because `secrets.token_urlsafe`
    // emits only [A-Za-z0-9_-], so a mailbox state can never look like a sign-in one.
    expect(isLoginState('login_abc')).toBe(false)
    expect(isLoginState('loginabc')).toBe(false)
  })
})

describe('redirectUri', () => {
  it('is the callback path on whatever origin the bundle is served from', () => {
    // One bundle, three registered URIs — the same property config.js buys for the API
    // origin. jsdom's default origin stands in for a deployment's.
    expect(redirectUri()).toBe(`${window.location.origin}/oauth/callback`)
  })
})

describe('readCallback', () => {
  it('is null on a normal load, so the tab strip renders', () => {
    visit('/')
    expect(readCallback()).toBeNull()
  })

  it('reads the code and state Google sent back', () => {
    visit('/oauth/callback?code=abc123&state=st_1&scope=https%3A%2F%2Fmail')
    expect(readCallback()).toEqual({ kind: 'granted', code: 'abc123', state: 'st_1' })
  })

  it('tolerates a trailing slash, which some providers add', () => {
    visit('/oauth/callback/?code=abc123&state=st_1')
    expect(readCallback()).toMatchObject({ kind: 'granted' })
  })

  it('reports a refusal instead of looking for a code', () => {
    // The user pressed Cancel. There is no code, and asking the API to exchange one
    // would turn a supported answer into an error.
    visit('/oauth/callback?error=access_denied&state=st_1')
    // `state` comes back even on a refusal: it is what says *which* flow was refused,
    // and "you did not grant access to your mailbox" and "you did not finish signing in"
    // are different sentences.
    expect(readCallback()).toEqual({
      kind: 'denied',
      error: 'access_denied',
      description: '',
      state: 'st_1',
    })
  })

  it('is empty on the callback path with nothing to exchange', () => {
    visit('/oauth/callback')
    expect(readCallback()).toEqual({ kind: 'empty' })
  })
})

describe('forgetCallbackUrl', () => {
  it('puts the address bar back, so a reload does not replay a spent code', () => {
    visit('/oauth/callback?code=abc123&state=st_1')
    forgetCallbackUrl()
    expect(window.location.pathname).toBe('/')
    expect(window.location.search).toBe('')
  })
})

describe('stateMatches', () => {
  it('rejects a callback this tab did not start', () => {
    expect(stateMatches('st_1', 'st_2')).toBe(false)
  })

  it('accepts one it did', () => {
    expect(stateMatches('st_1', 'st_1')).toBe(true)
  })

  it('accepts when nothing was remembered, and lets the API judge', () => {
    // A restored tab, a store that threw, or consent finished elsewhere — none of which
    // is evidence of an attack, and the API consumes the state row exactly once anyway.
    expect(stateMatches('', 'st_1')).toBe(true)
  })
})

describe('takeState', () => {
  it('is good for exactly one callback', () => {
    window.sessionStorage.setItem('motet.oauthState', 'st_1')
    expect(takeState()).toBe('st_1')
    expect(takeState()).toBe('')
  })
})
