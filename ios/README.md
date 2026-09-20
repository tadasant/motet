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
  Tests/MotetKitTests/     262 tests (265 on a Mac), incl. an end-to-end offline-walk journey
  App/Motet/               SwiftUI screens, the CarPlay scene, Info.plist, entitlements
  App/MotetUITests/        XCUITest: the one flow that RUNS the app, on a simulator
  App/Motet.xcodeproj/     two targets, three configurations (Debug, Release, Staging)
  bin/                     toolchain install + the three CI entry points
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
| **Deterministic commands** | `PlaybackCommand` is a closed set of pure state transitions. A lockscreen button, a steering-wheel remote, a CarPlay tap, and an on-screen tap all funnel through it, with no model and no network in the path. Play Live's seam (`NarrationControl`) sits *beside* it, so a spoken command can never do something a button could not. |

### Play Live is built on the phone, as on the web

`LiveSession` (MotetKit) is the SPA's `Live.tsx` ported rule for rule (motet#93): the API
mints the session and hands back a socket and the frame to open it with (invariant 2 — the
app never holds the voice service's start token); the app tells the service whether
narration is playing and where (`narration_delivered`, `narration_paused`,
`narration_resumed`, `playback_position`); listener audio goes as 16 kHz mono int16 binary
frames for the service's own detector to decide a barge-in on; a barge-in pauses the
player, a finished reply resumes it from the interruption offset, and a pause the listener
makes is never reported as one. `PlaybackController` is the narration it pauses and plays,
so the position is still ours (invariant 4), and nothing names a vendor (invariant 1).

The device halves are `MotetPlayback`'s: `URLSessionLiveTransport` (the socket — it sends no
`Origin`, which the voice service admits because that check is for browsers) and
`AVLiveAudio` (the mic through `AVAudioEngine` with voice processing on, and the replies —
streamed `pcm16` scheduled back to back, or one container for the composed arm). For the
length of a Live session the audio session is `.playAndRecord`; stopping hands the
listening session back. The mic pill is the SPA's: a press starts Play Live, a press while
narrating interrupts, and a deployment with no voice service shows it disabled with the
API's reason beside it.

## What is verified, and how

`bin/ci` runs the iOS checks along with everything else. On this repo's Linux runners it
installs a Swift toolchain once per runner (`ios/bin/install-swift-toolchain`, ~1 GB, cached
afterwards); on a Mac it uses Xcode's. Locally:

```bash
ios/bin/ci-swift          # swift build && swift test
ios/bin/build-app         # xcodebuild, for the simulator — needs a Mac
ios/bin/ui-test           # RUNS the app on a booted simulator — needs a Mac
bin/generate-ios-client   # regenerate the client from openapi.yaml
```

`bin/ci` is the Linux half. The Mac half is the `ios` job in `.github/workflows/ci.yml`,
on a GitHub-hosted `macos-latest` runner — free, because this repo is public, and
credential-free, because a **simulator** build needs no identity, no certificate, no
provisioning profile and no App Store Connect key. It runs on any change under `ios/**`,
and it runs `ios/bin/build-app` and `ios/bin/ci-swift`, nothing else — the second being the
same script `bin/ci` calls on Linux, which picks up whichever Swift is on PATH.

**`ios/bin/build-app` is a script rather than a command inlined in the workflow**, for the
same reason `bin/build-images` is: a check that exists only in YAML cannot be run by hand
and will rot. It is *not* called from `bin/ci`, and that is the same reason again in
reverse — it needs Xcode, which no machine in this project has except that runner, so
calling it from `bin/ci` would turn every Linux run red. On a Mac without Xcode it skips
and says so; in CI it fails, because a green run that compiled nothing is worse than a red
one.

**Verified:** the whole app compiles — `App/`, `Sources/MotetPlayback/` and
`Motet.xcodeproj` included — for the iOS Simulator, under Swift 6 language mode with
strict concurrency checking, and 262 tests pass — 265 on a Mac, where three more put the
local-file format rule to AVFoundation itself rather than to our own account of it.
Segment-boundary read state, the difference between listening and skipping, the
outbox's ordering/coalescing/backoff/durability (including a write made *while* another is
in flight), the download policy, what a downloaded file is named and what happens to one
an older build misnamed, position resume across a simulated relaunch, interruption
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

