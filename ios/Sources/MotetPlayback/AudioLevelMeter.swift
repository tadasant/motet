import Foundation
import MotetKit

#if canImport(AVFoundation) && canImport(MediaToolbox)
import AVFoundation
import MediaToolbox
import os

/// **A number for "is sound coming out", measured off the audio the player is rendering.**
///
/// Everything else the app can say about playback is the player's own account of itself,
/// and the failure that motivated all of this is precisely a player whose account is
/// wrong: `timeControlStatus` said `.playing` on a device where nothing was audible. The
/// only observation that is not the player's opinion is the samples, so this taps them.
///
/// `MTAudioProcessingTap` on the item's audio mix is how AVFoundation offers that. The tap
/// sits `PostEffects`, so what it measures is what the mix produces — which is one stage
/// before the route, and that is the honest limit of this measurement: **a non-zero level
/// proves the pipeline is rendering audio, not that a speaker moved.** The route is
/// reported beside it (``AudioRoute``) because the other half of "silent phone" is a
/// session iOS refused, and the two together are what a reader needs.
///
/// **It never makes playback worse, and three things are shaped by that rule.** The tap is
/// attached off the critical path, so a slow track load delays no audio; a format it
/// cannot measure is reported as *unmeasured* rather than as silence; and every failure
/// along the way — no audio track, a tap the system would not create, an unexpected sample
/// format — leaves ``level()`` answering nil, which ``PlaybackProbe/isAudible`` treats as
/// an abstention rather than a verdict.
public final class AudioLevelMeter: @unchecked Sendable {
    private static let logger = Logger(subsystem: "com.getmotet.app", category: "playback-probe")

    /// Half the reporting window. A measurement spans the current bucket plus the previous
    /// one, so it covers between one and two of these — long enough that an ordinary pause
    /// between words does not read as silence, short enough that a `pause()` is visible in
    /// about a second, which is what a UI test waits for.
    private static let halfWindow: TimeInterval = 0.5

    private let lock = NSLock()
    private var current = Bucket()
    private var previous = Bucket()
    private var currentStart = Date.distantPast
    /// Whether any buffer has *ever* arrived. Until one has, the tap is installed and
    /// unproven, and "no frames" means "nothing measured" rather than "silence" — the
    /// distinction ``PlaybackProbe/isAudible`` abstains on.
    private var hasEverReceived = false
    /// Set by the prepare callback when the stream is one this can read. A format it
    /// cannot read is not a failure to report loudly; it is a reason to abstain.
    private var isMeasurableFormat = false

    private struct Bucket {
        var peak: Double = 0
        var sumSquares: Double = 0
        var frames: Int = 0

        mutating func clear() { self = Bucket() }
    }

    public init() {}

    /// What the tap measured over its most recent window, or nil where nothing measured.
    ///
    /// The window rolls **here as well as on arrival**, which is the half that is easy to
    /// miss: a paused player delivers no buffers at all, so a window advanced only by the
    /// process callback would freeze at the last loud measurement and report sound coming
    /// out of a stopped player forever.
    public func level() -> AudioLevel? {
        lock.withLock {
            roll(at: Date())
            guard hasEverReceived, isMeasurableFormat else { return nil }
            let frames = current.frames + previous.frames
            let peak = max(current.peak, previous.peak)
            let sumSquares = current.sumSquares + previous.sumSquares
            let rms = frames > 0 ? (sumSquares / Double(frames)).squareRoot() : 0
            return AudioLevel(rms: rms, peak: peak, frames: frames)
        }
    }

    /// Forget everything measured so far — a new item is a new measurement.
    public func reset() {
        lock.withLock {
            current.clear()
            previous.clear()
            currentStart = .distantPast
            hasEverReceived = false
            isMeasurableFormat = false
        }
    }

