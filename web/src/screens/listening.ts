// Where the listener is in an episode, and the one write that says "I heard all of it".
//
// Shared by the shelf (Episodes.tsx) and the detail (EpisodeScreen.tsx) so the two cannot
// disagree about what "listened" means or about what marking it writes.
//
// **Listened state is derived, not stored**, from two fields the server already carries:
// `listened_through_ms` against `duration_ms`. There is no per-episode "listened" flag on
// the API, and adding one would be a second definition of a fact invariant 5 says lives on
// the news item. What that leaves — a row's verdict can disagree with its stories' read
// state, because the position cannot go down — is motet#89's question 2, answered for now
// as option (b) and put to the owner.

import { type Episode, api } from '../api/client'

/** How far short of the end still counts as having heard the whole thing. */
export const LISTENED_SLACK_MS = 5_000

export type ListenState = 'unlistened' | 'in_progress' | 'listened'

/**
 * Where the listener is in an episode, from the two fields the server carries.
 *
 * An episode that is not `ready` reads as unlistened however far anything says it has
 * been played: there is no audio to have heard, and a failed or re-scripted episode may
 * still carry the duration of a render that no longer stands.
 *
 * `listened_through_ms` is the *furthest* point, not the playhead (invariant 4): a
 * listener who scrubbed to the end and back reads as having heard it.
 */
export function listenState(
  episode: Pick<Episode, 'state' | 'listened_through_ms' | 'duration_ms'>,
): ListenState {
  const { listened_through_ms: at, duration_ms: total } = episode
  if (episode.state !== 'ready' || total <= 0 || at <= 0) return 'unlistened'
  if (at >= total - LISTENED_SLACK_MS) return 'listened'
  return 'in_progress'
}

/** `m:ss`, or `h:mm:ss` past an hour — the way a player shows a clock. */
export function formatClock(ms: number): string {
  const total = Math.max(0, Math.round(ms / 1000))
  const hours = Math.floor(total / 3600)
  const minutes = Math.floor((total % 3600) / 60)
  const seconds = total % 60
  const mmss = `${hours > 0 ? String(minutes).padStart(2, '0') : minutes}:${String(seconds).padStart(2, '0')}`
  return hours > 0 ? `${hours}:${mmss}` : mmss
}

/**
 * Mark an episode heard, writing both facts the server has, in this order.
 *
 * First every news item read (invariant 5 — the fact that matters), then the position at
 * the end (`PUT …/position`, the handler `POST …/progress` also reaches), so the episode
 * reads as Listened for the same reason a walk to the end would.
 * **One-way:** the position is monotonic on the server by design, so there is no "mark
 * unlistened" to offer — un-listening could only mean un-reading its news items, which is
 * a decision about the server rather than this screen (motet#89, question 2).
 *
 * Resolves to how many news items the first write marked read.
 */
export async function markEpisodeListened(episode: Episode): Promise<number> {
  const read = await api.markListened(episode.id)
  await api.setPosition(episode.id, episode.duration_ms)
  return read.news_items_marked_read
}
