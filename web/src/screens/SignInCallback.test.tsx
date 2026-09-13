import { render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { OAuthCallback } from '../oauth'
import { SignInCallback } from './SignInCallback'

const GRANTED: OAuthCallback = { kind: 'granted', code: 'google-code', state: 'login.abc' }

/** A fetch that answers the sign-in callback route with `body`. */
function answerCallbackWith(body: unknown) {
  vi.stubGlobal(
    'fetch',
    vi.fn(async () => ({ ok: true, status: 200, statusText: 'OK', json: async () => body }) as Response),
  )
}

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('SignInCallback', () => {
  it('stores the session a browser sign-in returns', async () => {
    answerCallbackWith({ token: 'session-token', email: 'owner@motet.test', expires_at: '2026-10-13T00:00:00Z' })
    const onSignedIn = vi.fn()
    const handOff = vi.fn()

    render(<SignInCallback callback={GRANTED} onSignedIn={onSignedIn} onDone={() => {}} handOff={handOff} />)

    await waitFor(() => expect(onSignedIn).toHaveBeenCalledWith('session-token'))
    expect(handOff).not.toHaveBeenCalled()
    expect(await screen.findByText('Signed in as owner@motet.test.')).toBeTruthy()
  })

  it('hands a sign-in the iOS app started back to the app and stores nothing', async () => {
    // The in-app browser shares Safari's storage, so a session kept here would outlive the
    // flow on a browser nobody is looking at. The link is the API's, followed as given.
    answerCallbackWith({ token: null, email: 'owner@motet.test', expires_at: null, handoff_url: 'motet://signed-in?code=one-time' })
    const onSignedIn = vi.fn()
    const handOff = vi.fn()

    render(<SignInCallback callback={GRANTED} onSignedIn={onSignedIn} onDone={() => {}} handOff={handOff} />)

    await waitFor(() => expect(handOff).toHaveBeenCalledWith('motet://signed-in?code=one-time'))
    expect(onSignedIn).not.toHaveBeenCalled()
    expect(await screen.findByText(/Returning you to the Motet app/)).toBeTruthy()
  })
})
