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
  bin/                     toolchain install + the two CI entry points
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
ios/bin/build-app         # xcodebuild, for the simulator — needs a Mac
bin/generate-ios-client   # regenerate the client from openapi.yaml
```

`bin/ci` is the Linux half. The Mac half is the `ios` job in `.github/workflows/ci.yml`,
on a GitHub-hosted `macos-latest` runner — free, because this repo is public, and
credential-free, because a **simulator** build needs no identity, no certificate, no
provisioning profile and no App Store Connect key. It runs on any change under `ios/**`,
and it runs `ios/bin/build-app` and `swift test --package-path ios`, nothing else.

**`ios/bin/build-app` is a script rather than a command inlined in the workflow**, for the
same reason `bin/build-images` is: a check that exists only in YAML cannot be run by hand
and will rot. It is *not* called from `bin/ci`, and that is the same reason again in
reverse — it needs Xcode, which no machine in this project has except that runner, so
calling it from `bin/ci` would turn every Linux run red. On a Mac without Xcode it skips
and says so; in CI it fails, because a green run that compiled nothing is worse than a red
one.

**Verified:** the whole app compiles — `App/`, `Sources/MotetPlayback/` and
`Motet.xcodeproj` included — for the iOS Simulator, under Swift 6 language mode with
strict concurrency checking, and 85 tests
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

## What is **not** verified — and what no longer belongs on that list

Three things used to head this list: that the app had never been built by Xcode, that
`Motet.xcodeproj` might not even parse, and that no simulator SDK had ever seen it. All
three are now answered by CI on every change under `ios/**`, and the answers were cheap:
the project file parses, and the first compiler ever pointed at this app found **three**
errors in about a thousand lines of unproven Swift.

Worth keeping, because they are the shape of what review cannot catch:

* Two in `NowPlayingController` — `MPRemoteCommandEvent` is a non-Sendable class MediaPlayer
  still owns, and a `Task` body is a `sending` closure, so reading `positionTime` *inside*
  the task is a data race rather than a convenience. Read the number out first, send the
  number.
* One in `CarPlaySceneDelegate` — `??` takes its right-hand side as an autoclosure, and an
  autoclosure is not `async`, so an `await` cannot live there however well the chain reads.

Everything below is still unproven, and the reason is the same for all of it: **a build is
not a run, and a simulator is not a phone.** A green `ios` job says the code compiles and
links. It says nothing about any of this.

1. **Nothing has been run or screenshotted.** The job builds for
   `generic/platform=iOS Simulator`, which never boots a simulator. No screen in this app
   has been looked at by anyone.
2. **Background audio.** `UIBackgroundModes: audio` plus the `.playback` category with
   `.spokenAudio` and `.longFormAudio` is written; a simulator's host OS does not enforce
   any of it. Prove it on a device: play, lock the screen, put the phone in a pocket, walk.
3. **The mute switch.** `.playback` is what keeps audio going when the ringer is silenced.
   Device-only.
4. **Lockscreen and Control Centre.** `MPNowPlayingInfoCenter` publishes *our* elapsed time
   and the real rate (a rate of 1.0 while playing at 1.5 makes the scrubber drift). Check
   the artwork-less layout, that the scrubber tracks, and that the skip intervals show as
   30/15.
5. **Remote commands.** Play/pause/skip/next/previous/seek/rate are wired to
   `MPRemoteCommandCenter`. Test from the lockscreen, from headphone buttons, and — the one
   most likely to be wrong — next/previous *track*, which this app maps to next/previous
   **story**.
6. **Interruptions and route changes.** A real phone call, Siri, and AirPods disconnecting
   (`.oldDeviceUnavailable` — otherwise a briefing suddenly plays out of the phone speaker
   on the street). The simulator does not generate these faithfully.
7. **Background downloads.** `URLSession.background` transfers that finish while the app is
   suspended, and the `handleEventsForBackgroundURLSession` wake-up. Device-only, and the
   thing to watch is that a download started over breakfast is *there* when the walk starts.
8. **The Keychain.** `kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly` behaves differently
   on a simulator than on a device.
9. **CarPlay.** Enrolment has landed, but `com.apple.developer.carplay-audio` is a separate
   manual review by Apple and can take weeks. **Do not wire `Motet.entitlements` into
   `CODE_SIGN_ENTITLEMENTS` before the grant arrives** — an ungranted entitlement makes the
   build fail to sign rather than merely lack CarPlay, which would take the `ios` job red
   for a reason that has nothing to do with the code. Once granted: set that build setting,
   then test with the simulator's CarPlay window and in a car.
10. **TestFlight and device installs.** Enrolment is done, so these are now unblocked
    rather than impossible — and both need signing, which the `ios` job deliberately does
    not do.

## Playback position is still device-local, and that is now a client gap

Read state syncs: position becomes *completed news items* via the segment map, and each one
is written with `POST /v1/news-items/{id}/read` — the same fact the web backlog writes.
That part is done, and it is what invariant 5 asks for.

`spoken_through_ms` is a different fact, and it is still kept **on the device only**, so a
second device does not know where you got to. When this app was written the contract had
nowhere to put it, which is why it was filed as
[issue #11](https://github.com/tadasant/motet/issues/11). The backend has since answered:
`POST /v1/episodes/{id}/listen-progress` arrived with the Phase 2 backend, and the
generated client already carries `reportListenProgress` because the generator picks up
whatever `openapi.yaml` says.

**Wiring it up is deliberately not in this change.** It is a behaviour change, not a
rebase, and it wants its own tests: `ListeningPositionStore` gains a remote sink, the outbox
gains a third entry kind, and resume has to decide what to do when the device and the server
disagree. Small, and worth doing on its own.

## Configuration

Nothing is baked in. The server URL and the `/v1` token are typed into Settings on first
run — the URL into `UserDefaults`, the token into the Keychain, on this device only. This
repo is public: a default hostname here would be infrastructure topology in it, and a
default token would be a credential in a shipped binary.
