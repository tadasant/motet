// "Models & spend": which model each LLM stage is on and why, and what each stage, user
// and queue has spent (motet#92). A block on the admin screen, in its own file.
//
// The dropdowns are only live where the deployment honours runtime settings; in production
// the API reports `writable: false`, the block shows the environment's answer read-only,
// and a PUT would be refused with 409 whatever this renders. Spend is loaded on mount and
// on demand rather than polled: it is a sum over the ledger, not queue state.

import { useCallback, useEffect, useState } from 'react'

import {
  type LlmConfig,
  type LlmSpend,
  type LlmSpendReport,
  type LlmStageConfig,
  type LlmStageUpdate,
  api,
} from '../api/client'

const SOURCE_LABEL: Record<string, string> = {
  settings: 'settings',
  stage_env: 'stage env',
  global_env: 'global env',
  default: 'default',
}

function usd(spend: LlmSpend | undefined): string {
  const value = spend?.usd ?? 0
  const text = value === 0 ? '$0' : value < 0.01 ? `$${value.toFixed(4)}` : `$${value.toFixed(2)}`
  // Completions on an unpriced model are left out of `usd`; say the figure is a floor.
  return spend && spend.unpriced_completions > 0 ? `≥ ${text}` : text
}

function tokens(value: number | undefined): string {
  if (!value) return '0'
  if (value < 10_000) return String(value)
  if (value < 1_000_000) return `${(value / 1000).toFixed(1)}k`
  return `${(value / 1_000_000).toFixed(2)}M`
}

function short(slug: string): string {
  return slug.replace(/^anthropic\//, '')
}

// One axis of the precedence chain: every rung, the one that won emphasised.
function Chain({ cfg, axis }: { cfg: LlmStageConfig; axis: 'model' | 'effort' }) {
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
          {SOURCE_LABEL[source]}={value ? short(value) : '—'}
        </span>
      ))}
    </div>
  )
}

function Money({ spend }: { spend: LlmSpend | undefined }) {
  return (
    <td
      className={`num spend ${spend?.usd || spend?.unpriced_completions ? '' : 'zero'}`}
      title={
        spend
          ? `${spend.completions} completions · in ${tokens(spend.input_tokens)} · out ${tokens(spend.output_tokens)}` +
            ` · cache read ${tokens(spend.cache_read_tokens)} · cache write ${tokens(spend.cache_write_tokens)}` +
            (spend.unpriced_completions ? ` · ${spend.unpriced_completions} on an unpriced model` : '')
          : undefined
      }
    >
      {usd(spend)}
    </td>
  )
}

