import { fireEvent, render, screen } from '@testing-library/react'
import { StrictMode } from 'react'
import { describe, expect, it, vi } from 'vitest'

import { AppConsentHandoff } from './AppConsentHandoff'

describe('handing a consent back to the iOS app', () => {
  it('sends the code and state to motet://consent once, on arrival', () => {
    const navigate = vi.fn()
    render(
      <StrictMode>
        <AppConsentHandoff
          callback={{ kind: 'granted', code: 'c/1+2', state: 'st_1' }}
          onFinishHere={vi.fn()}
          navigate={navigate}
        />
      </StrictMode>,
    )
    expect(navigate).toHaveBeenCalledTimes(1)
    expect(navigate).toHaveBeenCalledWith('motet://consent?code=c%2F1%2B2&state=st_1')
  })

  it('carries a connector issuer, and a refusal as a refusal', () => {
    const navigate = vi.fn()
    const { unmount } = render(
      <AppConsentHandoff
        callback={{ kind: 'granted', code: 'c', state: 'connector.s', iss: 'https://mcp.example' }}
        onFinishHere={vi.fn()}
        navigate={navigate}
      />,
    )
    expect(navigate).toHaveBeenLastCalledWith(
      'motet://consent?code=c&state=connector.s&iss=https%3A%2F%2Fmcp.example',
    )
    unmount()

    render(
      <AppConsentHandoff
        callback={{ kind: 'denied', error: 'access_denied', description: '', state: 'st_1' }}
        onFinishHere={vi.fn()}
        navigate={navigate}
      />,
    )
    expect(navigate).toHaveBeenLastCalledWith('motet://consent?error=access_denied&state=st_1')
  })

  it('offers the link again, and finishing in this browser instead', () => {
    const navigate = vi.fn()
    const onFinishHere = vi.fn()
    render(
      <AppConsentHandoff
        callback={{ kind: 'granted', code: 'c', state: 's' }}
        onFinishHere={onFinishHere}
        navigate={navigate}
      />,
    )
    fireEvent.click(screen.getByRole('button', { name: 'Open the Motet app' }))
    expect(navigate).toHaveBeenCalledTimes(2)
    fireEvent.click(screen.getByRole('button', { name: 'Finish here instead' }))
    expect(onFinishHere).toHaveBeenCalled()
  })
})
