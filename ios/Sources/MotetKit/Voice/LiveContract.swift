import Foundation

/// The voice service's socket, as the phone speaks it — hand-typed from
/// `voice/src/motet_voice/contract.py`, as the SPA's `Live.tsx` is, because the voice
/// service has no OpenAPI seam into either client. Nothing here names a vendor
/// (invariant 1): which provider answered is the service's business.
///
/// Keep in step with `contract.py` and `app.py`'s frame handling. An event type this build
/// does not know decodes as `.other` rather than failing, because the service deploys
/// without an App Store review and a new event must never take a session down.
public enum LiveEvent: Equatable, Sendable {
    /// `ready` / `listening` / `speaking` / `closed`. `reason` rides on the first `ready`
    /// of a session whose live channel did not open (`insufficient_quota`, `arm_dormant`, …);
    /// `live` says whether a speech-to-speech channel is open behind this state.
    case sessionState(state: String, detail: String?, reason: String?, live: Bool?)
    case transcript(speaker: String, text: String, final: Bool)
    /// Reply audio. `format` is `pcm16` (raw little-endian mono at `sampleRate`) or a
    /// container for a decoder — the composed arm sends Cartesia's MP3, the fake a WAV.
    case audioChunk(audio: Data, sampleRate: Int, durationMs: Int, format: String)
    case toolCall(name: String, arguments: String)
    case toolResult(name: String, ok: Bool, error: String?)
    /// The listener took the floor. `offsetMs` is the service's clock frozen at the
    /// decision — ours, never a provider's (invariant 4).
    case interruptedAt(offsetMs: Int, segmentTitle: String?, claimText: String?, decision: [String: JSONValue])
    case error(code: String, message: String)
    case other(type: String)

    private struct Wire: Decodable {
        let type: String
        let state: String?
        let detail: String?
        let reason: String?
        let live: Bool?
        let speaker: String?
        let text: String?
        let final: Bool?
        let pcm_base64: String?
        let sample_rate: Int?
        let duration_ms: Int?
        let format: String?
        let name: String?
        let arguments: JSONValue?
        let ok: Bool?
        let error: String?
        let offset_ms: Int?
        let decision: [String: JSONValue]?
        let context: [String: JSONValue]?
        let code: String?
        let message: String?
    }

    public enum DecodeError: Error, Equatable {
        case notAnEvent
        case badAudio
    }

    public static func decode(_ text: String) throws -> LiveEvent {
        guard let wire = try? JSONDecoder().decode(Wire.self, from: Data(text.utf8)) else {
            throw DecodeError.notAnEvent
        }
        switch wire.type {
        case "session_state":
            return .sessionState(
                state: wire.state ?? "", detail: wire.detail, reason: wire.reason, live: wire.live
            )
        case "transcript":
            return .transcript(speaker: wire.speaker ?? "", text: wire.text ?? "", final: wire.final ?? true)
        case "audio_chunk":
            guard let base64 = wire.pcm_base64, let audio = Data(base64Encoded: base64) else {
                throw DecodeError.badAudio
            }
            return .audioChunk(
                audio: audio,
                sampleRate: wire.sample_rate ?? 24_000,
                durationMs: wire.duration_ms ?? 0,
                format: wire.format ?? sniffFormat(audio)
            )
        case "tool_call":
            return .toolCall(name: wire.name ?? "", arguments: wire.arguments?.displayText ?? "")
        case "tool_result":
            return .toolResult(name: wire.name ?? "", ok: wire.ok ?? false, error: wire.error)
        case "interrupted_at":
            func text(_ key: String) -> String? {
                if case .string(let value)? = wire.context?[key], !value.isEmpty { return value }
                return nil
            }
            return .interruptedAt(
                offsetMs: wire.offset_ms ?? 0,
                segmentTitle: text("segment_title"),
                claimText: text("claim_text"),
                decision: wire.decision ?? [:]
            )
        case "error":
            return .error(code: wire.code ?? "error", message: wire.message ?? "")
        default:
            return .other(type: wire.type)
        }
    }

    /// For an older service that does not label `format`: the SPA's sniff, byte for byte.
    static func sniffFormat(_ bytes: Data) -> String {
        let head = [UInt8](bytes.prefix(4))
        guard head.count >= 2 else { return "pcm16" }
        if head.count >= 3, head[0] == 0x49, head[1] == 0x44, head[2] == 0x33 { return "mp3" }
        if head[0] == 0xFF, head[1] & 0xE0 == 0xE0 { return "mp3" }
        if head.count == 4, head == [0x52, 0x49, 0x46, 0x46] { return "wav" }
        return "pcm16"
    }
}

