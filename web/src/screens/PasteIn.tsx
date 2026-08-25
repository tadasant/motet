// Screen 1: paste a newsletter in.
//
// Phase 1's only ingestion route. The API stores the text and enqueues the work; nothing
// happens synchronously, so this screen's honest job is to say "queued" rather than to
// pretend a news item exists yet.

import { useState } from 'react'

import { ApiError, api } from '../api/client'

type Status = { kind: 'idle' } | { kind: 'busy' } | { kind: 'done'; id: string } | { kind: 'error'; message: string }

export function PasteIn({ onIngested }: { onIngested: () => void }) {
  const [title, setTitle] = useState('')
  const [text, setText] = useState('')
  const [status, setStatus] = useState<Status>({ kind: 'idle' })

  const submit = async (event: React.FormEvent) => {
    event.preventDefault()
    setStatus({ kind: 'busy' })
    try {
      const item = await api.paste(title.trim(), text)
      setStatus({ kind: 'done', id: item.id })
      setTitle('')
      setText('')
      onIngested()
    } catch (error) {
      setStatus({
        kind: 'error',
        message: error instanceof ApiError ? error.message : String(error),
      })
    }
  }

  return (
    <section aria-labelledby="paste-in-heading">
      <h2 id="paste-in-heading">Paste in</h2>
      <p className="hint">
        Paste a newsletter. A worker deduplicates it against your backlog and turns it into
        a news item — it will not appear here instantly.
      </p>
      <form onSubmit={submit}>
        <label htmlFor="paste-title">Title</label>
        <input
          id="paste-title"
          value={title}
          required
          maxLength={500}
          placeholder="Acme raises $20M Series A"
          onChange={(e) => setTitle(e.target.value)}
        />
        <label htmlFor="paste-text">Text</label>
        <textarea
          id="paste-text"
          value={text}
          required
          rows={14}
          placeholder="Paste the whole newsletter here."
          onChange={(e) => setText(e.target.value)}
        />
        <button type="submit" disabled={status.kind === 'busy' || !title.trim() || !text.trim()}>
          {status.kind === 'busy' ? 'Sending…' : 'Ingest'}
        </button>
      </form>
      {status.kind === 'done' && (
        <p className="ok" role="status">
          Queued as {status.id}. Watch it under Backlog &rarr; Processing: it shows up
          there while it is being worked on, and stays there with a reason if it fails.
        </p>
      )}
      {status.kind === 'error' && (
        <p className="error" role="alert">
          {status.message}
        </p>
      )}
    </section>
  )
}
