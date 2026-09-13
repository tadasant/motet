import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { LlmConfig, LlmSpend, LlmSpendReport, LlmStageConfig } from '../api/client'
import { ModelsAndSpend } from './ModelsAndSpend'

const SONNET = 'anthropic/claude-sonnet-5'
const HAIKU = 'anthropic/claude-haiku-4.5'

const stage = (name: string, overrides: Partial<LlmStageConfig> = {}): LlmStageConfig => ({
  stage: name,
  model: SONNET,
  model_source: 'default',
  effort: 'low',
  effort_source: 'default',
  setting_model: null,
  stage_env_model: null,
  global_env_model: null,
  default_model: SONNET,
  setting_effort: null,
  stage_env_effort: null,
  global_env_effort: null,
  default_effort: 'low',
  ...overrides,
})

const model = (slug: string, efforts: string[]) => ({
  slug,
  efforts,
  adaptive_thinking: true,
  reasoning_on_by_default: true,
  input_usd_per_mtok: 2,
  output_usd_per_mtok: 10,
  cache_read_usd_per_mtok: 0.2,
  cache_write_usd_per_mtok: 2.5,
  cache_write_1h_usd_per_mtok: 4,
})

const config = (writable: boolean, stages = [stage('dedup')]): LlmConfig => ({
  stages,
  models: [model(SONNET, ['low', 'medium', 'high']), model(HAIKU, [])],
  precedence: ['settings', 'stage_env', 'global_env', 'default'],
  applies: 'next_job',
  writable,
  writable_env: 'MOTET_SETTINGS_WRITABLE',
  settings_error: null,
})

const spend = (usd: number, unpriced = 0): LlmSpend => ({
  completions: 3,
  input_tokens: 1000,
  output_tokens: 100,
  reasoning_tokens: 0,
  cache_read_tokens: 0,
  cache_write_tokens: 0,
  usd,
  unpriced_completions: unpriced,
})

const SPEND: LlmSpendReport = {
  generated_at: '2026-09-13T00:00:00Z',
  since: '2026-09-12T00:00:00Z',
  window_days: 7,
  retention_days: 90,
  total: {
    stages: { dedup: spend(12.5, 1) },
    queues: { integrate: spend(12.5) },
    users: [{ user_id: 'motet-owner', email: null, spend: spend(12.5) }],
  },
  window: {
    stages: { dedup: spend(1.25) },
    queues: { integrate: spend(1.25) },
    users: [{ user_id: 'motet-owner', email: null, spend: spend(1.25) }],
  },
}

function mockApi(configResponse: LlmConfig, afterPut?: LlmConfig) {
  const calls: { url: string; method: string; body: unknown }[] = []
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      const method = init?.method ?? 'GET'
      calls.push({ url, method, body: init?.body ? JSON.parse(String(init.body)) : undefined })
      const body = url.includes('/v1/admin/llm-spend')
        ? SPEND
        : method === 'PUT'
          ? (afterPut ?? configResponse)
          : configResponse
      return { ok: true, status: 200, statusText: 'OK', json: async () => body } as Response
    }),
  )
  return calls
}

beforeEach(() => window.localStorage.setItem('motet.apiToken', 'test-token'))
afterEach(() => vi.unstubAllGlobals())

describe('Models & spend', () => {
  it('is read-only where the deployment does not honour settings, and says why', async () => {
    const calls = mockApi(config(false))
    render(<ModelsAndSpend />)

    const select = (await screen.findByLabelText('dedup model')) as HTMLSelectElement
    expect(select.disabled).toBe(true)
    expect(screen.getByText('MOTET_SETTINGS_WRITABLE')).toBeDefined()
    expect(screen.queryByRole('button', { name: 'reset' })).toBeNull()
    expect(calls.filter((call) => call.method === 'PUT')).toEqual([])
  })

  it('saves a model change, pairing a slug with no effort with off', async () => {
    const calls = mockApi(
      config(true),
      config(true, [
        stage('dedup', {
          model: HAIKU,
          model_source: 'settings',
          setting_model: HAIKU,
          effort: 'off',
          effort_source: 'settings',
          setting_effort: 'off',
        }),
      ]),
    )
    render(<ModelsAndSpend />)

    const select = (await screen.findByLabelText('dedup model')) as HTMLSelectElement
    expect(select.disabled).toBe(false)
    fireEvent.change(select, { target: { value: HAIKU } })

    await waitFor(() => expect(calls.some((call) => call.method === 'PUT')).toBe(true))
    const put = calls.find((call) => call.method === 'PUT')
    expect(put?.url).toMatch(/\/v1\/admin\/llm-config\/dedup$/)
    expect(put?.body).toEqual({ model: HAIKU, effort: 'off' })
    expect(await screen.findByRole('button', { name: 'reset' })).toBeDefined()
  })

  it('shows the week and the total, and marks a total with an unpriced model as a floor', async () => {
    mockApi(config(false))
    render(<ModelsAndSpend />)

    expect(await screen.findAllByText('$1.25')).toHaveLength(3) // stage, queue, user
    expect(screen.getByText('≥ $12.50')).toBeDefined()
    expect(screen.getAllByText('$12.50')).toHaveLength(2) // queue and user totals
    expect(screen.getByText('low (default)')).toBeDefined()
  })
})