**A downloaded episode is named after what its bytes are, and that is not cosmetic.**
AVFoundation picks a reader for a *local* file from the path extension and from nothing
else — there is no HTTP response to carry a `Content-Type` — so an extension it does not
recognise means no reader at all, and `AVURLAsset` fails with `AVFoundationErrorDomain
-11828` ("Cannot Open", "This media format is not supported") before a byte is parsed.
Every download used to be named `<episode-id>.audio`, which is a media extension nowhere,
so **no downloaded episode had ever been playable** — and because the download policy keeps
the newest episodes on the phone, the newest episode is the one that is always local and
therefore was always refused. `AudioFileFormat` sniffs the first few kilobytes and
`OfflineLibrary` names the file after the answer. Sniffing rather than trusting the
server's `audio_media_type`, for two reasons: it is the only thing that can repair a file
already on a phone, where no media type was ever recorded beside it, and it is the one
check that an HTML sign-in page or an empty object saved under an audio filename cannot
pass. Something that is not audio is refused rather than filed, because
`MotetLibrary.source(forEpisode:)` prefers the device — a bad file held in the library
would shadow the API for that episode on every future tap.

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

1. **Most of the app has still never been run.** `ios/bin/build-app` builds for
   `generic/platform=iOS Simulator`, which never boots one. What *is* now run is one flow:
   `ios/bin/ui-test` boots a simulator and drives the playback probe (below), which is the
   only screen an automated run can reach — because **an agent cannot sign in**, so no test
   here gets past the front door. Every other screen is still unlooked-at except through
   the `-MotetScreenshot` fixtures.
2. **Background audio.** `UIBackgroundModes: audio` plus the `.playback` category with
   `.spokenAudio` and `.longFormAudio` is written; a simulator's host OS does not enforce
   any of it. Prove it on a device: play, lock the screen, put the phone in a pocket, walk.
   What is no longer unknown is **whether the category was set at all**: see "Silence has a
   reason now" below. The `audio-session` log category names the shape that took.
3. **The mute switch.** `.playback` is what keeps audio going when the ringer is silenced.
   Device-only.
4. **Lockscreen and Control Centre.** `MPNowPlayingInfoCenter` publishes *our* elapsed time
   and the real rate (a rate of 1.0 while playing at 1.5 makes the scrubber drift). Check
   that the mark shows as artwork, that the scrubber tracks, and that the skip intervals
   show as 30/15.
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
10. **Signing and the upload.** `ios/bin/testflight check` proves an unsigned device archive
    has the shape App Store Connect wants, on every PR. Whether Apple's cloud signing accepts
    it and processing passes is only answered by the first `testflight.yml` run, which
    needs the Apple key — see "Distribution" below.
