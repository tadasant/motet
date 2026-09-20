// Renaming an episode, in the one place both surfaces that show a title can use it.
//
// An episode is named after the day it was made unless somebody typed something, so most
// of them arrive called `2026-09-20`. This is how one gets a name worth finding again.
//
// It is a single component rather than one control on the shelf and another on the detail
// because the two would otherwise disagree about what a blank title does, what an error
// looks like, and whether Escape cancels — three answers to questions with one right one.
// What differs between the surfaces is only what the title looks like when it is *not*
// being edited, which is the caller's `children`.

import { useRef, useState } from 'react'

import { ApiError, type Episode, api } from '../api/client'

export function EpisodeTitle({
  episode,
  onRenamed,
  children,
}: {
  episode: Episode
  /** The server's answer. The list this episode came from is now stale. */
  onRenamed: (episode: Episode) => void
  /** The title as this surface draws it when nothing is being edited. */
  children: React.ReactNode
}) {
  const [editing, setEditing] = useState(false)
  const [value, setValue] = useState(episode.title)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  // Which attempt is current. Cancelling bumps it, so a request that was already in
  // flight cannot report its failure afterwards: `save` captures the number it started
  // on and writes nothing once it has moved. Without it, Cancel-then-the-request-fails
  // parks a `role="alert"` beside the Rename button — the symptom `cancel` exists to
  // remove, one path along.
  const attempt = useRef(0)

  const open = () => {
    setValue(episode.title)
    setError('')
    setEditing(true)
  }

  // Both ways out of the editor, and they clear the error together: the message belongs to
  // an attempt that is over, and the non-editing branch renders it too — so leaving it set
  // would park a `role="alert"` beside the Rename button until the row unmounted.
  const cancel = () => {
    attempt.current += 1
    setBusy(false)
    setError('')
    setEditing(false)
  }

  const save = async () => {
    const title = value.trim()
    // Nothing to write, and an empty title is not a request to be re-named after the
    // date: that default is applied at creation, and undoing a rename is a rename.
    if (!title || title === episode.title) {
      cancel()
      return
    }
    const mine = attempt.current
    setBusy(true)
    try {
      const renamed = await api.renameEpisode(episode.id, title)
      if (attempt.current !== mine) return
      onRenamed(renamed)
      setEditing(false)
      setError('')
    } catch (err) {
      if (attempt.current !== mine) return
      setError(err instanceof ApiError ? err.message : String(err))
    } finally {
      if (attempt.current === mine) setBusy(false)
    }
  }

  if (!editing) {
    return (
      <>
        {children}
        <button
          type="button"
          className="linkish hint"
          onClick={(event) => {
            // On the shelf the row underneath opens the episode; renaming is not that.
            event.stopPropagation()
            open()
          }}
          aria-label={`Rename ${episode.title}`}
        >
          Rename
        </button>
        {error && (
          <span className="error" role="alert">
            {error}
          </span>
        )}
      </>
    )
  }

  return (
    <form
      className="rename"
      onClick={(event) => event.stopPropagation()}
      onSubmit={(event) => {
        event.preventDefault()
        void save()
      }}
    >
      <label className="visually-hidden" htmlFor={`rename-${episode.id}`}>
        Episode title
      </label>
      <input
        id={`rename-${episode.id}`}
        value={value}
        autoFocus
        maxLength={500}
        onChange={(event) => setValue(event.target.value)}
        onKeyDown={(event) => {
          if (event.key === 'Escape') cancel()
        }}
      />
      <button type="submit" className="primary" disabled={busy}>
        {busy ? 'Saving…' : 'Save'}
      </button>
      <button type="button" className="linkish hint" onClick={cancel}>
        Cancel
      </button>
      {error && (
        <span className="error" role="alert">
          {error}
        </span>
      )}
    </form>
  )
}
