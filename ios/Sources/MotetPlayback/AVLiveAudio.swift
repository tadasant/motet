import Foundation
import MotetKit

#if canImport(AVFoundation) && os(iOS)
import AVFoundation
import os

/// Play Live's audio on the phone: the open microphone, and the replies.
///
/// **The session changes shape for the length of Play Live and changes back.** Listening is
/// `.playback` / `.spokenAudio` / `.longFormAudio` (`AudioSessionController`), which cannot
/// record. Play Live needs the microphone while the briefing plays, so it switches to
/// `.playAndRecord`, and `stopCapture` hands the listening session back. The narration keeps
/// playing through `AVPlayer` across the switch.
///
/// **Echo is the thing to watch**, as it is on the web, where the SPA leans on the browser's
/// echo cancellation and recommends headphones. Here the input node's voice processing is
/// switched on — Apple's echo canceller — and *every* reply, streamed `pcm16` and the
/// composed arm's whole MP3 alike, is played through the same engine, so the canceller has
/// it as its reference. Whether it also removes the narration coming out of the speaker is a
/// device question nothing in this repo can answer. Headphones sidestep it, and the screen
/// says so.
///
/// **The engine can stop under a session** — AirPods connecting, a phone call — and a player
/// node told to play on a stopped engine raises an Objective-C exception. So every node call
/// is guarded on the engine running, a configuration change rebuilds the tap and restarts
/// the engine, and one that cannot be restarted is reported through `failed` so the session
/// ends and says why rather than going quietly deaf.
///
/// **Unverified here**, like everything in this target: see `ios/README.md`.
public final class AVLiveAudio: LiveAudio, @unchecked Sendable {
    public enum Failure: Error, CustomStringConvertible {
        case microphoneDenied
        case noInput

        public var description: String {
            switch self {
            case .microphoneDenied:
                return "Microphone access is off for Motet. Turn it on in Settings → Motet to use Play Live."
            case .noInput:
                return "No microphone is available."
            }
        }
    }

    private static let logger = Logger(subsystem: "com.getmotet.app", category: "live-audio")

    private let lock = NSLock()
    private var engine: AVAudioEngine?
    private var player: AVAudioPlayerNode?
    private var playerFormat: AVAudioFormat?
    private var pendingSeconds: Double = 0
    /// Bumped by every flush, so a completion from audio that was cut off does not subtract
    /// from what is queued now, and a container reply that was cut off reports `false`.
    private var flushGeneration = 0
    private var observers: [NSObjectProtocol] = []
    private var sinks: Sinks?
    /// Which capture this is: a stop, or a second start, moves it on, so a start still
    /// waiting for the permission prompt does not open a mic nobody wants.
    private var captureGeneration = 0
    private let restoreListening: @Sendable () -> Void

    private struct Sinks: Sendable {
        let frames: @Sendable (Data) -> Void
        let level: @Sendable (Double) -> Void
        let failed: @Sendable (String) -> Void
    }

    /// `restoreListening` puts the listening session back — `AudioSessionController.configure`.
    public init(restoreListening: @escaping @Sendable () -> Void) {
        self.restoreListening = restoreListening
    }

    // MARK: - The microphone

    public func startCapture(
        frames: @escaping @Sendable (Data) -> Void,
        level: @escaping @Sendable (Double) -> Void,
        failed: @escaping @Sendable (String) -> Void
    ) async throws {
        // One capture at a time: an older one still open is closed first, so its tap cannot
        // keep the mic on after its session is gone.
        await stopCapture()
        let mine = lock.withLock { () -> Int in
            captureGeneration += 1
            return captureGeneration
        }
        guard await AVAudioApplication.requestRecordPermission() else { throw Failure.microphoneDenied }
        guard lock.withLock({ captureGeneration == mine }) else { return }

        let session = AVAudioSession.sharedInstance()
        do {
            // `.allowBluetooth` lets AirPods be the mic, which moves them to the hands-free
            // profile: call-quality audio, the briefing included, for as long as Live runs.
            // The alternative is the phone's own mic in a pocket. See ios/README.md.
            try session.setCategory(
                .playAndRecord,
                mode: .default,
                options: [.defaultToSpeaker, .allowBluetooth, .allowBluetoothA2DP]
            )
            try session.setActive(true)
        } catch {
            restoreListening()
            throw error
        }

        let engine = AVAudioEngine()
        let player = AVAudioPlayerNode()
        let sinks = Sinks(frames: frames, level: level, failed: failed)
        do {
            try configure(engine: engine, player: player, sinks: sinks)
        } catch {
            engine.inputNode.removeTap(onBus: 0)
            restoreListening()
            throw error
        }
        lock.withLock {
            self.engine = engine
            self.player = player
            self.sinks = sinks
            self.pendingSeconds = 0
        }
        observe(engine)
    }

