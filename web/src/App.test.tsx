import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import App from './App'
import type { HealthResponse } from './api/client'

describe('App', () => {
  it('renders the three Phase 1 screens', () => {
    render(<App />)
    for (const heading of ['Paste in', 'Backlog', 'Episode']) {
      expect(screen.getByRole('heading', { name: heading })).toBeDefined()
    }
  })
})

describe('the generated contract', () => {
  it('types /healthz off openapi.yaml', () => {
    // Compile-time assertion: if the API drops a field, `bin/ci` regenerates
    // schema.gen.ts, this stops type-checking, and the drift is caught here rather
    // than in a browser.
    const health: HealthResponse = {
      status: 'ok',
      service: 'motet-api',
      telemetry_configured: false,
      errors_configured: false,
    }
    expect(health.status).toBe('ok')
  })
})
