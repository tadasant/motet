// Label sync for one mailbox (motet#96): the label a message leaves and the label it
// joins when you ingest it — the Gmail half of reading it.
//
// Its own component rather than more of `Sources.tsx`, so that the Sources screen can be
// reworked around it (motet#90) without this having to move: it takes one source and
// hands back the updated one, and knows nothing else about the page it is on.
//
// Three states, and the copy is the design. **Off** says Motet never changes the mailbox,
// because that is the promise a read-only connect made. **Needs re-authorization** is the
// one that asks for anything: the mailbox was connected read-only, labels are set, and
// the only way on is a second consent the owner starts from here. **On** says exactly
// which move happens, and what the last attempts did.

import { useEffect, useState } from 'react'

import { ApiError, type LabelSync as LabelSyncState, type Source, api } from '../api/client'
import { beginConsent, redirectUri, rememberState } from '../oauth'

type Status = { kind: 'idle' } | { kind: 'busy' } | { kind: 'error'; message: string }

const describeMove = (sync: LabelSyncState): string => {
  if (sync.remove_label && sync.add_label)
    return `moves its message from ${sync.remove_label} to ${sync.add_label}`
  if (sync.remove_label) return `takes ${sync.remove_label} off its message`
  return `adds ${sync.add_label ?? ''} to its message`
}

export function LabelSync({
  source,
  onChange,
  /** Overridden only by tests: jsdom cannot navigate. */
  navigate = beginConsent,
}: {
  source: Source
  onChange: (next: Source) => void
  navigate?: (url: string) => void
}) {
  const sync = source.label_sync
  const [remove, setRemove] = useState(sync?.remove_label ?? '')
  const [add, setAdd] = useState(sync?.add_label ?? '')
  const [status, setStatus] = useState<Status>({ kind: 'idle' })

  // Re-seeded when the server's values change underneath — another tab, a reload of the
  // list — so the inputs never offer to save back a setting that is no longer the stored one.
  const storedRemove = sync?.remove_label ?? ''
  const storedAdd = sync?.add_label ?? ''
  useEffect(() => {
    setRemove(storedRemove)
    setAdd(storedAdd)
  }, [storedRemove, storedAdd])

  if (!sync) return null
  const listId = `labels-${source.id}`
  const changed = remove.trim() !== (sync.remove_label ?? '') || add.trim() !== (sync.add_label ?? '')

  const save = async (event: React.FormEvent) => {
    event.preventDefault()
    setStatus({ kind: 'busy' })
    try {
      const next = await api.setLabelSync(source.id, remove.trim(), add.trim())
      setRemove(next.label_sync?.remove_label ?? '')
      setAdd(next.label_sync?.add_label ?? '')
      setStatus({ kind: 'idle' })
      onChange(next)
    } catch (err) {
      setStatus({ kind: 'error', message: err instanceof ApiError ? err.message : String(err) })
    }
  }

  const reauthorize = async () => {
    setStatus({ kind: 'busy' })
    try {
      const consent = await api.reauthorizeSource(source.id, redirectUri())
      // Before the redirect, for the same reason the first connect does it: once
      // `navigate` runs, nothing else in this tab executes.
      rememberState(consent.state)
      navigate(consent.authorization_url)
    } catch (err) {
      setStatus({ kind: 'error', message: err instanceof ApiError ? err.message : String(err) })
    }
  }

  return (
    <section className="label-sync" aria-label={`Label sync for ${source.name}`}>
      <div className="item-head">
        <strong>Label sync</strong>
        <span className="badge">
          {sync.status === 'on' ? 'on' : sync.status === 'off' ? 'off' : 'needs re-authorization'}
        </span>
      </div>

      {sync.status === 'off' && (
        <p className="hint">
          Off. Motet only reads this mailbox. Choose labels below to have ingesting an item
          move its message too, the way you would after reading it.
        </p>
      )}
      {sync.status === 'on' && (
        <p className="hint">
          On. Ingesting an item from this mailbox {describeMove(sync)}. Nothing else — a
          poll, or an item you have not ingested — touches your mail.
        </p>
      )}
      {sync.status === 'needs_reauthorization' && (
        <div className="stalled" role="status">
          <p>
            <strong>Needs re-authorization to enable label sync.</strong> This mailbox was
            connected read-only, so Motet cannot change its labels yet and will not try.
            Google&rsquo;s consent screen will ask to let Motet <em>view and modify</em>{' '}
            your email — the narrowest permission Gmail has that can move a message between
            labels. Motet uses it for that one move, when you ingest, and for nothing else.
          </p>
          <button type="button" onClick={reauthorize} disabled={status.kind === 'busy'}>
            {status.kind === 'busy' ? 'Redirecting…' : 'Re-authorize Gmail'}
          </button>
        </div>
      )}

      <form onSubmit={save}>
        <label htmlFor={`${listId}-remove`}>When I ingest an item, remove this label</label>
        <input
          id={`${listId}-remove`}
          list={listId}
          value={remove}
          maxLength={225}
          placeholder="e.g. Newsletters — or leave empty"
          onChange={(e) => setRemove(e.target.value)}
        />
        <label htmlFor={`${listId}-add`}>…and add this label</label>
        <input
          id={`${listId}-add`}
          list={listId}
          value={add}
          maxLength={225}
          placeholder="e.g. Completed — or leave empty"
          onChange={(e) => setAdd(e.target.value)}
        />
        <datalist id={listId}>
          {sync.available_labels.map((name) => (
            <option key={name} value={name} />
          ))}
        </datalist>
        <p className="hint">
          {sync.available_labels.length > 0
            ? `Suggestions are this mailbox's labels as last read from it${
                sync.labels_read_at
                  ? ` (${new Date(sync.labels_read_at).toLocaleString()})`
                  : ''
              }. `
            : 'No labels have been read from this mailbox yet — the next sync reads them. '}
          Leave both empty to turn label sync off. Motet will not move mail into Trash or
          Spam.
        </p>
        <button type="submit" disabled={status.kind === 'busy' || !changed}>
          Save labels
        </button>
      </form>

      {sync.status !== 'off' && (sync.last_synced_at || sync.failed_items > 0) && (
        <p className="hint">
          {sync.last_synced_at &&
            `Last moved a message ${new Date(sync.last_synced_at).toLocaleString()}. `}
          {sync.failed_items > 0 &&
            `${sync.failed_items} ingested item${sync.failed_items === 1 ? '' : 's'} could not be moved.`}
        </p>
      )}
      {sync.status !== 'off' && sync.failed_items > 0 && sync.last_error && (
        <p className="reason">{sync.last_error}</p>
      )}
      {status.kind === 'error' && (
        <p className="error" role="alert">
          {status.message}
        </p>
      )}
    </section>
  )
}