/// What the phone tells the voice service. Listener audio is not one of these: it goes as
/// binary frames of 16 kHz mono little-endian int16, which is what the service's detector
/// expects (any packetisation).
public enum LiveFrame: Equatable, Sendable {
    /// The client holds the whole rendered file, so the delivered ceiling is the episode.
    case narrationDelivered(durationMs: Int)
    case playbackPosition(spokenThroughMs: Int)
    /// A pause the listener made: the clock stops and nothing is engaged, so nothing is
    /// billed. Not a barge-in.
    case narrationPaused(spokenThroughMs: Int)
    /// Narration is playing again — after a reply, after "never mind", or after the
    /// listener pressed play. Closes the listener-audio gate on the service's side.
    case narrationResumed(spokenThroughMs: Int)
    /// The explicit floor-taking, for a listener who would rather press than talk over it.
    case bargeIn
    /// A typed question.
    case text(String)
    case close

    public var json: String {
        let object: [String: Any]
        switch self {
        case .narrationDelivered(let ms): object = ["type": "narration_delivered", "duration_ms": ms]
        case .playbackPosition(let ms): object = ["type": "playback_position", "spoken_through_ms": ms]
        case .narrationPaused(let ms): object = ["type": "narration_paused", "spoken_through_ms": ms]
        case .narrationResumed(let ms): object = ["type": "narration_resumed", "spoken_through_ms": ms]
        case .bargeIn: object = ["type": "barge_in"]
        case .text(let text): object = ["type": "text", "text": text]
        case .close: object = ["type": "close"]
        }
        let data = (try? JSONSerialization.data(withJSONObject: object, options: [.sortedKeys])) ?? Data("{}".utf8)
        return String(decoding: data, as: UTF8.self)
    }
}

/// Listener audio as the service's detector wants it: 16 kHz, mono, little-endian int16.
public enum LiveAudioFormat {
    public static let sampleRate = 16_000

    /// Linear resample of float samples to 16 kHz int16 — the SPA's `downsample`, so the
    /// two clients hand the detector the same thing.
    public static func pcm16(from samples: [Float], sampleRate fromRate: Double) -> Data {
        let target = Double(sampleRate)
        let ratio = fromRate / target
        let count = ratio == 1 ? samples.count : Int(Double(samples.count) / ratio)
        var out = [Int16](repeating: 0, count: count)
        for index in 0..<count {
            let value: Float
            if ratio == 1 {
                value = samples[index]
            } else {
                let position = Double(index) * ratio
                let lower = Int(position)
                let upper = min(lower + 1, samples.count - 1)
                let fraction = Float(position - Double(lower))
                value = samples[lower] * (1 - fraction) + samples[upper] * fraction
            }
            out[index] = Int16(max(-32_768, min(32_767, (value * 32_767).rounded())))
        }
        return out.withUnsafeBufferPointer { buffer in
            var data = Data(capacity: buffer.count * 2)
            for sample in buffer {
                withUnsafeBytes(of: sample.littleEndian) { data.append(contentsOf: $0) }
            }
            return data
        }
    }

    /// RMS in dBFS, floored at -100 like the service's own `dbfs()`, so the number beside
    /// the mic meter is the one the detector compares against its noise floor.
    public static func dbfs(_ samples: [Float]) -> Double {
        guard !samples.isEmpty else { return -100 }
        let total = samples.reduce(Float(0)) { $0 + $1 * $1 }
        let rms = (total / Float(samples.count)).squareRoot()
        return rms > 0 ? max(-100, 20 * log10(Double(rms))) : -100
    }

    /// Raw little-endian int16 reply audio as floats, for a player that wants them.
    public static func floats(fromPCM16 data: Data) -> [Float] {
        let count = data.count / 2
        var out = [Float](repeating: 0, count: count)
        data.withUnsafeBytes { raw in
            for index in 0..<count {
                let sample = Int16(littleEndian: raw.loadUnaligned(fromByteOffset: index * 2, as: Int16.self))
                out[index] = Float(sample) / 32_768
            }
        }
        return out
    }
}
