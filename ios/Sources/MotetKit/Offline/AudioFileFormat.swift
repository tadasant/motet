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
    /// Only the first bytes are needed and only the first bytes are read: the caller holds a
    /// file that may be twenty megabytes, and the question is answered by twelve.
    public static func sniff(_ data: Data) -> AudioFileFormat? {
        let head = [UInt8](data.prefix(12))
        guard head.count >= 4 else { return nil }

        // "RIFF" .... "WAVE"
        if head.starts(with: [0x52, 0x49, 0x46, 0x46]), head.count >= 12,
           head[8...11].elementsEqual([0x57, 0x41, 0x56, 0x45]) {
            return .wav
        }
        // "ftyp" at offset 4 — the ISO base media box every MP4/M4A file opens with.
        if head.count >= 8, head[4...7].elementsEqual([0x66, 0x74, 0x79, 0x70]) {
            return .m4a
        }
        if head.starts(with: [0x63, 0x61, 0x66, 0x66]) { return .caf }  // "caff"
        if head.starts(with: [0x49, 0x44, 0x33]) { return .mp3 }  // an ID3v2 tag
        return mpegSyncWord(head[0], head[1])
    }

    /// MPEG audio and ADTS AAC share a sync word; the layer field is what separates them.
    private static func mpegSyncWord(_ first: UInt8, _ second: UInt8) -> AudioFileFormat? {
        guard first == 0xFF, second & 0xE0 == 0xE0 else { return nil }
        // Bits 4–3 are the MPEG version and bits 2–1 the layer. `01` is a reserved version
        // and `00` a reserved layer, so neither can appear in an MPEG audio frame — which is
        // what makes this a check rather than a guess, and what keeps a run of 0xFF bytes in
        // some other file from reading as audio. ADTS AAC reuses the sync word and puts `00`
        // in the layer field, so that value identifies it rather than disqualifying it.
        let version = (second >> 3) & 0b11
        let layer = (second >> 1) & 0b11
        if version == 1 { return nil }
        return layer == 0 ? .aac : .mp3
    }
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
