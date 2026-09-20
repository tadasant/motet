import Foundation

/// What kind of audio a file holds, decided from its first bytes.
///
/// **AVFoundation identifies a *local* file by its path extension, never by its contents.**
/// There is no HTTP response to carry a `Content-Type`, so `AVURLAsset` maps the path
/// extension to a UTI and picks a reader from that; an extension it does not recognise means
/// no reader, and the asset fails to open with `AVFoundationErrorDomain -11828`
/// (`AVErrorFileFormatNotRecognized` — "Cannot Open", "This media format is not supported")
/// before a byte of the file is parsed. The same bytes served over HTTP play, because there
/// the response's `Content-Type` answers the question the extension answers here.
///
/// The offline library used to name every download `<episode-id>.audio`, which is a media
/// extension nowhere, so **no downloaded episode had ever been playable** — a perfectly good
/// MP3 that streams fine is refused the moment it is on the device, and since the download
/// policy keeps the newest episodes on the phone, the newest episode is the one that is
/// always local and therefore always refused.
///
/// The type is read from the bytes rather than from the server's `audio_media_type` for two
/// reasons, and both of them are the reason this is not a one-line rename. It is the only
/// thing that can repair a file already on a phone, where no media type was ever recorded
/// alongside it. And it is the one check an error document saved under an audio filename
/// cannot pass, which is the other way this error arrives.
public enum AudioFileFormat: String, CaseIterable, Sendable {
    case mp3
    case wav
    case m4a
    case aac
    case caf

    /// How much of a file `sniff` needs. Wide enough to find the first MPEG frame behind a
    /// tag or padding an encoder put ahead of it — the same window the worker's publish
    /// guard allows, so what the server lets out is what the phone lets in.
    public static let headLength = 4096

    /// The extension a file of this format must carry for AVFoundation to open it.
    public var pathExtension: String { rawValue }

    /// The format a path extension names, or nil where AVFoundation would recognise none.
    ///
    /// Case-insensitive, because the extension is compared against what is on disk and a
    /// file named by an older build — or by a server — is not obliged to be lowercase.
    public init?(pathExtension: String) {
        guard let match = AudioFileFormat(rawValue: pathExtension.lowercased()) else {
            return nil
        }
        self = match
    }

    /// The format `data` begins with, or nil if it does not begin as audio at all.
    ///
    /// The pipeline emits MP3 and WAV; the other three are what a server that changed its
    /// mind could plausibly send and iOS can play, named correctly rather than refused. Only
    /// `headLength` bytes are ever needed: the caller holds a file that may be twenty
    /// megabytes, and the question is answered inside the first four kilobytes.
    public static func sniff(_ data: Data) -> AudioFileFormat? {
        let head = [UInt8](data.prefix(headLength))
        guard head.count >= 4 else { return nil }

        // Signatures at offset zero first; the box at offset four last, so bytes 4–7 of an
        // MP3 that happen to spell "ftyp" cannot claim it.
        if head.starts(with: [0x52, 0x49, 0x46, 0x46]), head.count >= 12,  // "RIFF" … "WAVE"
           head[8...11].elementsEqual([0x57, 0x41, 0x56, 0x45]) {
            return .wav
        }
        if head.starts(with: [0x63, 0x61, 0x66, 0x66]) { return .caf }  // "caff"
        if let mpeg = mpegFormat(in: head) { return mpeg }
        if head.count >= 8, head[4...7].elementsEqual([0x66, 0x74, 0x79, 0x70]) {  // "ftyp"
            return .m4a
        }
        return nil
    }

