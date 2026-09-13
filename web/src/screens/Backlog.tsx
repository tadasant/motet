// Screen 2: the backlog, and the button that turns it into an episode.
//
// One job: decide what gets briefed, and make the briefing. The screen is one column in
// the order the person asks rather than the order the data flows — a sticky summary with
// the counts and the primary action, a one-line status strip about the worker, search and
// sort over everything, then two sections that share one row shape and one selection
// model: **Waiting for you** (held items, the ingest decision) and **Ready to brief**
// (unread news items), with **Read** folded under. proto/design/backlog-crit.md is the
// crit that produced this shape, with the before/after screenshots.
//
// Read state is per news item (invariant 5) and the toggle writes the same column that
// "I listened to this episode" does — so marking something read here and having heard it
// on a walk are one fact, not two that drift.
//
// PROTOTYPE (proto/local-ux): the held list and the lifecycle drawer are the manual ingest
// gate of proto/issues/03.

import { useCallback, useEffect, useMemo, useState } from 'react'

import {
  ApiError,
  type Episode,
  type IngestionItem,
  type NewsItem,
  type ProcessingStatus,
  api,
} from '../api/client'
import { BacklogHeader, DEFAULT_MAX_MINUTES } from './backlog/BacklogHeader'
import { Drawer } from './backlog/Drawer'
import { HeldList } from './backlog/HeldList'
import { NewsList } from './backlog/NewsList'
import { type Selection, SelectionBar } from './backlog/SelectionBar'
import { StatusStrip } from './backlog/StatusStrip'
import { integrateHeld, useHeld } from './backlog/useHeld'

type Sort = 'newest' | 'oldest'

const NO_SELECTION: Selection = { list: 'news', ids: new Set() }

