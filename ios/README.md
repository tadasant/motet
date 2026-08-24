# iOS app — Phase 2

Motet's listening surface, **replacing the Phase 1 RSS feed**: playback with speed control
and skips, episodes downloaded before you leave, background audio and lockscreen controls,
CarPlay templates, and read state that agrees with the web backlog.

RSS bought background audio, offline, lockscreen, CarPlay, and speed control with zero iOS
code, and it answered the question that mattered weeks earlier. What it cannot buy is the
thing Phase 2 is for: a client that reports where the listener actually got to, and a place
for voice interaction to attach. That is what this app is.

---

## The shape, and why

```
ios/
  Package.swift            SwiftPM: MotetKit + MotetPlayback + tests
  Sources/MotetKit/        Foundation only. The whole brain. Tested in bin/ci.
  Sources/MotetPlayback/   AVFoundation / MediaPlayer. Needs Apple platforms.
  Tests/MotetKitTests/     85 tests, including an end-to-end offline-walk journey
  App/Motet/               SwiftUI screens, the CarPlay scene, Info.plist, entitlements
  App/Motet.xcodeproj/     the app target
  bin/                     toolchain install + the CI entry point
  tools/                   the openapi.yaml -> Swift generator
```

**The split is the point.** `MotetKit` holds every rule that could be wrong — what a skip
button does, when a story counts as heard, when a position is written, what happens with no
signal — and imports nothing but Foundation. So it builds and its tests run on a Linux CI
runner with no Xcode, no simulator, and no Apple Developer Program membership. `MotetPlayback`
holds the parts that only exist on Apple platforms, behind `#if canImport(AVFoundation)`, so
the package still builds where they do not.

### The contract is generated, like the SPA's

`ios/Sources/MotetKit/Generated/Schema.swift` is generated from `openapi.yaml` by
`ios/tools/generate_swift_client.py`, and `bin/ci` regenerates it and fails on any diff —
the same guarantee `web/src/api/schema.gen.ts` has. Regenerate with:

```bash
bin/generate-ios-client
```

Never hand-edit the generated file. The generator is stdlib Python rather than
`swift-openapi-generator` because `bin/ci` has to run offline, on a laptop, with no Swift
toolchain and no JVM.

### The invariants this app is built around

| | |
|---|---|
| **The client never speaks a vendor protocol** (invariant 1) | There is no OpenAI, Cartesia, or Anthropic call anywhere in this app and no credential for one. Audio comes from `/v1/episodes/{id}/audio`, which either serves bytes or redirects to a signed URL; the client follows the redirect and cannot tell which. |
| **`spoken_through_ms` is ours** (invariant 4) | `PlaybackController` owns the position. `AVPlayer`'s clock is an *input* — it reports 0 while re-buffering after an interruption and knows nothing after the process is killed. The position is written durably by us and survives both. |
| **Read state is per News Item, synced** (invariant 5) | `SegmentTimeline` turns a position into the set of news items fully spoken; each one is written with the same `POST /v1/news-items/{id}/read` the SPA uses, queued in a durable outbox when there is no signal. The app is a participant, not a local copy. |
| **Deterministic commands** | `PlaybackCommand` is a closed set of pure state transitions. A lockscreen button, a steering-wheel remote, a CarPlay tap, and an on-screen tap all funnel through it, with no model and no network in the path. The voice seam (`NarrationControl`) sits *beside* it, so a spoken command can never do something a button could not. |

### The voice seam is a seam, not a stub

`NarrationControl` says what the barge-in path (session #8530's `voice/`) will need from the
player: suspend, resume, and "what is being spoken". `PlaybackController` conforms to it.
Nothing voice-related is implemented here — that is deliberate, and it is the other
session's work.

---

## What is verified, and how

`bin/ci` runs the iOS checks along with everything else. On this repo's Linux runners it
installs a Swift toolchain once per runner (`ios/bin/install-swift-toolchain`, ~1 GB, cached
afterwards); on a Mac it uses Xcode's. Locally:

```bash
ios/bin/ci-swift          # swift build && swift test
bin/generate-ios-client   # regenerate the client from openapi.yaml
```

**Verified:** `MotetKit` compiles under Swift 6's strict concurrency checking, and 85 tests
pass — segment-boundary read state, the difference between listening and skipping, the
outbox's ordering/coalescing/backoff/durability (including a write made *while* another is
in flight), the download policy, position resume across a simulated relaunch, interruption
handling, error mapping, timestamp decoding against the exact shape FastAPI emits, and an
end-to-end "dog walk with no signal" journey that drives the real library, controller,
outbox, and offline store together against fakes for the audio engine, the downloader, and
the network.

