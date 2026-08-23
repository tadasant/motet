import { Backlog, Episode, PasteIn } from './screens'

// No router yet — Phase 1's SPA is three screens, and routing them is the first thing the
// screen work will add. Rendering them together keeps the scaffold honest about what
// exists.
export default function App() {
  return (
    <main>
      <h1>Motet</h1>
      <PasteIn />
      <Backlog />
      <Episode />
    </main>
  )
}
