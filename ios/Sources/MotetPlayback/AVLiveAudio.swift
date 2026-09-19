import Foundation
import MotetKit

#if canImport(AVFoundation) && os(iOS)
import AVFoundation

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
/// switched on — Apple's echo canceller — and the replies are played through the same
/// engine, so they are cancelled as their own output; whether the canceller also removes the
/// narration coming out of the speaker is a device question nothing in this repo can answer.
/// Headphones sidestep it, and the screen says so.
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

    private let lock = NSLock()
    private var engine: AVAudioEngine?
    private var player: AVAudioPlayerNode?
    private var playerRate: Double = 0
    private var pendingSeconds: Double = 0
    /// Bumped by every flush, so a completion from audio that was cut off does not subtract
    /// from what is queued now.
    private var flushGeneration = 0
    private var container: AVAudioPlayer?
    private var containerDelegate: ContainerDelegate?
    private let restoreListening: @Sendable () -> Void

    /// `restoreListening` puts the listening session back — `AudioSessionController.configure`.
    public init(restoreListening: @escaping @Sendable () -> Void) {
        self.restoreListening = restoreListening
    }

    // MARK: - The microphone

    public func startCapture(
        frames: @escaping @Sendable (Data) -> Void,
        level: @escaping @Sendable (Double) -> Void
    ) async throws {
        guard await AVAudioApplication.requestRecordPermission() else { throw Failure.microphoneDenied }

        let session = AVAudioSession.sharedInstance()
        try session.setCategory(
            .playAndRecord,
            mode: .default,
            options: [.defaultToSpeaker, .allowBluetooth, .allowBluetoothA2DP]
        )
        try session.setActive(true)

        let engine = AVAudioEngine()
        let input = engine.inputNode
        // Apple's echo canceller. A failure here is not fatal — the mic still works, and the
        // detector has the SNR gate — so it is tried and not required.
        try? input.setVoiceProcessingEnabled(true)
        // Voice processing ducks every other sound by default, the briefing included; the
        // narration is the thing being listened to, so keep that to the minimum.
        input.voiceProcessingOtherAudioDuckingConfiguration = .init(
            enableAdvancedDucking: false, duckingLevel: .min
        )

        let format = input.outputFormat(forBus: 0)
        guard format.sampleRate > 0, format.channelCount > 0 else { throw Failure.noInput }
        let meter = MeterThrottle()
        let inputRate = format.sampleRate
        input.installTap(onBus: 0, bufferSize: 1_024, format: format) { buffer, _ in
            guard let channel = buffer.floatChannelData?[0] else { return }
            let samples = Array(UnsafeBufferPointer(start: channel, count: Int(buffer.frameLength)))
            frames(LiveAudioFormat.pcm16(from: samples, sampleRate: inputRate))
            if meter.tick() { level(LiveAudioFormat.dbfs(samples)) }
        }

        let player = AVAudioPlayerNode()
        engine.attach(player)
        engine.connect(player, to: engine.mainMixerNode, format: Self.replyFormat(sampleRate: 24_000))
        engine.prepare()
        do {
            try engine.start()
        } catch {
            input.removeTap(onBus: 0)
            restoreListening()
            throw error
        }
        player.play()
        lock.withLock {
            self.engine = engine
            self.player = player
            self.playerRate = 24_000
            self.pendingSeconds = 0
        }
    }

    public func stopCapture() async {
        let (engine, player) = lock.withLock { () -> (AVAudioEngine?, AVAudioPlayerNode?) in
            defer {
                self.engine = nil
                self.player = nil
                self.pendingSeconds = 0
                self.flushGeneration += 1
            }
            return (self.engine, self.player)
        }
        stopContainer()
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
              let (player, generation) = playerReady(for: Double(sampleRate)),
              let buffer = AVAudioPCMBuffer(
                  pcmFormat: Self.replyFormat(sampleRate: Double(sampleRate)),
                  frameCapacity: AVAudioFrameCount(samples.count)
              ),
              let channel = buffer.floatChannelData?[0]
        else { return }
        samples.withUnsafeBufferPointer { source in
            channel.update(from: source.baseAddress!, count: samples.count)
        }
        buffer.frameLength = AVAudioFrameCount(samples.count)
        let seconds = Double(samples.count) / Double(sampleRate)
        lock.withLock { pendingSeconds += seconds }
        player.scheduleBuffer(buffer, completionCallbackType: .dataPlayedBack) { [weak self] _ in
            self?.played(seconds, generation: generation)
        }
    }

    public func playContainer(_ data: Data) async throws -> Bool {
        stopContainer()
        let audio = try AVAudioPlayer(data: data)
        let seconds = audio.duration
        return await withCheckedContinuation { continuation in
            let delegate = ContainerDelegate { [weak self] finished in
                self?.lock.withLock {
                    self?.pendingSeconds = max(0, (self?.pendingSeconds ?? 0) - seconds)
                    self?.container = nil
                    self?.containerDelegate = nil
                }
                continuation.resume(returning: finished)
            }
            audio.delegate = delegate
            lock.withLock {
                container = audio
                containerDelegate = delegate
                pendingSeconds += seconds
            }
            if !audio.play() { delegate.finish(false) }
        }
    }

    public func flushReplies() async {
        let player = lock.withLock { () -> AVAudioPlayerNode? in
            flushGeneration += 1
            pendingSeconds = 0
            return self.player
        }
        stopContainer()
        guard let player else { return }
        player.stop()
        player.play()
    }

    public func pendingReplySeconds() async -> Double {
        lock.withLock { pendingSeconds }
    }

    // MARK: - Internals

    private static func replyFormat(sampleRate: Double) -> AVAudioFormat {
        AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: sampleRate, channels: 1, interleaved: false)!
    }

    /// The player node, connected at `sampleRate` — reconnected if a reply arrives at a rate
    /// the last one did not use.
    private func playerReady(for sampleRate: Double) -> (AVAudioPlayerNode, Int)? {
        // Node operations happen outside the lock: stopping a player node runs the
        // completion handlers of what it had queued, and those take the lock.
        let state = lock.withLock { () -> (AVAudioEngine, AVAudioPlayerNode, Bool, Int)? in
            guard let engine, let player else { return nil }
            let reconnect = playerRate != sampleRate
            if reconnect {
                playerRate = sampleRate
                pendingSeconds = 0
                flushGeneration += 1
            }
            return (engine, player, reconnect, flushGeneration)
        }
        guard let (engine, player, reconnect, generation) = state else { return nil }
        if reconnect {
            player.stop()
            engine.disconnectNodeOutput(player)
            engine.connect(player, to: engine.mainMixerNode, format: Self.replyFormat(sampleRate: sampleRate))
            player.play()
        }
        return (player, generation)
    }

    private func played(_ seconds: Double, generation: Int) {
        lock.withLock {
            guard generation == flushGeneration else { return }
            pendingSeconds = max(0, pendingSeconds - seconds)
        }
    }

    private func stopContainer() {
        let (audio, delegate) = lock.withLock { () -> (AVAudioPlayer?, ContainerDelegate?) in
            (container, containerDelegate)
        }
        audio?.stop()
        delegate?.finish(false)
    }
}

/// Resumes a container reply's continuation exactly once — on its end, or on a flush.
private final class ContainerDelegate: NSObject, AVAudioPlayerDelegate, @unchecked Sendable {
    private let lock = NSLock()
    private var done: ((Bool) -> Void)?

    init(_ done: @escaping (Bool) -> Void) {
        self.done = done
    }

    func finish(_ finished: Bool) {
        let callback = lock.withLock { () -> ((Bool) -> Void)? in
            defer { done = nil }
            return done
        }
        callback?(finished)
    }

    func audioPlayerDidFinishPlaying(_ player: AVAudioPlayer, successfully flag: Bool) {
        finish(flag)
    }

    func audioPlayerDecodeErrorDidOccur(_ player: AVAudioPlayer, error: Error?) {
        finish(false)
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