    /// Tap the input, attach the reply player, start. Also what a configuration change runs
    /// again, because the input's format may be different afterwards.
    private func configure(engine: AVAudioEngine, player: AVAudioPlayerNode, sinks: Sinks) throws {
        let input = engine.inputNode
        // Apple's echo canceller. A failure here is not fatal — the mic still works, and the
        // detector has its SNR gate — so it is tried and not required.
        try? input.setVoiceProcessingEnabled(true)
        // Voice processing ducks every other sound by default, the briefing included; the
        // narration is what is being listened to, so keep that to the minimum.
        input.voiceProcessingOtherAudioDuckingConfiguration = .init(
            enableAdvancedDucking: false, duckingLevel: .min
        )
        let format = input.outputFormat(forBus: 0)
        guard format.sampleRate > 0, format.channelCount > 0 else { throw Failure.noInput }
        let meter = MeterThrottle()
        let inputRate = format.sampleRate
        input.removeTap(onBus: 0)
        input.installTap(onBus: 0, bufferSize: 1_024, format: format) { buffer, _ in
            guard let channel = buffer.floatChannelData?[0] else { return }
            let samples = Array(UnsafeBufferPointer(start: channel, count: Int(buffer.frameLength)))
            sinks.frames(LiveAudioFormat.pcm16(from: samples, sampleRate: inputRate))
            if meter.tick() { sinks.level(LiveAudioFormat.dbfs(samples)) }
        }
        if player.engine == nil { engine.attach(player) }
        let replyFormat = lock.withLock { playerFormat } ?? Self.monoFormat(sampleRate: 24_000)
        engine.connect(player, to: engine.mainMixerNode, format: replyFormat)
        lock.withLock { playerFormat = replyFormat }
        engine.prepare()
        try engine.start()
        player.play()
    }

    /// A route change or an interruption stops the engine. Rebuild and restart it; if that is
    /// not possible, say so, once.
    private func observe(_ engine: AVAudioEngine) {
        let center = NotificationCenter.default
        let restart: @Sendable (Notification) -> Void = { [weak self] _ in self?.restartIfStopped() }
        let added = [
            center.addObserver(forName: .AVAudioEngineConfigurationChange, object: engine, queue: nil, using: restart),
            center.addObserver(
                forName: AVAudioSession.interruptionNotification, object: AVAudioSession.sharedInstance(), queue: nil
            ) { [weak self] note in
                guard let raw = note.userInfo?[AVAudioSessionInterruptionTypeKey] as? UInt,
                      AVAudioSession.InterruptionType(rawValue: raw) == .ended else { return }
                self?.restartIfStopped()
            },
        ]
        lock.withLock { observers = added }
    }

    private func restartIfStopped() {
        let current = lock.withLock { () -> (AVAudioEngine, AVAudioPlayerNode, Sinks)? in
            guard let engine, let player, let sinks else { return nil }
            return (engine, player, sinks)
        }
        guard let (engine, player, sinks) = current, !engine.isRunning else { return }
        // What was queued is gone with the old configuration.
        lock.withLock {
            pendingSeconds = 0
            flushGeneration += 1
        }
        do {
            try configure(engine: engine, player: player, sinks: sinks)
            Self.logger.notice("live audio engine restarted after a configuration change")
        } catch {
            Self.logger.error("live audio engine could not restart: \(error.localizedDescription, privacy: .public)")
            sinks.failed("The microphone stopped when the audio route changed. Start Play Live again.")
        }
    }

    public func stopCapture() async {
        let (engine, player, observers) = lock.withLock {
            () -> (AVAudioEngine?, AVAudioPlayerNode?, [NSObjectProtocol]) in
            defer {
                self.engine = nil
                self.player = nil
                self.sinks = nil
                self.observers = []
                self.pendingSeconds = 0
                self.flushGeneration += 1
                self.captureGeneration += 1
            }
            return (self.engine, self.player, self.observers)
        }
        observers.forEach(NotificationCenter.default.removeObserver)
        guard let engine else { return }
        engine.inputNode.removeTap(onBus: 0)
        player?.stop()
        engine.stop()
        restoreListening()
    }

    // MARK: - Replies

    public func enqueue(pcm16: Data, sampleRate: Int) async {
        let samples = LiveAudioFormat.floats(fromPCM16: pcm16)
        guard !samples.isEmpty, sampleRate > 0,
              let buffer = AVAudioPCMBuffer(
                  pcmFormat: Self.monoFormat(sampleRate: Double(sampleRate)),
                  frameCapacity: AVAudioFrameCount(samples.count)
              ),
              let channel = buffer.floatChannelData?[0]
        else { return }
        samples.withUnsafeBufferPointer { source in
            channel.update(from: source.baseAddress!, count: samples.count)
        }
        buffer.frameLength = AVAudioFrameCount(samples.count)
        _ = schedule(buffer)
    }

