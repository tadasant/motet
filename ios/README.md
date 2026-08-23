# iOS app — Phase 2

Swift, on device. **Not built yet.** This directory exists so the component has an address
before it has code.

Native iOS is required rather than preferred: a PWA has no background mic, no CarPlay, and
unreliable lockscreen audio. Phase 1 ships a private authenticated RSS feed instead, which
buys background audio, offline, lockscreen, CarPlay, and speed control with zero iOS code —
and answers the question that matters weeks earlier. This app replaces that feed.

Invariant 4 applies here more than anywhere: **`spoken_through_ms` is tracked by us**, not
read back out of a vendor SDK.