export function ModelsAndSpend() {
  const [config, setConfig] = useState<LlmConfig | null>(null)
  const [spend, setSpend] = useState<LlmSpendReport | null>(null)
  const [error, setError] = useState('')
  const [saving, setSaving] = useState<string | null>(null)

  const load = useCallback(() => {
    api
      .llmConfig()
      .then((next) => {
        setConfig(next)
        setError('')
      })
      .catch((err: unknown) => setError(err instanceof Error ? err.message : String(err)))
    api
      .llmSpend()
      .then(setSpend)
      .catch((err: unknown) => setError(err instanceof Error ? err.message : String(err)))
  }, [])

  useEffect(load, [load])

  const update = (stage: string, body: LlmStageUpdate) => {
    setSaving(stage)
    api
      .setLlmConfig(stage, body)
      .then((next) => {
        setConfig(next)
        setError('')
      })
      .catch((err: unknown) => setError(err instanceof Error ? err.message : String(err)))
      .finally(() => setSaving(null))
  }

  const writable = config?.writable ?? false
  const days = spend?.window_days ?? 7

  return (
    <section className="models-and-spend">
      <div className="row">
        <h2>Models &amp; spend</h2>
        <span className="hint">
          {config && (
            <>
              precedence {config.precedence.map((p) => SOURCE_LABEL[p] ?? p).join(' > ')}
              {writable ? ' · a change applies to the worker’s next job' : ''}
            </>
          )}
        </span>
        <button type="button" onClick={load}>
          Refresh spend
        </button>
      </div>
      {config && !writable && (
        <p className="hint">
          Read-only on this deployment: <code>{config.writable_env}</code> is off, so the environment is the whole
          model configuration and no settings row is read.
        </p>
      )}
      {config?.settings_error && <p className="error">Stored settings are not being applied: {config.settings_error}</p>}
      {error && <p className="error">{error}</p>}

      <table className="grid models">
        <thead>
          <tr>
            <th>stage</th>
            <th>model</th>
            <th>effort</th>
            <th className="num">compl. {days}d</th>
            <th className="num">spend {days}d</th>
            <th className="num">spend total</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          {config?.stages.map((cfg) => {
            const spec = config.models.find((m) => m.slug === cfg.model)
            const efforts = spec?.efforts ?? []
            const overridden = cfg.setting_model !== null || cfg.setting_effort !== null
            const busy = saving === cfg.stage || !writable
            const week = spend?.window.stages[cfg.stage]
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
                      const next = config.models.find((m) => m.slug === e.target.value)
                      const body: LlmStageUpdate = { model: e.target.value }
                      // A slug with no selectable effort pairs only with `off`; send both so
                      // the server does not have to refuse the pairing.
                      if (next && next.efforts.length === 0 && cfg.effort !== 'off') body.effort = 'off'
                      update(cfg.stage, body)
                    }}
                  >
                    {config.models.map((m) => (
                      <option key={m.slug} value={m.slug}>
                        {short(m.slug)}
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
                    {[...efforts, 'off'].map((effort) => (
                      <option key={effort} value={effort}>
                        {effort === cfg.default_effort ? `${effort} (default)` : effort}
                      </option>
                    ))}
                  </select>
                  <Chain cfg={cfg} axis="effort" />
                </td>
                <td className={`num ${week?.completions ? '' : 'zero'}`}>{week?.completions ?? 0}</td>
                <Money spend={week} />
                <Money spend={spend?.total.stages[cfg.stage]} />
                <td>
                  {overridden && writable && (
                    <button
                      type="button"
                      className="linkish"
                      disabled={saving === cfg.stage}
                      onClick={() => update(cfg.stage, { model: null, effort: null })}
                    >
                      reset
                    </button>
                  )}
                </td>
              </tr>
            )
          })}
          {!config && !error && (
            <tr>
              <td colSpan={7} className="hint">
                loading model configuration…
              </td>
            </tr>
          )}
        </tbody>
      </table>

      {spend && (
        <table className="grid spend-by">
          <thead>
            <tr>
              <th>spent by</th>
              <th className="num">spend {days}d</th>
              <th className="num">spend total</th>
            </tr>
          </thead>
          <tbody>
            {Object.keys(spend.total.queues).map((queue) => (
              <tr key={`q-${queue}`}>
                <td>
                  queue <strong>{queue}</strong>
                </td>
                <Money spend={spend.window.queues[queue]} />
                <Money spend={spend.total.queues[queue]} />
              </tr>
            ))}
            {spend.total.users.map((user) => (
              <tr key={`u-${user.user_id}`}>
                <td>
                  user <strong>{user.email ?? user.user_id}</strong>
                </td>
                <Money spend={spend.window.users.find((u) => u.user_id === user.user_id)?.spend} />
                <Money spend={user.spend} />
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <p className="hint">
        {spend?.since
          ? `Ledger since ${new Date(spend.since).toLocaleString()}; rows older than ${spend.retention_days} days are deleted. `
          : 'No completions recorded yet — the ledger starts empty and nothing is backfilled. '}
        Voice turns are not in it: the voice service has no database, so their spend is a metric only.
      </p>
    </section>
  )
}
