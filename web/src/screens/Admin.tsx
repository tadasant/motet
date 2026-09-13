// PROTOTYPE — the /admin view. Cross-user queue and pipeline visibility for an operator.
//
// Rendered when the page is loaded at /admin (see App.tsx), the same "one path, no router"
// trick oauth.ts uses. No RBAC yet: it talks to GET /v1/admin/overview with whatever
// credential the SPA already holds.

import { useCallback, useEffect, useState } from 'react'

import { apiBaseUrl, getToken } from '../api/client'
import type { components } from '../api/schema.gen'

// Hand-typed against the contract; swap for the generated schema type once the route is
// in schema.gen.ts.
type Counts = Record<string, number>
type AdminUser = {
  user_id: string
  email: string | null
  source_items: Counts
  news_items: Counts
  episodes: Counts
  jobs: Counts
}
type AdminQueue = {
  queue: string
  ready: number
  running: number
  done: number
  failed: number
  oldest_ready_age_s: number | null
  last_heartbeat_at: string | null
}
type AdminJob = {
  id: number
  queue: string
  state: string
  attempts: number
  user_id: string | null
  subject: string | null
  last_error: string | null
  run_at: string
  created_at: string
  updated_at: string
  locked_at: string | null
}
type Costs = components['schemas']['AdminCostsResponse']
type Spend = components['schemas']['LlmSpend']
type LlmConfig = components['schemas']['LlmConfigResponse']
type LlmStageConfig = components['schemas']['LlmStageConfigResponse']
type LlmModel = components['schemas']['LlmModelOption']
type Overview = {
  generated_at: string
  users: AdminUser[]
  queues: AdminQueue[]
  jobs: AdminJob[]
  costs: Costs
}

const POLL_MS = 3_000
const SOURCE_STATES = ['pending', 'integrated', 'failed']
const NEWS_STATES = ['unread', 'read']
const EPISODE_STATES = ['pending', 'scripting', 'rendering', 'ready', 'failed']
const JOB_STATES = ['ready', 'running', 'done', 'failed']

async function fetchOverview(userId: string | null): Promise<Overview> {
  const query = userId ? `?user_id=${encodeURIComponent(userId)}` : ''
  const token = getToken()
  const response = await fetch(`${apiBaseUrl()}/v1/admin/overview${query}`, {
    headers: token ? { Authorization: `Bearer ${token}` } : {},
  })
  if (!response.ok) throw new Error(`GET /v1/admin/overview → ${response.status}`)
  return (await response.json()) as Overview
}

async function fetchLlmConfig(): Promise<LlmConfig> {
  const token = getToken()
  const response = await fetch(`${apiBaseUrl()}/v1/admin/llm-config`, {
    headers: token ? { Authorization: `Bearer ${token}` } : {},
  })
  if (!response.ok) throw new Error(`GET /v1/admin/llm-config → ${response.status}`)
  return (await response.json()) as LlmConfig
}

