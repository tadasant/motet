import { fireEvent, render, screen, waitFor } from '@testing-library/react'
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

  it('hands a sign-in to the iOS app only once the person confirms, and stores nothing', async () => {
    // Any app on the phone can start a native sign-in and wait for the link, so the page
    // asks before following it. The in-app browser shares Safari's storage, so nothing is
    // kept here either way.
    answerCallbackWith({ token: null, email: 'owner@motet.test', expires_at: null, handoff_url: 'motet://signed-in?code=one-time' })
    const onSignedIn = vi.fn()
    const handOff = vi.fn()

    render(<SignInCallback callback={GRANTED} onSignedIn={onSignedIn} onDone={() => {}} handOff={handOff} />)

    const button = await screen.findByRole('button', { name: 'Continue to the Motet app' })
    expect(handOff).not.toHaveBeenCalled()
    fireEvent.click(button)
    expect(handOff).toHaveBeenCalledWith('motet://signed-in?code=one-time')
    expect(onSignedIn).not.toHaveBeenCalled()
  })

  it('refuses a handoff link that is not the API’s motet:// link', async () => {
    answerCallbackWith({ token: null, email: 'owner@motet.test', expires_at: null, handoff_url: 'javascript:alert(1)' })
    const handOff = vi.fn()

    render(<SignInCallback callback={GRANTED} onSignedIn={vi.fn()} onDone={() => {}} handOff={handOff} />)

    expect(await screen.findByRole('alert')).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Continue to the Motet app' })).toBeNull()
    expect(handOff).not.toHaveBeenCalled()
  })
})
