// PROTOTYPE — one source item's life, told as three stages.
//
// The owner's ask: make it clear (1) what the deterministic scrape pulled in, (2) what
// processing turned that into, and (3) the deduped news item it feeds. Three stacked
// cards, numbered, each with a line saying what the stage *is* — because the step between
// 1 and 2 is going to grow agentic enrichment (following links, pulling structured data),
// and stage 2 has to be a visible box today even when all it says is "not processed yet".

import { useEffect, useState } from 'react'

import { apiBaseUrl, getToken } from '../api/client'
import type { components } from '../api/schema.gen'

export type SourceItemDetailData = components['schemas']['SourceItemDetailResponse']

async function fetchDetail(id: string): Promise<SourceItemDetailData> {
  const token = getToken()
  const response = await fetch(`${apiBaseUrl()}/v1/source-items/${encodeURIComponent(id)}`, {
    headers: token ? { Authorization: `Bearer ${token}` } : {},
  })
  if (!response.ok) throw new Error(`GET /v1/source-items/${id} → ${response.status}`)
  return (await response.json()) as SourceItemDetailData
}

function stamp(iso: string | null | undefined): string {
  if (!iso) return '—'
  return new Date(iso).toLocaleString([], {
    month: 'short',
    day: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
  })
}

const STATUS_COPY: Record<string, string> = {
  held: 'Not processed yet — waiting for you. Nothing has been spent on this item; select it under Waiting for you and press Ingest.',
  queued: 'Queued. An integrate job is waiting for a worker.',
  running: 'Running. A worker holds the integrate job now.',
  done: 'Done. Dedup read the extracted text against the current window of news items and wrote the result below.',
  failed: 'Failed.',
}

function Stage({
  number,
  name,
  what,
  children,
}: {
  number: number
  name: string
  what: string
  children: React.ReactNode
}) {
  return (
    <section className="stage" aria-label={`Stage ${number}: ${name}`}>
      <h4>
        <span className="stage-number">{number}</span> {name}
      </h4>
      <p className="hint stage-what">{what}</p>
      {children}
    </section>
  )
}