    /// MPEG audio or ADTS AAC, found the way a player finds it: past an ID3v2 tag and any
    /// bytes that are not a frame, at the first header whose *length* lands on another.
    ///
    /// The worker's `mpeg_duration_ms` admits a segment by resynchronising exactly like
    /// this, so a rule here that wanted a sync word at byte zero would refuse a download
    /// the server was right to publish — and refuse it on every sync, forever, with the
    /// episode streaming instead and nothing saying why.
    private static func mpegFormat(in head: [UInt8]) -> AudioFileFormat? {
        var offset = 0
        if head.count >= 10, head.starts(with: [0x49, 0x44, 0x33]) {  // "ID3"
            // A syncsafe size: seven bits per byte, so it can never contain a sync word.
            var size = 0
            for byte in head[6..<10] { size = (size << 7) | Int(byte & 0x7F) }
            offset = 10 + size
        }
        while offset + 4 <= head.count {
            defer { offset += 1 }
            guard let frame = frameHeader(head, at: offset) else { continue }
            let following = offset + frame.length
            if following + 4 > head.count { return frame.format }  // ends inside: nothing to contradict it
            return frameHeader(head, at: following)?.format == frame.format ? frame.format : nil
        }
        return nil
    }

    /// A frame header at `offset`: its byte length and which of the two formats it is.
    private static func frameHeader(_ head: [UInt8], at offset: Int) -> (length: Int, format: AudioFileFormat)? {
        let b1 = head[offset + 1], b2 = head[offset + 2]
        guard head[offset] == 0xFF, b1 & 0xE0 == 0xE0 else { return nil }
        // Bits 4–3 of the second byte are the MPEG version and bits 2–1 the layer. `01` is
        // a reserved version, so it disqualifies; `00` is a reserved *layer*, and ADTS AAC —
        // which reuses the sync word — is what puts it there, so it identifies rather than
        // disqualifies.
        let version = (b1 >> 3) & 0b11
        let layer = (b1 >> 1) & 0b11
        if version == 1 { return nil }
        if layer == 0 {
            // ADTS: a 13-bit frame length spans bytes 3–5.
            guard offset + 6 <= head.count else { return nil }
            let length = (Int(head[offset + 3] & 0x03) << 11)
                | (Int(head[offset + 4]) << 3)
                | (Int(head[offset + 5]) >> 5)
            return length > 7 ? (length, .aac) : nil
        }
        // MPEG audio, Layer III only — the layer every encoder this pipeline meets emits.
        guard layer == 1 else { return nil }
        let bitrateIndex = Int((b2 >> 4) & 0b1111)
        let sampleRateIndex = Int((b2 >> 2) & 0b11)
        let padding = Int((b2 >> 1) & 0b1)
        guard sampleRateIndex != 3 else { return nil }
        let isV1 = version == 3
        let bitrate = (isV1 ? bitratesV1 : bitratesV2)[bitrateIndex]
        guard bitrate > 0 else { return nil }
        let sampleRate = sampleRates[Int(version)]![sampleRateIndex]
        let samples = isV1 ? 1152 : 576
        let length = (samples / 8) * bitrate * 1000 / sampleRate + padding
        return length > 4 ? (length, .mp3) : nil
    }

    private static let bitratesV1 = [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 0]
    private static let bitratesV2 = [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160, 0]
    private static let sampleRates: [Int: [Int]] = [
        3: [44100, 48000, 32000],  // MPEG 1
        2: [22050, 24000, 16000],  // MPEG 2
        0: [11025, 12000, 8000],  // MPEG 2.5
    ]
}

/// A downloaded file that is not audio any player could open.
///
/// Its own error rather than a `MotetError` case: the transfer succeeded and the network is
/// not what went wrong, and `MotetError.isRetryable` is the outbox's retry rule, which has no
/// opinion to offer about a file's contents.
public struct UnplayableAudioError: Error, CustomStringConvertible, Sendable {
    /// How big the file was — zero is its own diagnosis, and a common one.
    public let byteCount: Int
    /// The first bytes, as hex, so a log line says which of the usual suspects arrived: an
    /// HTML sign-in page, a JSON error body, an XML `AccessDenied` from the object store.
    public let firstBytes: String

    public init(byteCount: Int, head: Data) {
        self.byteCount = byteCount
        self.firstBytes = head.prefix(12).map { String(format: "%02x", $0) }.joined(separator: " ")
    }

    public var description: String {
        if byteCount == 0 { return "The downloaded episode audio was empty." }
        return "The downloaded episode audio is not a format this device can play "
            + "(\(byteCount) bytes beginning \(firstBytes))."
    }
}
