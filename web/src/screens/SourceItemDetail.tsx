// One source item's life, told as three stages (motet#91).
//
// What the deterministic scrape pulled in, what processing turned it into, and the
// deduped news item it feeds. Three stacked cards, numbered, each with a line saying what
// the stage *is* — because the step between 1 and 2 is where enrichment will go, stage 2
// has to be a visible box even when all it says is "not processed yet". Stage 2 is a list
// of steps; dedup is the only one today.

import { useEffect, useState } from 'react'

import { type ProcessingStep, type SourceItemDetail as Detail, api } from '../api/client'

function stamp(iso: string | null | undefined): string {
  if (!iso) return '—'
  return new Date(iso).toLocaleString([], {
    month: 'short',
    day: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
  })
}

/** What to say when stage 2 has no steps, by the item's overall status. */
const EMPTY_STAGE_COPY: Record<string, string> = {
  held: 'Not processed yet — held. Nothing has been spent on this item; pick it and press Ingest now.',
  dismissed: 'Dismissed. Nothing was spent on this item and it will not be briefed.',
}

const STEP_COPY: Record<string, string> = {
  queued: 'Queued. An integrate job is waiting for a worker.',
  running: 'Running. A worker holds the integrate job now.',
  done: 'Done. Dedup read the extracted text against the window of news items.',
  failed: 'Failed.',
}

const BASIS_COPY: Record<string, string> = {
  first_pass: 'the first pass decided',
  second_look: 'the first pass was unsure; a focused second look decided',
  title_backstop: 'dedup said new, but an unread story already had this title, so it merged',
}

const RELATION_COPY: Record<string, string> = {
  same_event: 'same event',
  related: 'related (unsure)',
  unrelated: 'unrelated',
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

function Step({ step }: { step: ProcessingStep }) {
  const { job, decision } = step
  return (
    <div className="step">
      <p className={`stage-status status-${step.status}`}>
        <strong>{step.step}</strong> · <strong>{step.status}</strong> —{' '}
        {STEP_COPY[step.status] ?? ''}
        {step.status === 'failed' && step.error && <span className="error"> {step.error}</span>}
      </p>
      <dl className="facts">
        {job && (
          <>
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
            {job.last_error && step.status !== 'failed' && (
              <>
                <dt>last error</dt>
                <dd className="error">{job.last_error}</dd>
              </>
            )}
          </>
        )}
        {step.status === 'done' && (
          <>
            <dt>finished</dt>
            <dd title={step.finished_at ?? undefined}>{stamp(step.finished_at)}</dd>
            <dt>outcome</dt>
            <dd>
              {step.outcome === 'new' && 'new story — created the news item below'}
              {step.outcome === 'merged' && 'same story — merged into an existing news item'}
              {step.outcome === null && 'unknown — integrated, but no news item links to it'}
            </dd>
            {decision ? (
              <>
                <dt>relation</dt>
                <dd>
                  {decision.relation
                    ? (RELATION_COPY[decision.relation] ?? decision.relation)
                    : '—'}
                  {decision.candidate_id && (
                    <span className="hint">
                      {' '}
                      to{' '}
                      {decision.candidate_title
                        ? `“${decision.candidate_title}”`
                        : 'an unknown story'}{' '}
                      <span className="mono">({decision.candidate_id})</span>
                    </span>
                  )}
                </dd>
                <dt>why</dt>
                <dd>{decision.reason ?? '—'}</dd>
                <dt>decided by</dt>
                <dd>
                  {BASIS_COPY[decision.basis] ?? decision.basis}
                  {decision.model && <span className="hint mono"> · {decision.model}</span>}
                  <span className="hint" title={decision.decided_at}>
                    {' '}
                    · {stamp(decision.decided_at)}
                  </span>
                </dd>
                <dt>title then</dt>
                <dd>{decision.title ?? '—'}</dd>
                <dt>summary then</dt>
                <dd>{decision.summary ?? '—'}</dd>
              </>
            ) : (
              <>
                <dt>decision</dt>
                <dd className="hint">
                  Not recorded — this item was integrated before dedup’s decisions were kept.
                </dd>
              </>
            )}
          </>
        )}
      </dl>
      {!step.cost_recorded && (
        <p className="hint stage-gaps">
          Not recorded on this item: the inference spend (logged beside the item and metered per
          stage, never stored per item).
        </p>
      )}
    </div>
  )
}

export function SourceItemDetail({
  id,
  onClose,
  onJumpToNewsItem,
}: {
  id: string
  onClose: () => void
  /** Scroll to / highlight a news item in the Processed list, when the list is on screen. */
  onJumpToNewsItem?: ((newsItemId: string) => void) | undefined
}) {
  const [data, setData] = useState<Detail | null>(null)
  const [error, setError] = useState('')
  const [showText, setShowText] = useState(false)

  useEffect(() => {
    let cancelled = false
    setData(null)
    setError('')
    api
      .sourceItem(id)
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
        <button type="button" className="linkish hint" onClick={onClose}>
          close
        </button>
      </div>
    )
  }
  if (!data) return <div className="lifecycle hint">Loading…</div>

  const { pulled, processed } = data

  return (
    <div className="lifecycle">
      <div className="row lifecycle-head">
        <strong>{data.title || '(untitled)'}</strong>
        <span className="hint">{data.id}</span>
        <button type="button" className="linkish hint" onClick={onClose}>
          close
        </button>
      </div>

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
          <dd title={pulled.received_at}>
            {stamp(pulled.received_at)}
            <span className="hint" title={pulled.stored_at}>
              {' '}
              · stored {stamp(pulled.stored_at)}
            </span>
          </dd>
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
        what="Inference: dedup reads the extracted text against the news-item window and decides whether it is a new story or part of one. Enrichment steps will sit here too."
      >
        {processed.length === 0 ? (
          <p className={`stage-status status-${data.status}`}>
            <strong>{data.status}</strong> — {EMPTY_STAGE_COPY[data.status] ?? ''}
          </p>
        ) : (
          processed.map((step) => <Step key={step.step} step={step} />)
        )}
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
                    show in Processed list
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