    /// The composed arm's reply: a whole MP3 (or the fake's WAV), decoded and played through
    /// the engine like a streamed one, so the echo canceller hears it too.
    public func playContainer(_ data: Data) async throws -> Bool {
        // The extension is the decoder's hint to the container, so it is named for what is
        // inside rather than left off.
        let isWave = data.prefix(4) == Data([0x52, 0x49, 0x46, 0x46])
        let file = FileManager.default.temporaryDirectory
            .appendingPathComponent("motet-live-reply-\(UUID().uuidString).\(isWave ? "wav" : "mp3")")
        try data.write(to: file)
        defer { try? FileManager.default.removeItem(at: file) }
        let audio = try AVAudioFile(forReading: file)
        guard let buffer = AVAudioPCMBuffer(
            pcmFormat: audio.processingFormat, frameCapacity: AVAudioFrameCount(audio.length)
        ) else { return false }
        try audio.read(into: buffer)
        guard let scheduled = schedule(buffer) else { return false }
        return await withCheckedContinuation { continuation in
            scheduled.onPlayed { continuation.resume(returning: $0) }
        }
    }

    public func flushReplies() async {
        let player = lock.withLock { () -> AVAudioPlayerNode? in
            flushGeneration += 1
            pendingSeconds = 0
            return self.player
        }
        guard let player, player.engine?.isRunning == true else { return }
        player.stop()
        player.play()
    }

    public func pendingReplySeconds() async -> Double {
        lock.withLock { pendingSeconds }
    }

    // MARK: - Internals

    private static func monoFormat(sampleRate: Double) -> AVAudioFormat {
        AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: sampleRate, channels: 1, interleaved: false)!
    }

    /// Queue a buffer behind whatever is playing, reconnecting the player when the format
    /// differs from the last reply's. Nil when there is no running engine to play it on.
    private func schedule(_ buffer: AVAudioPCMBuffer) -> Scheduled? {
        // Node operations happen outside the lock: stopping a player node runs the
        // completion handlers of what it had queued, and those take the lock.
        let state = lock.withLock { () -> (AVAudioEngine, AVAudioPlayerNode, Bool)? in
            guard let engine, let player else { return nil }
            let reconnect = playerFormat.map { !$0.isEqual(buffer.format) } ?? true
            if reconnect {
                playerFormat = buffer.format
                pendingSeconds = 0
                flushGeneration += 1
            }
            return (engine, player, reconnect)
        }
        guard let (engine, player, reconnect) = state, engine.isRunning else { return nil }
        if reconnect {
            player.stop()
            engine.disconnectNodeOutput(player)
            engine.connect(player, to: engine.mainMixerNode, format: buffer.format)
            player.play()
        }
        let seconds = Double(buffer.frameLength) / buffer.format.sampleRate
        let generation = lock.withLock { () -> Int in
            pendingSeconds += seconds
            return flushGeneration
        }
        let scheduled = Scheduled()
        player.scheduleBuffer(buffer, completionCallbackType: .dataPlayedBack) { [weak self] _ in
            let stillCurrent = self?.played(seconds, generation: generation) ?? false
            scheduled.finish(stillCurrent)
        }
        return scheduled
    }

    /// Returns whether the audio was still current — played out, rather than flushed.
    private func played(_ seconds: Double, generation: Int) -> Bool {
        lock.withLock {
            guard generation == flushGeneration else { return false }
            pendingSeconds = max(0, pendingSeconds - seconds)
            return true
        }
    }
}

/// A scheduled buffer's end, handed to whoever waits for it — exactly once, whichever of the
/// completion and the waiter arrives first.
private final class Scheduled: @unchecked Sendable {
    private let lock = NSLock()
    private var result: Bool?
    private var waiter: ((Bool) -> Void)?

    func finish(_ played: Bool) {
        let waiter = lock.withLock { () -> ((Bool) -> Void)? in
            guard result == nil else { return nil }
            result = played
            defer { self.waiter = nil }
            return self.waiter
        }
        waiter?(played)
    }

    func onPlayed(_ callback: @escaping (Bool) -> Void) {
        let ready = lock.withLock { () -> Bool? in
            if let result { return result }
            waiter = callback
            return nil
        }
        if let ready { callback(ready) }
    }
}

/// About four meter readings a second from a tap that fires ~45 times a second.
private final class MeterThrottle: @unchecked Sendable {
    private let lock = NSLock()
    private var count = 0

    func tick() -> Bool {
        lock.withLock {
            count += 1
            return count % 12 == 0
        }
    }
}
#endif