// `null` clears an override; a key left out is untouched (see LlmStageConfigUpdate).
async function putLlmConfig(
  stage: string,
  body: { model?: string | null; effort?: string | null },
): Promise<LlmConfig> {
  const token = getToken()
  const response = await fetch(`${apiBaseUrl()}/v1/admin/llm-config/${encodeURIComponent(stage)}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json', ...(token ? { Authorization: `Bearer ${token}` } : {}) },
    body: JSON.stringify(body),
  })
  if (!response.ok) {
    let detail = `${response.status}`
    try {
      const parsed = (await response.json()) as { detail?: unknown }
      if (typeof parsed.detail === 'string') detail = parsed.detail
    } catch {
      // keep the status
    }
    throw new Error(detail)
  }
  return (await response.json()) as LlmConfig
}

function usd(value: number | undefined): string {
  if (value === undefined) return '—'
  if (value === 0) return '$0'
  return value < 0.01 ? `$${value.toFixed(4)}` : `$${value.toFixed(2)}`
}

function tokens(value: number | undefined): string {
  if (value === undefined || value === 0) return '0'
  if (value < 10_000) return String(value)
  if (value < 1_000_000) return `${(value / 1000).toFixed(1)}k`
  return `${(value / 1_000_000).toFixed(2)}M`
}

const SOURCE_LABEL: Record<string, string> = {
  settings: 'settings',
  stage_env: 'stage env',
  global_env: 'global env',
  default: 'default',
}

// The precedence chain for one axis, winner first-class, the rest muted. Each rung is
// `label=value`, or `label=—` where nothing is set on that rung.
function Chain({
  cfg,
  axis,
}: {
  cfg: LlmStageConfig
  axis: 'model' | 'effort'
}) {
  const winner = axis === 'model' ? cfg.model_source : cfg.effort_source
  const rungs: Array<[string, string | null]> =
    axis === 'model'
      ? [
          ['settings', cfg.setting_model],
          ['stage_env', cfg.stage_env_model],
          ['global_env', cfg.global_env_model],
          ['default', cfg.default_model],
        ]
      : [
          ['settings', cfg.setting_effort],
          ['stage_env', cfg.stage_env_effort],
          ['global_env', cfg.global_env_effort],
          ['default', cfg.default_effort],
        ]
  return (
    <div className="chain">
      {rungs.map(([source, value]) => (
        <span key={source} className={source === winner ? 'won' : 'lost'} title={`${axis} from ${source}`}>
          {SOURCE_LABEL[source]}={value ? value.replace(/^anthropic\//, '') : '—'}
        </span>
      ))}
    </div>
  )
}

function ago(iso: string | null): string {
  if (!iso) return '—'
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000)
  if (s < 60) return `${Math.round(s)}s ago`
  if (s < 3600) return `${Math.round(s / 60)}m ago`
  if (s < 86400) return `${(s / 3600).toFixed(1)}h ago`
  return `${(s / 86400).toFixed(1)}d ago`
}

function seconds(value: number | null): string {
  if (value === null) return '—'
  if (value < 60) return `${Math.round(value)}s`
  if (value < 3600) return `${Math.round(value / 60)}m`
  return `${(value / 3600).toFixed(1)}h`
}

function Cell({ n, state }: { n: number | undefined; state: string }) {
  const value = n ?? 0
  const cls = value === 0 ? 'zero' : state === 'failed' ? 'bad' : state === 'running' ? 'live' : ''
  return <td className={`num ${cls}`}>{value}</td>
}

export function Admin() {
  const [data, setData] = useState<Overview | null>(null)
  const [error, setError] = useState('')
  const [selectedUser, setSelectedUser] = useState<string | null>(null)
  const [queueFilter, setQueueFilter] = useState<string | null>(null)
  const [stateFilter, setStateFilter] = useState<string | null>(null)
  const [paused, setPaused] = useState(false)
  const [llm, setLlm] = useState<LlmConfig | null>(null)
  const [llmError, setLlmError] = useState('')
  const [saving, setSaving] = useState<string | null>(null)

  const loadLlm = useCallback(() => {
    fetchLlmConfig()
      .then((next) => {
        setLlm(next)
        setLlmError('')
      })
      .catch((err: unknown) => setLlmError(err instanceof Error ? err.message : String(err)))
  }, [])

  useEffect(loadLlm, [loadLlm])

  const update = (stage: string, body: { model?: string | null; effort?: string | null }) => {
    setSaving(stage)
    putLlmConfig(stage, body)
      .then((next) => {
        setLlm(next)
        setLlmError('')
      })
      .catch((err: unknown) => setLlmError(err instanceof Error ? err.message : String(err)))
      .finally(() => setSaving(null))
  }

  const refresh = useCallback(() => {
    fetchOverview(selectedUser)
      .then((next) => {
        setData(next)
        setError('')
      })
      .catch((err: unknown) => setError(err instanceof Error ? err.message : String(err)))
  }, [selectedUser])

  useEffect(() => {
    refresh()
    if (paused) return
    const timer = window.setInterval(refresh, POLL_MS)
    return () => window.clearInterval(timer)
  }, [refresh, paused])

  const jobs = (data?.jobs ?? []).filter(
    (job) => (!queueFilter || job.queue === queueFilter) && (!stateFilter || job.state === stateFilter),
  )
  const open = data?.queues.reduce((sum, q) => sum + q.ready + q.running, 0) ?? 0
  const failed = data?.queues.reduce((sum, q) => sum + q.failed, 0) ?? 0

  return (
    <div className="admin">
      <div className="row admin-bar">
        <h2>Admin</h2>
        <span className="hint">
          {open} open · <span className={failed ? 'error' : ''}>{failed} failed</span> · refreshed{' '}
          {data ? ago(data.generated_at) : '—'}
        </span>
        <button type="button" onClick={() => setPaused((p) => !p)}>
          {paused ? 'Resume polling' : 'Pause polling'}
        </button>
        <button type="button" onClick={refresh}>
          Refresh now
        </button>
        <a href="/" className="hint">
          ← app
        </a>
      </div>
      {error && <p className="error">{error}</p>}

      <div className="row">
        <h3>Models &amp; spend</h3>
        <span className="hint">
          {data?.costs.since
            ? `spend since ${new Date(data.costs.since).toLocaleString()} (nothing backfilled)`
            : 'no completions recorded yet — the ledger starts empty, nothing is backfilled'}
          {llm && (
            <>
              {' · '}
              {llm.applies === 'next_job'
                ? 'a change applies to the worker\u2019s next job'
                : 'worker restart needed after a change'}
              {' · precedence: '}
              {llm.precedence.map((p) => SOURCE_LABEL[p] ?? p).join(' > ')}
            </>
          )}
        </span>
      </div>
      {llmError && <p className="error">{llmError}</p>}
      <table className="grid models">
        <thead>
          <tr>
            <th>stage</th>
            <th>model</th>
            <th>effort</th>
            <th className="num">compl.</th>
            <th className="num">in</th>
            <th className="num">out</th>
            <th className="num">reason.</th>
            <th className="num">cache rd</th>
            <th className="num">cache wr</th>
            <th className="num">spend</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          {llm?.stages.map((cfg) => {
            const spec: LlmModel | undefined = llm.models.find((m) => m.slug === cfg.model)
            const efforts = spec?.efforts ?? []
            const spend: Spend | undefined = data?.costs.stages[cfg.stage]
            const overridden = cfg.setting_model !== null || cfg.setting_effort !== null
            const busy = saving === cfg.stage
            return (
              <tr key={cfg.stage} className={overridden ? 'overridden' : ''}>
                <td>
                  <strong>{cfg.stage}</strong>
                </td>
                <td>
                  <select
                    value={cfg.model}
                    disabled={busy}
                    aria-label={`${cfg.stage} model`}
                    onChange={(e) => {
                      const slug = e.target.value
                      const next = llm.models.find((m) => m.slug === slug)
                      // A slug with no selectable effort only pairs with `off`; send both so
                      // the server does not have to refuse the pairing.
                      const body: { model: string; effort?: string } = { model: slug }
                      if (next && next.efforts.length === 0 && cfg.effort !== 'off') body.effort = 'off'
                      update(cfg.stage, body)
                    }}
                  >
                    {llm.models.map((m) => (
                      <option key={m.slug} value={m.slug}>
                        {m.slug}
                      </option>
                    ))}
                  </select>
                  <Chain cfg={cfg} axis="model" />
                </td>
                <td>
                  <select
                    value={cfg.effort}
                    disabled={busy}
                    aria-label={`${cfg.stage} effort`}
                    onChange={(e) => update(cfg.stage, { effort: e.target.value })}
                  >
                    {efforts.map((eff) => (
                      <option key={eff} value={eff}>
                        {eff}
                      </option>
                    ))}
                    <option value="off">off</option>
                  </select>
                  <Chain cfg={cfg} axis="effort" />
                </td>
                <td className={`num ${spend?.completions ? '' : 'zero'}`}>{spend?.completions ?? 0}</td>
                <td className={`num ${spend?.input_tokens ? '' : 'zero'}`}>{tokens(spend?.input_tokens)}</td>
                <td className={`num ${spend?.output_tokens ? '' : 'zero'}`}>{tokens(spend?.output_tokens)}</td>
                <td className={`num ${spend?.reasoning_tokens ? '' : 'zero'}`}>
                  {tokens(spend?.reasoning_tokens)}
                </td>
                <td className={`num ${spend?.cache_read_tokens ? '' : 'zero'}`}>
                  {tokens(spend?.cache_read_tokens)}
                </td>
                <td className={`num ${spend?.cache_write_tokens ? '' : 'zero'}`}>
                  {tokens(spend?.cache_write_tokens)}
                </td>
                <td className={`num spend ${spend?.usd ? '' : 'zero'}`}>{usd(spend?.usd)}</td>
                <td>
                  {overridden && (
                    <button
                      type="button"
                      className="linkish"
                      disabled={busy}
                      onClick={() => update(cfg.stage, { model: null, effort: null })}
                    >
                      reset to default
                    </button>
                  )}
                </td>
              </tr>
            )
          })}
          {!llm && !llmError && (
            <tr>
              <td colSpan={11} className="hint">
                loading model configuration…
              </td>
            </tr>
          )}
        </tbody>
      </table>

      <h3>Queues</h3>
      <table className="grid">
        <thead>
          <tr>
            <th>queue</th>
            {JOB_STATES.map((s) => (
              <th key={s} className="num">
                {s}
              </th>
            ))}
            <th>oldest ready</th>
            <th>worker heartbeat</th>
            <th className="num">spend</th>
          </tr>
        </thead>
        <tbody>
          {data?.queues.map((q) => (
            <tr
              key={q.queue}
              className={queueFilter === q.queue ? 'selected' : ''}
              onClick={() => setQueueFilter(queueFilter === q.queue ? null : q.queue)}
            >
              <td>{q.queue}</td>
              {JOB_STATES.map((s) => (
                <Cell key={s} n={q[s as keyof AdminQueue] as number} state={s} />
              ))}
              <td className={q.oldest_ready_age_s !== null && q.oldest_ready_age_s > 120 ? 'bad' : ''}>
                {seconds(q.oldest_ready_age_s)}
              </td>
              <td
                className={
                  !q.last_heartbeat_at || Date.now() - new Date(q.last_heartbeat_at).getTime() > 60_000
                    ? 'bad'
                    : 'ok'
                }
              >
                {ago(q.last_heartbeat_at)}
              </td>
              <td
                className={`num spend ${data.costs.queues[q.queue]?.usd ? '' : 'zero'}`}
                title={
                  data.costs.queues[q.queue]
                    ? `${data.costs.queues[q.queue]?.completions ?? 0} completions`
                    : 'no LLM stage on this queue'
                }
              >
                {usd(data.costs.queues[q.queue]?.usd)}
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      <h3>Users</h3>
      <table className="grid">
        <thead>
          <tr>
            <th rowSpan={2}>user</th>
            <th colSpan={SOURCE_STATES.length}>source items</th>
            <th colSpan={NEWS_STATES.length}>news items</th>
            <th colSpan={EPISODE_STATES.length}>episodes</th>
            <th colSpan={JOB_STATES.length}>jobs</th>
            <th rowSpan={2} className="num">
              spend
            </th>
          </tr>
          <tr>
            {[...SOURCE_STATES, ...NEWS_STATES, ...EPISODE_STATES, ...JOB_STATES].map((s, i) => (
              <th key={`${s}-${i}`} className="num sub">
                {s}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {data?.users.map((u) => (
            <tr
              key={u.user_id}
              className={selectedUser === u.user_id ? 'selected' : ''}
              onClick={() => setSelectedUser(selectedUser === u.user_id ? null : u.user_id)}
            >
              <td>
                <strong>{u.user_id}</strong>
                {u.email && <span className="hint"> {u.email}</span>}
              </td>
              {SOURCE_STATES.map((s) => (
                <Cell key={s} n={u.source_items[s]} state={s} />
              ))}
              {NEWS_STATES.map((s) => (
                <Cell key={s} n={u.news_items[s]} state={s} />
              ))}
              {EPISODE_STATES.map((s) => (
                <Cell key={s} n={u.episodes[s]} state={s} />
              ))}
              {JOB_STATES.map((s) => (
                <Cell key={s} n={u.jobs[s]} state={s} />
              ))}
              <td
                className={`num spend ${data.costs.users[u.user_id]?.usd ? '' : 'zero'}`}
                title={`${data.costs.users[u.user_id]?.completions ?? 0} completions`}
              >
                {usd(data.costs.users[u.user_id]?.usd)}
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      <div className="row">
        <h3>
          Jobs{selectedUser && <> · {selectedUser}</>}
          {queueFilter && <> · {queueFilter}</>}
        </h3>
        <span className="hint">{jobs.length} shown</span>
        <select value={stateFilter ?? ''} onChange={(e) => setStateFilter(e.target.value || null)}>
          <option value="">all states</option>
          {JOB_STATES.map((s) => (
            <option key={s} value={s}>
              {s}
            </option>
          ))}
        </select>
        {(selectedUser || queueFilter || stateFilter) && (
          <button
            type="button"
            className="linkish"
            onClick={() => {
              setSelectedUser(null)
              setQueueFilter(null)
              setStateFilter(null)
            }}
          >
            clear filters
          </button>
        )}
      </div>
      <table className="grid jobs">
        <thead>
          <tr>
            <th>id</th>
            <th>queue</th>
            <th>state</th>
            <th className="num">att.</th>
            <th>user</th>
            <th>subject</th>
            <th>created</th>
            <th>runs / ran</th>
            <th>error</th>
          </tr>
        </thead>
        <tbody>
          {jobs.map((job) => (
            <tr key={job.id} className={job.state}>
              <td className="num">{job.id}</td>
              <td>{job.queue}</td>
              <td>
                <span className={`badge ${job.state}`}>{job.state}</span>
              </td>
              <td className="num">{job.attempts}</td>
              <td>
                {job.user_id ? (
                  <button type="button" className="linkish" onClick={() => setSelectedUser(job.user_id)}>
                    {job.user_id}
                  </button>
                ) : (
                  '—'
                )}
              </td>
              <td className="mono">{job.subject ?? '—'}</td>
              <td title={job.created_at}>{ago(job.created_at)}</td>
              <td title={job.state === 'ready' ? `run_at ${job.run_at}` : `updated_at ${job.updated_at}`}>
                {job.state === 'ready'
                  ? new Date(job.run_at).getTime() > Date.now()
                    ? `in ${seconds((new Date(job.run_at).getTime() - Date.now()) / 1000)}`
                    : 'due'
                  : job.state === 'running'
                    ? `since ${ago(job.locked_at)}`
                    : ago(job.updated_at)}
              </td>
              <td className="err">{job.last_error ?? ''}</td>
            </tr>
          ))}
          {data && jobs.length === 0 && (
            <tr>
              <td colSpan={9} className="hint">
                no jobs match
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </div>
  )
}