export function SourceItemDetail({
  id,
  onClose,
  onJumpToNewsItem,
}: {
  id: string
  onClose: () => void
  /** Scroll to / highlight a news item in the backlog, when the list is on screen. */
  onJumpToNewsItem?: ((newsItemId: string) => void) | undefined
}) {
  const [data, setData] = useState<SourceItemDetailData | null>(null)
  const [error, setError] = useState('')
  const [showText, setShowText] = useState(false)

  useEffect(() => {
    let cancelled = false
    setData(null)
    setError('')
    fetchDetail(id)
      .then((next) => {
        if (!cancelled) setData(next)
      })
      .catch((err: unknown) => {
        if (!cancelled) setError(err instanceof Error ? err.message : String(err))
      })
    return () => {
      cancelled = true
    }
  }, [id])

  if (error) {
    return (
      <div className="lifecycle">
        <p className="error" role="alert">
          {error}
        </p>
        <button type="button" className="linkish hint close" onClick={onClose} aria-label="Close">
          close
        </button>
      </div>
    )
  }
  if (!data) return <div className="lifecycle hint">Loading…</div>

  const { pulled, processed } = data
  const job = processed.job

  return (
    <div className="lifecycle">
      <div className="row lifecycle-head">
        <strong>{data.title || '(untitled)'}</strong>
        <button type="button" className="linkish hint close" onClick={onClose} aria-label="Close">
          close
        </button>
      </div>
      <p className="hint mono lifecycle-id">{data.id}</p>

      <Stage
        number={1}
        name="Pulled in"
        what="The deterministic scrape: polled, fetched and extracted with no model involved. This is exactly what the source handed over."
      >
        <dl className="facts">
          <dt>source</dt>
          <dd>
            {pulled.source_name} <span className="hint">({pulled.source_kind})</span>
          </dd>
          <dt>message id</dt>
          <dd className="mono">{pulled.external_id ?? '— (pasted)'}</dd>
          <dt>received</dt>
          <dd title={pulled.received_at}>{stamp(pulled.received_at)}</dd>
          <dt>size</dt>
          <dd>
            {pulled.chars.toLocaleString()} chars of extracted text
            {!pulled.raw_stored && (
              <span className="hint"> · raw message bytes are not stored</span>
            )}
          </dd>
        </dl>
        <button type="button" className="linkish hint" onClick={() => setShowText((s) => !s)}>
          {showText ? 'hide extracted text' : 'show extracted text'}
        </button>
        {showText && <pre className="extracted">{pulled.text}</pre>}
      </Stage>

      <Stage
        number={2}
        name="Processed"
        what="Inference: dedup reads the extracted text against the news-item window and decides whether it is a new story or part of one. Enrichment steps (following links, pulling structured data) will sit here too."
      >
        <p className={`stage-status status-${processed.status}`}>
          <strong>{processed.status}</strong> — {STATUS_COPY[processed.status] ?? ''}
          {processed.status === 'failed' && processed.error && (
            <span className="error"> {processed.error}</span>
          )}
        </p>
        {job && (
          <dl className="facts">
            <dt>integrate job</dt>
            <dd>
              #{job.id} · {job.state} · attempt {job.attempts} of {job.max_attempts}
              {job.work_committed && <span className="hint"> · work committed</span>}
            </dd>
            <dt>queued</dt>
            <dd title={job.created_at}>{stamp(job.created_at)}</dd>
            {job.state === 'ready' && (
              <>
                <dt>next attempt</dt>
                <dd title={job.run_at}>{stamp(job.run_at)}</dd>
              </>
            )}
            {job.state === 'running' && (
              <>
                <dt>lease touched</dt>
                <dd title={job.locked_at ?? undefined}>{stamp(job.locked_at)}</dd>
              </>
            )}
            {job.last_error && processed.status !== 'failed' && (
              <>
                <dt>last error</dt>
                <dd className="error">{job.last_error}</dd>
              </>
            )}
          </dl>
        )}
        {processed.status === 'done' && (
          <dl className="facts">
            <dt>integrated</dt>
            <dd title={processed.integrated_at ?? undefined}>{stamp(processed.integrated_at)}</dd>
            <dt>decision</dt>
            <dd>
              {processed.outcome === 'new' && 'new story — created the news item below'}
              {processed.outcome === 'merged' && 'same story — merged into an existing news item'}
              {processed.outcome === null && 'unknown — integrated, but no news item links to it'}
            </dd>
            <dt>title written</dt>
            <dd>{processed.title ?? '—'}</dd>
            <dt>summary written</dt>
            <dd>{processed.summary ?? '—'}</dd>
          </dl>
        )}
        <p className="hint stage-gaps">
          Not recorded on this item:
          {!processed.decision_recorded &&
            ' dedup’s relation / reason / closest candidate (logged only);'}
          {!processed.cost_recorded &&
            ' the inference spend (logged and metered per stage, never per item).'}
        </p>
      </Stage>

      <Stage
        number={3}
        name="News item"
        what="The deduped story in the backlog that this source item backs, alongside any other sources that told the same story."
      >
        {data.news_items.length === 0 ? (
          <p className="hint">None yet. A news item appears here once stage 2 completes.</p>
        ) : (
          <ul className="linked-news">
            {data.news_items.map((item) => (
              <li key={item.id}>
                <strong>{item.title}</strong>{' '}
                <span className="hint">
                  · {item.read ? 'read' : 'unread'} · {item.source_count} source
                  {item.source_count === 1 ? '' : 's'}
                  {item.source_count > 1 &&
                    ` (this one is #${item.position + 1}${item.position === 0 ? ', created it' : ', merged in'})`}
                </span>
                <p>{item.summary}</p>
                {onJumpToNewsItem ? (
                  <button
                    type="button"
                    className="linkish"
                    onClick={() => onJumpToNewsItem(item.id)}
                  >
                    Show in backlog
                  </button>
                ) : (
                  <span className="hint mono">{item.id}</span>
                )}
              </li>
            ))}
          </ul>
        )}
      </Stage>
    </div>
  )
}