export function Backlog({
  items,
  ingestion,
  ingestionUnavailable,
  processing,
  onChanged,
  onOpenEpisode,
}: {
  items: NewsItem[]
  ingestion: IngestionItem[]
  ingestionUnavailable: boolean
  processing: ProcessingStatus | null
  onChanged: () => void
  onOpenEpisode: (episode: Episode) => void
}) {
  const [minutes, setMinutes] = useState(DEFAULT_MAX_MINUTES)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [query, setQuery] = useState('')
  const [sort, setSort] = useState<Sort>('newest')
  const [selection, setSelection] = useState<Selection>(NO_SELECTION)
  // Which news row is expanded to its summary, and which source item's lifecycle is open
  // in the drawer. The drawer is one for the whole screen, whichever list opened it.
  const [expanded, setExpanded] = useState<string | null>(null)
  const [drawer, setDrawer] = useState<string | null>(null)
  // The news item being pointed at (scrolled to and briefly highlighted) from a lifecycle.
  const [flash, setFlash] = useState<string | null>(null)
  const [readOpen, setReadOpen] = useState(false)

  const { held, error: heldError, refresh: refreshHeld } = useHeld()
  const heldAll = useMemo(() => held ?? [], [held])
  const heldIds = useMemo(() => new Set(heldAll.map((item) => item.id)), [heldAll])

  // Ages are against this browser's clock at render. The worker-freshness decision in the
  // status strip uses the server's clock (motet#38), but an *age* re-read from a server
  // stamp that stops updating the moment nothing is pending would freeze at page load.
  const now = Date.now()

  // A jump from a lifecycle: clear the search so the row exists, open the Read fold if the
  // row is in it, then scroll. The highlight fades on its own.
  useEffect(() => {
    if (!flash) return
    const target = items.find((item) => item.id === flash)
    if (target?.read) setReadOpen(true)
    const raf = window.requestAnimationFrame(() => {
      document.getElementById(`ni-${flash}`)?.scrollIntoView({ behavior: 'smooth', block: 'center' })
    })
    const timer = window.setTimeout(() => setFlash(null), 2_500)
    return () => {
      window.cancelAnimationFrame(raf)
      window.clearTimeout(timer)
    }
  }, [flash, items])

  const jumpToNewsItem = useCallback((id: string) => {
    setQuery('')
    setFlash(id)
  }, [])

  // Selections follow the data: an item that integrated or was deleted leaves the set.
  useEffect(() => {
    setSelection((current) => {
      const present =
        current.list === 'held' ? heldIds : new Set(items.map((item) => item.id))
      const kept = [...current.ids].filter((id) => present.has(id))
      return kept.length === current.ids.size ? current : { ...current, ids: new Set(kept) }
    })
  }, [heldIds, items])

  // Search and sort, over both lists at once.
  const needle = query.trim().toLowerCase()
  const matches = (title: string) => needle === '' || title.toLowerCase().includes(needle)
  const byTime = (a: string, b: string) => {
    const delta = new Date(a).getTime() - new Date(b).getTime()
    return sort === 'newest' ? -delta : delta
  }
  const heldShown = heldAll
    .filter((item) => matches(item.title))
    .sort((a, b) => byTime(a.received_at, b.received_at))
  const unreadAll = items.filter((item) => !item.read)
  const readAll = items.filter((item) => item.read)
  const newsShown = (list: NewsItem[]) =>
    list.filter((item) => matches(item.title)).sort((a, b) => byTime(a.created_at, b.created_at))
  const unreadShown = newsShown(unreadAll)
  const readShown = newsShown(readAll)

  // One selection, in one list at a time. Selecting in the other list starts over.
  const toggle = (list: Selection['list'], id: string) => {
    setSelection((current) => {
      const ids = new Set(current.list === list ? current.ids : [])
      if (ids.has(id)) ids.delete(id)
      else ids.add(id)
      return { list, ids }
    })
  }
  const toggleAll = (list: Selection['list'], ids: string[]) => {
    setSelection((current) => {
      const have = current.list === list ? current.ids : new Set<string>()
      const every = ids.length > 0 && ids.every((id) => have.has(id))
      const next = new Set(have)
      for (const id of ids) {
        if (every) next.delete(id)
        else next.add(id)
      }
      return { list, ids: next }
    })
  }
  const clearSelection = () => setSelection(NO_SELECTION)

  const setRead = async (targets: NewsItem[], read: boolean) => {
    setError('')
    setBusy(true)
    try {
      // One request per item: the API has no bulk route, and a row that fails to flip
      // should fail alone rather than take the rest with it.
      const results = await Promise.allSettled(targets.map((item) => api.setRead(item.id, read)))
      const failed = results.filter((result) => result.status === 'rejected')
      if (failed.length > 0) {
        const first = failed[0] as PromiseRejectedResult
        const reason = first.reason
        setError(
          `${failed.length} of ${targets.length} could not be marked: ${reason instanceof ApiError ? reason.message : String(reason)}`,
        )
      }
      onChanged()
      clearSelection()
    } finally {
      setBusy(false)
    }
  }

  const ingest = async () => {
    setBusy(true)
    setError('')
    setNotice('')
    try {
      const result = await integrateHeld([...selection.ids])
      setNotice(
        `${result.queued} queued for processing${result.skipped ? ` (${result.skipped} skipped)` : ''}.`,
      )
      clearSelection()
      refreshHeld()
      onChanged()
    } catch (err) {
      setError(err instanceof ApiError ? err.message : err instanceof Error ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }

  const makeEpisode = async () => {
    setBusy(true)
    setError('')
    try {
      const episode = await api.createEpisode(
        `Briefing — ${new Date().toLocaleDateString()}`,
        minutes * 60_000,
      )
      onOpenEpisode(episode)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }

  const inFlight = ingestion.some((item) => !heldIds.has(item.id))
  const nothingAtAll =
    items.length === 0 && heldAll.length === 0 && !inFlight && !ingestionUnavailable

  return (
    <section aria-labelledby="backlog-heading" className="backlog">
      <h2 id="backlog-heading">Backlog</h2>

      <BacklogHeader
        waiting={heldAll.length}
        ready={unreadAll.length}
        read={readAll.length}
        minutes={minutes}
        onMinutes={setMinutes}
        busy={busy}
        onMakeEpisode={makeEpisode}
      >
        <div className="backlog-tools">
          <StatusStrip
            ingestion={ingestion}
            unavailable={ingestionUnavailable}
            processing={processing}
            heldIds={heldIds}
          />
          <div className="backlog-filters">
            <input
              type="search"
              className="backlog-search"
              aria-label="Search titles"
              placeholder="Search titles…"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
            />
            <label className="sort">
              <span className="hint">Sort</span>
              <select
                aria-label="Sort"
                value={sort}
                onChange={(event) => setSort(event.target.value as Sort)}
              >
                <option value="newest">Newest first</option>
                <option value="oldest">Oldest first</option>
              </select>
            </label>
          </div>
        </div>
      </BacklogHeader>

      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
      {notice && (
        <p className="ok backlog-notice" role="status">
          {notice}
        </p>
      )}

      <HeldList
        items={heldShown}
        total={heldAll.length}
        query={query}
        selected={selection.list === 'held' ? selection.ids : new Set()}
        onToggle={(id) => toggle('held', id)}
        onToggleAll={(ids) => toggleAll('held', ids)}
        onOpen={setDrawer}
        now={now}
        error={heldError}
      />

      <NewsList
        unread={unreadShown}
        read={readShown}
        totalUnread={unreadAll.length}
        totalRead={readAll.length}
        nothingAtAll={nothingAtAll}
        query={query}
        selected={selection.list === 'news' ? selection.ids : new Set()}
        expanded={expanded}
        flash={flash}
        readOpen={readOpen}
        onReadOpen={setReadOpen}
        now={now}
        onToggle={(id) => toggle('news', id)}
        onToggleAll={(ids) => toggleAll('news', ids)}
        onExpand={setExpanded}
        onOpenSource={setDrawer}
        onSetRead={(item, read) => void setRead([item], read)}
      />

      <SelectionBar
        selection={selection}
        held={heldAll}
        news={items}
        busy={busy}
        onIngest={() => void ingest()}
        onSetRead={(read) =>
          void setRead(
            items.filter((item) => selection.ids.has(item.id) && item.read !== read),
            read,
          )
        }
        onClear={clearSelection}
      />

      {drawer !== null && (
        <Drawer sourceItemId={drawer} onClose={() => setDrawer(null)} onJumpToNewsItem={jumpToNewsItem} />
      )}
    </section>
  )
}
