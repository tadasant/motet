// The three Phase 1 screens: paste-in, backlog, episode.
//
// Placeholders on purpose. The SPA is the eyes-on backlog surface, not the product — if
// SPA work is still running after a week, that is a tripwire (see AGENTS.md). They exist
// so the shape of the app is visible and so there is something for the contract and the
// build to hang off.

export function PasteIn() {
  return <section aria-labelledby="paste-in-heading">
    <h2 id="paste-in-heading">Paste in</h2>
    <p>Phase 1&rsquo;s only ingestion route. Not built yet.</p>
  </section>
}

export function Backlog() {
  return <section aria-labelledby="backlog-heading">
    <h2 id="backlog-heading">Backlog</h2>
    <p>Deduped news items and their read state. Not built yet.</p>
  </section>
}

export function Episode() {
  return <section aria-labelledby="episode-heading">
    <h2 id="episode-heading">Episode</h2>
    <p>Transcript with each claim beside its source span. Not built yet.</p>
  </section>
}