    /// Build the audio mix that carries the tap, for an asset's first audio track.
    ///
    /// Answers nil — rather than throwing — for every way this can not happen: an asset
    /// with no audio track, a tap the system refuses to create, a load that failed. The
    /// caller's only sane response to each is the same: carry on playing and measure
    /// nothing.
    public func audioMix(for asset: AVAsset) async -> AVAudioMix? {
        guard let track = try? await asset.loadTracks(withMediaType: .audio).first else {
            Self.logger.notice("audio level: no audio track to tap")
            return nil
        }
        // Held for the tap's lifetime by `passRetained`, released by the finalize callback.
        // The tap outlives this scope and is owned by the mix, so ARC cannot be what keeps
        // the meter alive.
        let clientInfo = UnsafeMutableRawPointer(Unmanaged.passRetained(self).toOpaque())

        // Local constants rather than file-scope ones: a `@convention(c)` function stored
        // in a global would be a global mutable-ish binding for Swift 6's concurrency
        // checker to have an opinion about, and these need none.
        let tapInit: MTAudioProcessingTapInitCallback = { _, info, storageOut in
            storageOut.pointee = info
        }
        let tapFinalize: MTAudioProcessingTapFinalizeCallback = { tap in
            // Balances the `passRetained` above. The tap owns this reference for its whole
            // life, so ARC is not what keeps the meter alive while audio is flowing.
            Unmanaged<AudioLevelMeter>.fromOpaque(MTAudioProcessingTapGetStorage(tap)).release()
        }
        let tapPrepare: MTAudioProcessingTapPrepareCallback = { tap, _, format in
            let meter = Unmanaged<AudioLevelMeter>
                .fromOpaque(MTAudioProcessingTapGetStorage(tap))
                .takeUnretainedValue()
            meter.prepare(format: format.pointee)
        }
        let tapProcess: MTAudioProcessingTapProcessCallback = {
            tap, frames, _, bufferList, framesOut, flagsOut in
            let status = MTAudioProcessingTapGetSourceAudio(
                tap, frames, bufferList, flagsOut, nil, framesOut
            )
            guard status == noErr else { return }
            let meter = Unmanaged<AudioLevelMeter>
                .fromOpaque(MTAudioProcessingTapGetStorage(tap))
                .takeUnretainedValue()
            meter.accumulate(bufferList, frames: Int(framesOut.pointee))
        }

        var callbacks = MTAudioProcessingTapCallbacks(
            version: kMTAudioProcessingTapCallbacksVersion_0,
            clientInfo: clientInfo,
            init: tapInit,
            finalize: tapFinalize,
            prepare: tapPrepare,
            unprepare: nil,
            process: tapProcess
        )
        var created: Unmanaged<MTAudioProcessingTap>?
        let status = MTAudioProcessingTapCreate(
            kCFAllocatorDefault,
            &callbacks,
            // PostEffects: what the mix produces, which is the closest to the ear this
            // API reaches.
            kMTAudioProcessingTapCreationFlag_PostEffects,
            &created
        )
        guard status == noErr, let created else {
            // The retain above is ours to undo: with no tap, no finalize callback will
            // ever run to release it.
            Unmanaged<AudioLevelMeter>.fromOpaque(clientInfo).release()
            Self.logger.error("audio level: MTAudioProcessingTapCreate failed (\(status))")
            return nil
        }
        let parameters = AVMutableAudioMixInputParameters(track: track)
        parameters.audioTapProcessor = created.takeRetainedValue()
        let mix = AVMutableAudioMix()
        mix.inputParameters = [parameters]
        return mix
    }

    // MARK: - Called from the audio thread

    private func prepare(format: AudioStreamBasicDescription) {
        // Only 32-bit float is read, because that is what the mix hands a tap and because
        // guessing at an integer layout is how a meter ends up reporting confident
        // nonsense. Anything else abstains.
        let isFloat = format.mFormatFlags & kAudioFormatFlagIsFloat != 0
        let is32Bit = format.mBitsPerChannel == 32
        lock.withLock { isMeasurableFormat = isFloat && is32Bit }
        if !(isFloat && is32Bit) {
            Self.logger.notice(
                "audio level: unmeasured format (flags=\(format.mFormatFlags) bits=\(format.mBitsPerChannel))"
            )
        }
    }

    /// Called on the real-time audio thread, so it does the arithmetic and nothing else —
    /// no allocation, no logging, and a lock held for the length of a few adds.
    private func accumulate(_ bufferList: UnsafeMutablePointer<AudioBufferList>, frames: Int) {
        guard frames > 0 else { return }
        var peak = 0.0
        var sumSquares = 0.0
        var counted = 0
        for buffer in UnsafeMutableAudioBufferListPointer(bufferList) {
            guard let raw = buffer.mData else { continue }
            let channels = max(1, Int(buffer.mNumberChannels))
            let available = Int(buffer.mDataByteSize) / MemoryLayout<Float>.size
            // Interleaved buffers carry every channel in one block; non-interleaved carry
            // one channel each. Either way the samples to read are what the buffer says it
            // holds, bounded by the frames the tap reported.
            let count = min(available, frames * channels)
            guard count > 0 else { continue }
            let samples = raw.bindMemory(to: Float.self, capacity: count)
            for index in 0..<count {
                let value = abs(Double(samples[index]))
                if value > peak { peak = value }
                sumSquares += value * value
            }
            counted += count
        }
        guard counted > 0 else { return }
        lock.withLock {
            roll(at: Date())
            hasEverReceived = true
            current.frames += counted
            current.sumSquares += sumSquares
            if peak > current.peak { current.peak = peak }
        }
    }

    /// Advance the two buckets. Caller holds the lock.
    private func roll(at now: Date) {
        let elapsed = now.timeIntervalSince(currentStart)
        guard elapsed >= Self.halfWindow else { return }
        // A gap longer than the whole window means nothing arrived for it, so the previous
        // bucket is stale too — which is what makes a paused player fall to zero rather
        // than keeping the last loud reading.
        previous = elapsed >= Self.halfWindow * 2 ? Bucket() : current
        current.clear()
        currentStart = now
    }
}
#endif