**Read state is computed from what was played, not from how far the playhead got.**
`ListenedCoverage` accumulates the intervals the audio actually played, and a news item is
read only once every segment it occupies is covered. That distinction is load-bearing:
`AVPlayer` reports a position immediately after a seek, so a high-water mark would treat
three taps of *next story* as having heard three stories — and quietly empty the backlog,
which is the product's memory. The coverage is persisted beside the position, so a story
skipped on Monday is still unread on Tuesday.

## What is **not** verified — the list to work through once enrolment lands

There is no Apple Developer Program membership yet, **and there is no macOS machine in this
project's CI or agent environment at all.** The second constraint is the larger one: it means
nothing in `App/`, nothing in `Sources/MotetPlayback/`, and not `Motet.xcodeproj` itself has
been compiled, run, or screenshotted. Every item below is written to be correct and is
unproven.

1. **It has never been built by Xcode.** `MotetPlayback` and the SwiftUI/CarPlay sources are
   syntax-checked by review only. Expect to fix compile errors on the first build; that is
   the honest expectation, not a surprise.
2. **`Motet.xcodeproj` is hand-written** (Xcode 16 synchronized root groups, so it is small
   and has no per-file entries). If Xcode refuses to open it: File > New > Project > App
   named `Motet`, delete the generated sources, drag `App/Motet` in as a folder reference,
   add `ios` as a local package dependency, and select MotetKit + MotetPlayback. Two
   minutes, and nothing is lost — the sources are the deliverable.
3. **Simulator playback.** No simulator has run this. `xcodebuild -scheme Motet -sdk
   iphonesimulator build` is the first command to try; signing is already disabled
   (`CODE_SIGNING_ALLOWED = NO`) so it needs no identity.
4. **Background audio.** `UIBackgroundModes: audio` plus the `.playback` category with
   `.spokenAudio` and `.longFormAudio` is written; a simulator's host OS does not enforce
   any of it. Prove it on a device: play, lock the screen, put the phone in a pocket, walk.
5. **The mute switch.** `.playback` is what keeps audio going when the ringer is silenced.
   Device-only.
6. **Lockscreen and Control Centre.** `MPNowPlayingInfoCenter` publishes *our* elapsed time
   and the real rate (a rate of 1.0 while playing at 1.5 makes the scrubber drift). Check
   the artwork-less layout, that the scrubber tracks, and that the skip intervals show as
   30/15.
7. **Remote commands.** Play/pause/skip/next/previous/seek/rate are wired to
   `MPRemoteCommandCenter`. Test from the lockscreen, from headphone buttons, and — the one
   most likely to be wrong — next/previous *track*, which this app maps to next/previous
   **story**.
8. **Interruptions and route changes.** A real phone call, Siri, and AirPods disconnecting
   (`.oldDeviceUnavailable` — otherwise a briefing suddenly plays out of the phone speaker
   on the street). The simulator does not generate these faithfully.
9. **Background downloads.** `URLSession.background` transfers that finish while the app is
   suspended, and the `handleEventsForBackgroundURLSession` wake-up. Device-only, and the
   thing to watch is that a download started over breakfast is *there* when the walk starts.
10. **The Keychain.** `kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly` behaves differently
    on a simulator than on a device.
11. **CarPlay.** `com.apple.developer.carplay-audio` is granted by Apple's manual review
    *after* enrolment, and can take weeks. **Do not wire `Motet.entitlements` into
    `CODE_SIGN_ENTITLEMENTS` before the grant arrives** — an ungranted entitlement makes the
    build fail to sign rather than merely lack CarPlay. Once granted: set that build
    setting, then test with the simulator's CarPlay window and in a car.
12. **TestFlight and device installs.** Both need the membership.

## One thing the API does not offer yet

There is no endpoint for reporting a playback position. `POST /v1/episodes/{id}/listened`
marks a whole episode read; `POST /v1/news-items/{id}/read` marks one item. So the app does
what the contract allows and what invariant 5 actually asks for: it converts position into
*completed news items* via the segment map and writes those, and it keeps
`spoken_through_ms` durably **on the device** for resume.

That is a real implementation of read-state sync, and an incomplete implementation of shared
playback position: a second device would not know where you got to. Filed as
[issue #11](https://github.com/tadasant/motet/issues/11) against the API rather than papered
over here — when the endpoint exists, `ListeningPositionStore` gains a sink and the outbox
gains a third entry kind. Nothing else changes.

## Configuration

Nothing is baked in. The server URL and the `/v1` token are typed into Settings on first
run — the URL into `UserDefaults`, the token into the Keychain, on this device only. This
repo is public: a default hostname here would be infrastructure topology in it, and a
default token would be a credential in a shipped binary.
