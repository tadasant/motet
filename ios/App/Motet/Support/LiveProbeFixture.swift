#if DEBUG
import Foundation
import MotetKit

/// **Play Live on the playback-probe screen, with no voice service** (motet#152).
///
/// The audio readout has to be shown in Live mode, and Live mode is a running `LiveSession`.
/// A real one needs a signed-in session to mint it and a deployed voice service to talk to,
/// and an automated run has neither (an agent cannot sign in; AGENTS.md says why). So this
/// is the narrowest stand-in that keeps the thing being shown real: the **real**
/// `LiveSession`, pausing and resuming the **real** `PlaybackController` over the tone
/// `PlaybackProbeFixture` plays, with only its two outward edges scripted —
///
/// * the API answers the mint with a session on a socket that is never dialled, and
/// * the socket answers `authenticate` with a live `ready`, and a `barge_in` with an
///   `interrupted_at`, which is what the voice service does.
///
/// **The microphone is never opened and no reply is ever played.** `ProbeLiveAudio`
/// captures nothing and discards replies, because a CI simulator has no microphone and a
/// permission prompt would stop the run. That is honest about what the readout can show:
/// in Live mode the reply path is the one it calls `unmeasured`, so the fixture scripting
/// it silent proves nothing false. What the frames do show is the readout's two Live
/// answers — `audible` while the narration plays under a running session, `unmeasured`
/// once the session has paused it for a question.
enum LiveProbeFixture {
    /// `-MotetPlaybackProbeLive`, alongside `-MotetPlaybackProbe`: offer the Live controls.
    static var isRequested: Bool {
        ProcessInfo.processInfo.arguments.contains("-MotetPlaybackProbeLive")
    }

    static func makeSession(narration: any NarrationControl) -> LiveSession {
        let transport = ProbeLiveTransport()
        return LiveSession(
            api: ProbeVoiceAPI(),
            narration: narration,
            audio: ProbeLiveAudio(),
            makeTransport: { transport }
        )
    }
}

/// The mint. A URL that is never dialled: `ProbeLiveTransport` answers in-process.
private struct ProbeVoiceAPI: VoiceAPI {
    func voiceStatus() async throws -> VoiceStatusResponse {
        VoiceStatusResponse(configured: true)
    }

    func startVoiceSession(episodeId: String, spokenThroughMs: Int) async throws -> VoiceSessionResponse {
        VoiceSessionResponse(
            arm: "probe_fixture",
            authenticateFrame: .object(["type": .string("authenticate"), "token": .string("probe")]),
            conversational: true,
            expiresAt: "2099-01-01T00:00:00Z",
            sessionId: "probe",
            sessionToken: "probe",
            websocketUrl: "wss://probe.invalid/ws"
        )
    }
}

/// The voice service's two answers this fixture needs, and nothing else.
private final class ProbeLiveTransport: LiveTransport, @unchecked Sendable {
    private let lock = NSLock()
    private var continuation: AsyncStream<LiveTransportEvent>.Continuation?
    private var interruptions = 0

    func open(url: URL) -> AsyncStream<LiveTransportEvent> {
        AsyncStream { continuation in lock.withLock { self.continuation = continuation } }
    }

    func sendText(_ text: String) {
        let type = (try? JSONSerialization.jsonObject(with: Data(text.utf8)) as? [String: Any])?["type"] as? String
        switch type {
        case "authenticate":
            yield(#"{"type":"session_state","at_ms":0,"state":"ready","detail":"live conversation open (probe fixture)","live":true}"#)
        case "barge_in":
            let count = lock.withLock { () -> Int in
                interruptions += 1
                return interruptions
            }
            yield(#"{"type":"interrupted_at","at_ms":\#(count),"offset_ms":0,"decision":{"trigger":"button"}}"#)
        default:
            break
        }
    }

    func sendAudio(_ data: Data) {}

    func close() {
        let continuation = lock.withLock { () -> AsyncStream<LiveTransportEvent>.Continuation? in
            defer { self.continuation = nil }
            return self.continuation
        }
        continuation?.finish()
    }

    private func yield(_ json: String) {
        _ = lock.withLock { continuation }?.yield(.text(json))
    }
}

/// No microphone and no reply player — see `LiveProbeFixture`.
private struct ProbeLiveAudio: LiveAudio {
    func startCapture(
        frames: @escaping @Sendable (Data) -> Void,
        level: @escaping @Sendable (Double) -> Void,
        failed: @escaping @Sendable (String) -> Void
    ) async throws {}
    func stopCapture() async {}
    func enqueue(pcm16: Data, sampleRate: Int) async {}
    func playContainer(_ data: Data) async throws -> Bool { true }
    func flushReplies() async {}
    func pendingReplySeconds() async -> Double { 0 }
}
#endif