11. **The brand typefaces.** Fraunces and Instrument Sans are bundled under `App/Motet/Fonts`
    and registered at runtime (`BrandFont`), with `wght`/`opsz`/`SOFT` set through a
    variation descriptor (`WONK` 0 too). A build cannot tell a registered face from the
    system fallback, and the fallback is also a serif, so look for what only the real face
    does wrong: **headings in a heavy black weight mean the variations were dropped** (the
    file's default instance is 9pt Black). The `fonts` log category says which faces
    registered.
12. **The custom scrubber and Dynamic Type.** Check a drag that is released, a drag cancelled
    by pulling the sheet down (the knob and both times must return to the playback
    position), VoiceOver's adjust gesture (it skips), and a text-size change in Control
    Centre while the app is open (the tree rebuilds at the new size).
13. **Play Live on a phone.** Everything `LiveSession` decides is tested; what `AVLiveAudio`
    does is not. The questions only a device answers: that the permission prompt appears
    and a refusal says so; that switching to `.playAndRecord` mid-briefing does not stall
    `AVPlayer`; that voice processing cancels the replies and — the open one — whether it
    also cancels the narration coming out of the speaker, or only headphones make an open
    mic workable (the web has the same question and recommends headphones); how AirPods
    route — the session allows Bluetooth input, which most likely moves AirPods to the
    hands-free profile and makes the briefing itself call-quality while Live runs, the
    trade for a mic that is at the listener's ear rather than in a pocket; that a route
    change mid-session restarts the engine rather than ending it; and that the listening
    session comes back after Stop Live, lock screen and all. The mic meter's dBFS is the
    number the service's detector compares against its noise floor, so a session that
    never barges in is diagnosable from the screen.
14. **A consent handed back through the web app.** Connecting a mailbox, label sync's
    re-consent and authorizing an MCP server all rely on the web app's `/oauth/callback`
    page, loaded inside the sheet, handing the consent to `motet://consent` (see "Sources
    and connectors on the phone"). The first real mailbox connected from the phone is the
    evidence.

## Silence has a reason now

Reported against TestFlight on 2026-09-20: *"The Play button doesn't actually start the
audio… can't hear anything. Same with Live. The VAD seems to work but not getting any audio
and the timer line doesn't move."* Production telemetry settled where the fault is **not**:
the phone fetched `GET /v1/episodes/{id}/audio` and the API answered `307` to a signed URL,
one request, no error, no expiry, and no second request — so nothing was wrong with the
server, the route, the feed token, or range support. The narration offsets the phone
reported to the voice service never advance on their own; they are scrub positions. The
clock was never running.

**What no observation in this app could tell you is why**, and that was the defect worth
fixing here, because it is the one that made a device bug undiagnosable from anywhere:

* **`AVPlayer` has three control states and this app observed two.** `.playing` and
  `.paused` were wired; `.waitingToPlayAtSpecifiedRate` — the one a device sits in when a
  remote asset will not start — was not. In it `AVPlayerItem.status` stays `.readyToPlay`,
  no error is ever set, `didPlayToEndTime` never fires and the clock does not tick, so every
  other observation in `AVPlayerPlaybackEngine` reported that all was well while the screen
  drew a pause button over silence. It is now `PlaybackEngineEvent.waiting`, carrying
  `reasonForWaitingToPlay`; the engine re-sends it every two seconds, because a stalled
  player emits nothing else and "still waiting" is the whole thing there is to decide.
  `PlaybackController` shows it as a sentence under a spinner while it can still clear, and
  turns it into an error after fifteen seconds — measured from the start of the *wait*, not
  of whichever reason is current, because `AVPlayer` walks between reasons inside one wait
  as a matter of routine. `noItemToPlay` is an error at once, because nothing is arriving
  that would end it. `play()` no longer emits an optimistic `.playing`: `timeControlStatus`
  is the only source of both, so the truthful event cannot be raced away by the hopeful one.
* **The other silent shape has no `AVPlayer` reason at all, so it has one of ours.**
  `playImmediately(atRate:)` can leave `timeControlStatus` at `.paused` — which is what a
  refused or inactive audio session looks like from here — and then nothing changes, so no
  KVO fires and there is no `reasonForWaitingToPlay` to read. `play()` therefore arms the
  same heartbeat, and a player still `.paused` a probe later is reported as
  `PlaybackWaitReason.notStarted`. That case is not one of AVFoundation's; it is the
  reported bug's own shape.
* **It is logged, under `playback` and `audio-session`.** A screen only says something while
  somebody is looking at it, and this is a bug on a phone in somebody else's pocket: the
  line that travels is the one in a sysdiagnose. Each wait is logged once when it starts,
  with its reason, and once when it clears; the audio session logs which rung took.
* **A refused `setCategory` was swallowed by `try?`, at both call sites.** That is the
  quietest failure the app had: the session stays on the process default `.soloAmbient`,
  which is *muted by the ringer switch* and *stops on lock* — the exact bug report, with
  nothing anywhere saying so. iOS validates category, mode, policy and options together and
  has tightened that validation between releases (18.5 refuses any explicit
  `CategoryOptions` alongside `longFormAudio` — not a tightening this app hit, since it
  passed none, but the general point stands), so one hard-coded combination is a bet on a
  table that moves. `AudioSessionPlan.listening` is a ladder instead — `.playback`
  throughout, giving up long-form route sharing and then spoken-audio ducking — and running
  out of rungs is a sentence on the player screen, not a `try?`. **A rung that took is not
  shown to the listener**, only logged: "no AirPlay 2 grouping" on a working player in error
  red would train them to ignore the one line that means silence. The ladder *and the walk
  down it* live in `MotetKit`, so CI tests both where AVFoundation does not exist;
  `AudioSessionController` supplies the closure that calls `AVAudioSession`.
* **The session shape is now applied before every play**, not once at launch. It is
  process-wide state that Play Live deliberately moves to `.playAndRecord`, and assuming
  the startup call still holds is how a briefing ends up in the earpiece.

**Play Live's silence in production is a different thing and is not an app bug.** The voice
service logs, at startup, `arm=composed … openai_key=absent` and *"arm composed is dormant:
no speech-to-text vendor is provisioned for the composed arm"*. Barge-in detection is
unaffected — which is precisely why the VAD looked healthy — and no reply is possible at
all. Provisioning that vendor is the private repo's, not this one's. What **was** an app bug
is that the session said so only as prose inside a transcript log line: `ready` now carries
`can_answer`, `LiveSnapshot.canAnswer` reads it, and the panel says the session will not
answer rather than offering typed questions it cannot answer either. Deliberately a field
and not a `reason` code — `arm_dormant` is emitted for a live channel that would not open on
an arm whose typed questions still work, so a client branching on the code alone tells half
of them the wrong thing (AGENTS.md, "Play Live is built").

**What none of this proves is that the audio now plays.** Nothing in CI boots a simulator,
and the fault is a device one. The evidence that the fix worked is the first TestFlight
build where Play produces sound; the evidence that this change is worth having either way is
that a build where it *doesn't* now says what stopped it, on screen and in the
`audio-session` and `playback` log categories, instead of showing a button that does
nothing.

## "Is sound coming out" is a number now, and a machine can read it

`Sources/MotetKit/Diagnostics/`, `Sources/MotetPlayback/AudioLevelMeter.swift`,
`App/Motet/Support/PlaybackProbe*.swift`, `App/MotetUITests/`.

The section above fixed a silent player and left the *reporting* where it was: the only
witness to "there was no sound" was a person listening. That is not a gap automation can
close from outside, and it is worth saying why rather than discovering it again —
**no cloud device service captures iOS audio.** AWS Device Farm's session artifacts are
video, logs and screenshots, with no audio track documented anywhere; Appetize supported
iOS audio output in the past, has deprecated it with no plans to restore it, and has never
supported microphone input on any platform. So the question has to be answered by the app
about itself, or it cannot be answered at all.

`PlaybackProbe` is that answer, in three layers of increasing proof and decreasing
availability:

| Layer | What it is | What it catches |
|---|---|---|
| 1 | `AVPlayer.timeControlStatus`, the rate, `reasonForWaitingToPlay`, the last error | told to play vs actually playing |
| 2 | whether the clock **moved**, over `PlaybackClockWatch.window` | the failure above: `.waitingToPlayAtSpecifiedRate` sets no error, emits no event and never ticks |
| 3 | RMS/peak off an `MTAudioProcessingTap` on the player item's audio mix | a running clock and a silent speaker — the only layer that is not the player's own opinion |

Beside them sits the audio **route** as iOS actually has it, because the other half of a
silent phone is a `setCategory` the system refused, which leaves the session on
`.soloAmbient` while every other signal says playback is fine.

Four things about it are decisions rather than implementation:

- **A missing layer abstains; it does not vote no.** `AudioLevelMeter.level()` answers nil
  until a buffer has *ever* arrived and whenever the sample format is one it cannot read,
  and `PlaybackProbe.isAudible` treats nil as "nothing measured" rather than "silence".
  Conflating the two would make any build where the tap did not install report a fault it
  has no evidence for — on every perfectly good episode.
- **The clock question is a window, not a comparison.** `AVPlayer` reports its position
  about once a second, so two samples 80 ms apart are equal on a healthy player;
  `PlaybackClockWatch` keeps a 2.5-second window and answers how far the position moved
  across it, forward only. A seek, a skip and a resume all call `discontinuity()`, because a
  jump the listener asked for is not audio — the same distinction `maxListeningStepMs`
  already makes for read state, asked one command earlier.
- **The diagnostic never makes playback worse.** The tap is attached off the critical path
  (loading an asset's tracks is a network round trip for a remote episode), a tap that
  cannot be created is logged and dropped, and nothing in this path can throw into a `play`.
- **It runs in Release, and that is the point.** The on-screen strip is `#if DEBUG`
  furniture; the `playback-probe` log category is what makes a TestFlight build answer the
  question. One line, `key=value`, at WARNING when a player that was told to play is
  producing nothing:

  ```
  playback silent: episode=ep_1f2c8dc8486c audible=false transport=playing position_ms=9408
    advanced_ms=0 rate=1.0000 silence=clockNotMoving wait=none rms=unmeasured
    category=Playback mode=SpokenAudio policy=longFormAudio output=Speaker error=none
  ```

  That is what the 2026-09-20 report would have looked like, from the phone, without
  anybody having to listen.

**What is tested where.** The rules — the window, the abstention, the verdict, the
locale-proof formatting — are `MotetKit` and run on Linux in `bin/ci`, which is the same
split `AudioSessionPlan` makes. Whether the tap actually fires is a claim about a running
`AVPlayer`, and `ios/bin/ui-test` is what makes it.

## The one flow that actually runs the app

`ios/bin/ui-test`, `App/MotetUITests/PlaybackProbeUITests.swift`,
`.github/workflows/ios-ui-tests.yml`.

```bash
ios/bin/ui-test                                              # Debug, no default server
ios/bin/ui-test --configuration Staging --api-base-url https://example.test
gh workflow run ios-ui-tests.yml --ref main \
  -f configuration=Staging -f api_base_url_variable=MOTET_IOS_STAGING_API_BASE_URL
```

It boots a simulator, builds, records video, runs two XCUITests, and keeps the result
bundle, the video and **three** screenshots. Three because the obvious one is worthless
alone: XCUITest terminates the app when the run ends, so a `simctl` screenshot taken then
catches the springboard. The other two relaunch the app and photograph the probe idle and
then — with `-MotetPlaybackProbeAutoplay` — playing, because `simctl` can screenshot and
cannot tap, which is the whole reason that launch argument exists. **A pair, because
either alone proves nothing**: a probe hard-wired to AUDIBLE would take the second and one
hard-wired to SILENT would take the first. The UI test does not use autoplay — its
assertion is the same transition, which needs a press. **XCUITest rather than Maestro**: the project
already has Swift, an Xcode project and a macOS runner, and Maestro would add a JVM, a YAML
dialect and a second thing to keep in step with the app's accessibility identifiers for no
capability this needs.

**What it drives, and why it is not the real player screen.** An agent cannot sign in —
Google refuses an automated browser at the identifier step, which AGENTS.md records as
settled — so no automated run reaches a screen that needs a session or an episode that came
from a server. `-MotetPlaybackProbe` is a Debug-only fixture instead: it generates a 20-second
tone (a WAV is a header and a sine; a binary fixture in a public repo is a thing nobody can
review), and plays it through the **real** `AVPlayerPlaybackEngine`, the **real**
`AudioSessionController`, the **real** `PlaybackController` and the **real**
`PlaybackProbeRecorder`. Everything between the play button and the audio tap is the code
that ships. The network half — the feed token, the 307, the signed URL — is deliberately not
in it, and has its own tests.

**The assertion is the transition, not either end of it.** "It says audible while playing"
would pass against a probe hard-wired to true, and "silent while paused" against one
hard-wired to false; the test asserts both and that they differ. Layer 3 is asserted only
when something measured it, and an abstention is recorded as an activity rather than failed —
a runner with no audio device installs the tap and is handed nothing, and layers 1 and 2
still answer the question.

**One flow on purpose.** A broad suite that is flaky on day one reddens every iOS pull
request for reasons that have nothing to do with the change.

**Where it runs, and what that costs.** The `ios` job in `ci.yml` runs it on every change
under `ios/**`, on the macOS runner that job was already paying for — a simulator boot plus
one build-and-run, roughly 5 to 10 minutes on top of a job that was already taking about
that. GitHub bills macOS at roughly ten times the Linux multiplier, which is free on a
public repository and would not be on a private one; a second macOS job would have doubled
the iOS cost for the same coverage, so this rides the existing one.
`.github/workflows/ios-ui-tests.yml` is the `workflow_dispatch` entry point on top, for
pointing a build at a server CI does not know about — expected to be run by hand and rarely.
It carries no credential (a simulator build needs no identity) and names no host: the server
arrives as the **name** of a repository variable, and its value is masked out of the log.

## A build says which deployment it is for

`MOTET_BUILD_ENVIRONMENT` → `Info.plist`'s `MotetBuildEnvironment` →
`MotetKit.BuildEnvironment` → the badge and the `build-target` line.

The server a build defaults to has been switchable since TestFlight shipped
(`MOTET_DEFAULT_API_BASE_URL`, passed on the command line so no host is written here). What
did not exist was any way to **tell which one you were on**: a build pointed at staging and
a build pointed at production were byte-identical on screen, so a tester, a screenshot, a
bug report and an automated run could none of them say which API the app in front of them
was talking to. The two problems look like one and are not.

So there is a third build configuration, `Staging` — `Debug` plus
`MOTET_BUILD_ENVIRONMENT = staging` — and a shared `Motet (Staging)` scheme:

```bash
ios/bin/build-app --configuration Staging --api-base-url https://…
```

- **The label is not a hostname**, which is the whole reason it can live in a public
  repository when the URL cannot. It carries no topology at all; the host beside it is
  whatever the build or the device was given.
- **Unlabelled reads as production**, and that direction is the safe one: a staging build
  mistaken for production wears a badge it should not, and a production build mistaken for
  staging would put "this is disposable" over somebody's real backlog.
- **Two facts, not one.** `BuildTarget` reports the compiled-in label *and* the server in
  force, with `source=build` or `source=settings` — because saving a server under Advanced
  makes them disagree, and a badge that kept claiming the build's answer would be lying.
- **`Staging` keeps `DEBUG` defined**, deliberately: the fixtures a UI test drives are
  `#if DEBUG`, and a staging build without them could not be exercised by anything
  automated. It is a build to *test* with, not a second thing to ship.
- **`MotetUITests` is not in the `Motet` scheme**, and that is load-bearing:
  `ios/bin/testflight` reads `xcodebuild -showBuildSettings -scheme Motet` to decide which
  entitlements file is signed in, and that guard reasons about the blocks it gets back — one
  per target in the scheme.

**What is proven, and what is configuration.** `ios/bin/ui-test` builds `Staging` with a
base URL and then checks the built `.app`'s own `Info.plist` for both keys — so the
substitution is proven from the artifact — while the UI test asserts what the *running* app
made of them. What no run here can prove is that the app reaches the real staging API: the
host lives in the private infrastructure repo, and **turning this on is one variable**, a
repository or environment variable (`MOTET_IOS_STAGING_API_BASE_URL` is the name the
dispatch input expects) holding staging's https base URL.

**A staging build on a phone beside production is deliberately not built.** That needs a
second bundle identifier, and with it a second App ID, its own Associated Domains tick and
its own App Store Connect record — all of which are invariant 9's human half. The simulator
is where an automated run lives, and there is nothing for a staging bundle id to collide
with.

## Playback position is cross-device

Read state has always synced: position becomes *completed news items* via the segment map,
each written with `POST /v1/news-items/{id}/read` — the same fact the web backlog writes
(invariant 5). The **position** now syncs too ([issue #11](https://github.com/tadasant/motet/issues/11)).

* **Reading it.** Every `EpisodeResponse` carries the server's `listened_through_ms`. Loading
  an episode resumes there when it is past everything *this* phone ever heard of it — the
  listening happened on another device — and at the phone's own playhead otherwise,
  including one the listener deliberately scrubbed back to, which stays device-local. An
  episode never played on the phone shows the server's progress in its row.
* **Writing it.** `PUT /v1/episodes/{id}/position`, every ten seconds of listening and on
  pause, finish and unload. What is sent is **not** the playhead: the server marks every
  story a position has passed, so the value is the end of the listening that is unbroken
  from where the server already is (`ListenedCoverage.frontier`) — the SPA's rule, "only
  continuous listening from the frontier moves the position". Listening on past a skipped
  story moves nothing. Best-effort and not queued: the server's value is monotonic and the
  next report carries the same frontier, so a report lost to no signal is made good by the
  next one; after a failure the phone leaves the server alone for ten seconds rather than
  trying on every tick.
* **Mark listened** (swipe an episode right) is the SPA's: every story read through the
  outbox, then the position at the end.

**Ordering constraint:** `listened_through_ms` is a required field, so a build cannot read
episodes from an API revision older than it. Since a merge uploads a TestFlight build on its
own (see "Distribution"), nothing enforces that order any more: a new field the app decodes
has to be optional with a default, as `keep_in_backlog` is, or the build fails until the API
behind `MOTET_IOS_API_BASE_URL` serves it — check its `/internal/health` `revision`.

## Audio that will not load is said out loud

The controller always knew when audio failed to load and when it was still loading; the
player showed neither, so an episode whose audio would not load was a play button that did
nothing. The player now says so — and says **why**, because `AVPlayer`'s error carries no
HTTP status: it asks the audio route for two bytes (`MotetHTTPClient.audioProblem`), as the
web player does since motet#129. A 410 is the API's own sentence — the file was rendered and
has since been removed from storage, which no retry fixes, so the player offers none; a
served file means the phone could not play it, and **Try again** stays. A refused feed token
is replaced and the question asked again: the token authenticates the audio route and used
to be cached until the app was reinstalled, so a token rotated on the web broke streaming on
the phone for good.

## Sources and connectors on the phone

The **Sources** tab is the SPA's Sources and Credentials screens (motet#90, motet#102) on the
phone: every mailbox with its status, last sync and what it pulled in; Sync now; how far back
its first sync reached and a "Sync further back" that searches again from a chosen point
(motet#139); label sync and its re-consent (motet#96); Disconnect, and removing a consent that
never finished; the built-in Paste source; and the sites and MCP servers enrichment may use —
added, authorized and removed. It calls the same routes the SPA does and adds none. The rules
— what a row means, what a card says, which counts belong to which mailbox, what a sync window
is called — are `SourceStatus`, `ConnectorStatus` and `FirstSyncWindow` in MotetKit, ported
from the SPA and tested on Linux.

**Held items are a screen, not a number** (`HeldItemsView`). A connected mailbox polls,
fetches and extracts on its own and stops; inference is spent only when a person picks items
and says ingest (motet#91). The app used to show only the *count* of what was waiting, on the
Sources screen, with nothing behind it — so from a phone, 55 newsletters that had been pulled
in correctly were indistinguishable from 55 that were never synced, because the Backlog tab
lists news items and a held item is deliberately not one yet (motet#139). It is now the SPA's
"Pulled in, waiting for you" panel, rule for rule, reachable from the mailbox's own count and
from a banner above the Backlog.

**A consent comes back to the phone through the web app's callback page.** Google returns
every consent to the one address registered on Motet's OAuth client, the web app's
`/oauth/callback`, and it refuses a custom scheme on a web client — so a second registration
would be a human step (invariant 9) and a second OAuth client. Instead the app asks the API
for that same web address and opens the consent in the system sign-in sheet, told to finish
on **`motet://consent`**. Google's redirect loads the web app's callback page inside the
sheet; that page finds nothing remembered in its (empty) storage, so it knows the consent is
the app's, exchanges nothing, and hands exactly what Google sent to `motet://consent`
(`web/src/screens/AppConsentHandoff.tsx`). The sheet closes on it and the app finishes the
consent with its own session at the route the SPA would have called (`/v1/sources/callback`,
or `/v1/connectors/oauth/callback` for an MCP server). `ConsentCallback` checks the scheme,
the host and the `state` before anything is spent.

**It used to wait for Google's https redirect instead, and that is what broke** (Tadas,
2026-09-19: "the iOS app can't do it at all"). An https callback is honoured only on iOS 17.4
and later, only once the device has verified the `webcredentials` association, and only if
iOS catches Google's cross-site redirect. When any of that failed, the web app loaded in the
sheet — which holds no Motet session, by design — and the consent died there with a 401 the
app never heard about. A custom scheme is caught on every iOS, with no verification.

**The custom scheme is safe for a consent in a way it was not for sign-in.** Any app may
register `motet`, but a consent's code is finished only with an allowlisted Motet session,
and redeemed with a PKCE verifier (and, for Google, a client secret) that never leaves the
API — so a code another app intercepted is worth nothing to it.

It needs `MOTET_IOS_APP_DOMAIN` signed into the build (the web app whose callback page does
the handoff), a web app deployed with that page, and the app on **the build's own server**:
a server changed under Advanced has a different web app. Where the build knows no web app
the screen says so and offers the web app's Sources screen instead. A refusal is told apart
from a person's Cancel by the same one-second rule the sign-in uses, and a refused mailbox
connect removes the row `/v1/sources/connect` made for it, since nobody ever saw Google's
page. Every ending of a consent is `ConsentFlow`, in MotetKit, and tested.

## Configuration

**Sign in with Google is the app's front door, and the only way in.** Nothing but the
sign-in screen renders until a session exists (Tadas, 2026-09-19). It opens the web sign-in
in the system sign-in sheet, which returns a one-time code — on the verified https link
where the deployment serves one, else on `motet://signed-in` — and the app redeems it with
its PKCE verifier for an ordinary thirty-day session (AGENTS.md, "The phone signs in through
the web sign-in"). The session is kept in the Keychain, on this device only, and **Sign
out** revokes it on the server.

**There is no pasted API token any more.** It was a non-expiring, owner-equivalent
credential on a device that can be lost; the first launch of a build without it removes
one an earlier build stored, and says so on the sign-in screen. `MOTET_API_TOKEN` still
works against the API — the feed, scripts — just not typed into the phone. No token is ever
baked in either, because a default token would be a credential in every copy of the binary.

**A sign-in window iOS refuses is never silent.** `ASWebAuthenticationSession` reports a
refusal with the same `canceledLogin` code as a person pressing Cancel, so a "cancel" inside
`NativeSignIn.refusalWindow` (one second — nobody loads Google's page and dismisses it that
fast) is treated as a refusal: the https attempt falls back to a fresh sign-in on the scheme,
and a scheme attempt says "The sign-in window didn't open". The detail is in the `signin`
log category. The case that surfaced it: a build installed before the web app served its
association file, which iOS checks at install and update time, so the https callback stays
refused on that device until the next install.

The sign-in needs the deployment's web app to exist (`MOTET_APP_BASE_URL`), because Google
returns to the web app's registered callback. The `motet` scheme needs no `Info.plist`
registration: the sheet watches for it itself, and nothing else in the app handles it.

The server URL is under **Advanced**, on the sign-in screen and in Settings, and is rarely
touched: a TestFlight build arrives with it prefilled, and other builds ask for it there.
Changing it signs the phone out, because a session belongs to the server that issued it.
`MotetDefaultBaseURL` in `Info.plist` comes from the `MOTET_DEFAULT_API_BASE_URL` build
setting, which is empty in this repo and in CI. Beside it, `MotetBuildEnvironment` says
*which deployment* that server is — a label, never a hostname, which is what lets it have a
value here when the URL cannot (see "A build says which deployment it is for"). The TestFlight workflow fills it from the
`testflight` environment's `MOTET_IOS_API_BASE_URL` variable, so no host is written in this
repo's files. The variable holds the product's public API name, which the public SPA already
serves in its `config.js`. It is not masked, and it shows in the workflow's logs. Never set
it to an internal address such as a `*.run.app` URL. Only an `https://` value is honoured,
and a URL saved in Settings always wins.

## Distribution: TestFlight

`.github/workflows/testflight.yml` archives, signs, uploads, and waits until App Store
Connect has *processed* the build, because an upload Xcode calls successful can still fail
processing. It runs `ios/bin/testflight upload`. `ios/bin/testflight check` is the
credential-free half of the same script, and the `ios` CI job runs it on every PR.

**It runs on every push to `main` that touches the app under `ios/` or the workflow
itself**, so a merged iOS PR reaches TestFlight once the run and Apple's processing finish,
with nobody dispatching anything. This README and `Tests/` are excluded; a commit that
touches nothing else here uploads nothing. The app is built entirely from this directory,
including the generated Swift client, which `bin/ci` keeps in step with `openapi.yaml`.
Several merges in a row upload the one already building plus the newest: GitHub replaces a
pending run with the next one (it shows as "cancelled") and never cancels an upload in
progress.

A dispatch still works — to re-upload after a failure, after changing a `testflight`
environment variable, or for the signing fallback. Re-upload with a dispatch rather than
Re-run on an old run, which rebuilds that run's commit under a lower build number:

```bash
gh workflow run testflight.yml --ref main          # an agent can do this; so can the Actions tab
gh workflow run testflight.yml --ref main -f signing=archive   # fallback, see below
```

**A build can now land before the API it needs.** The API behind `MOTET_IOS_API_BASE_URL`
runs whatever the private repo pins, and a TestFlight build no longer waits for anybody to
check. A field the app adds to what it decodes must therefore be optional with a default, or
the build fails on the phone until that API is deployed — see the ordering constraint under
"Playback position is cross-device".

**Signing happens in Apple's cloud, at export.** The archive is unsigned. `-exportArchive`
with `-allowProvisioningUpdates` and an App Store Connect API key signs it with the team's
cloud-managed distribution certificate and creates the App Store profile. So the whole
credential is one key: no .p12, no profile, no keychain on the runner. If Apple ever refuses
export-time signing of an unsigned archive, `signing=archive` signs during the archive
instead. It works from the same key, but mints a development certificate per run, so it
is the fallback rather than the default. Apple caps a team's development certificates, so
a run of fallback builds eventually needs old ones revoked in Certificates, IDs & Profiles.
If the first real run shows export-time signing is refused, make `archive` the default
rather than living on the fallback.

**Build numbers** are `run_number.run_attempt`: unique, including for a re-run, and
increasing in the order runs are created — `run_number` is one counter per workflow, shared
by push and dispatch runs. The marketing version is `MARKETING_VERSION` in the project. Bump it for a
release that should read differently in TestFlight.

**What a human does, once** (invariant 9). Each step unblocks the next:

1. Accept any pending agreement at developer.apple.com and in App Store Connect →
   Business. Uploads are refused while one is pending.
2. Register the App ID `com.getmotet.app` (Certificates, IDs & Profiles → Identifiers).
   Tick **Associated Domains** — that is the https sign-in handoff, and it is granted by the
   tick rather than by review. Background audio is not a capability. Do **not** tick CarPlay:
   it is granted by manual review, and an ungranted entitlement fails a build to *sign*.
3. Create the App Store Connect app record for that bundle id.
4. Create a Team API key with **Admin** access (Users and Access → Integrations → App Store
   Connect API). Admin is what cloud-managed signing needs.
5. In the `testflight` GitHub environment, add the secrets `APP_STORE_CONNECT_API_KEY_ID`,
   `APP_STORE_CONNECT_API_ISSUER_ID` and `APP_STORE_CONNECT_API_KEY_P8` (the whole .p8
   file), and the variables `APPLE_TEAM_ID` and `MOTET_IOS_APP_DOMAIN` (the web app's bare
   host, e.g. `app.example.com`). `MOTET_IOS_API_BASE_URL` is already there. Leaving
   `MOTET_IOS_APP_DOMAIN` unset is supported and ships the `motet://` handoff instead.
6. After the first build processes, add testers under TestFlight → Internal Testing.
   Internal testers are offered every processed build automatically. An *external* group
   needs each build added to it, and its first build reviewed by Apple, so it does not
   follow `main` on its own.

Before it spends ten minutes archiving, the workflow checks that the variables are set and
that the key can see the app record (`app_store_connect.py preflight`). A pending
agreement shows up there as a 403. A wrong team id is only caught at export.

**The https sign-in handoff needs all three of its parts** (AGENTS.md, "The handoff comes
back on a verified https link"). `MOTET_IOS_APP_DOMAIN` here signs the entitlement into the
build; `MOTET_IOS_APP_ID` on the web image makes the container serve the app-site-association
file; `MOTET_IOS_APP_LINK=1` on the API makes it hand back the https link. Any of them
missing falls back to `motet://signed-in`, which still works, and the order they are set in
is safe: the app declares what it can receive at the start of every sign-in and the API
agrees or falls back, so no combination leaves a sheet waiting for a link nobody sends.

**What the signed build asks for is `webcredentials`, not `applinks`** — an https callback
to `ASWebAuthenticationSession` is verified through shared web credentials rather than as a
universal link. The App ID capability is still called Associated Domains, so step 2 above is
unchanged.

**Verify the first signed build by hand.** With the default `MOTET_SIGNING=export` the
archive is unsigned and the entitlement is materialised by Apple at export, so nothing in
this repo can prove it survived. `codesign -d --entitlements :- Motet.app` on the exported
build is the check. It is worth doing once: the app reads `MotetAppLinkDomain` out of its
Info.plist to decide it is entitled, and that key is set from a build setting entirely
independently of whether the entitlement was signed in — though a build that is wrong about
this now falls back to the scheme rather than failing, because a refused https callback is
retried on the scheme.

**The app icon is the brand mark** from the Polyphony restyle
([#115](https://github.com/tadasant/motet/pull/115)),
`App/Motet/Assets.xcassets/AppIcon.appiconset/AppIcon.png`. Whoever replaces it must keep it
1024×1024 with no alpha channel, or App Store Connect rejects the upload. `testflight check`
catches a missing icon, but not an alpha channel.
