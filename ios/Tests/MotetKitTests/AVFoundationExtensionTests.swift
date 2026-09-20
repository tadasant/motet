// The claim the rest of this fix rests on, put to AVFoundation itself.
//
// `AudioFileFormat` asserts that a *local* file's path extension is the only thing
// AVFoundation identifies it by — there is no HTTP response to carry a `Content-Type` — and
// that the `.audio` every download used to be named therefore made good audio unopenable,
// with `AVFoundationErrorDomain -11828`. Every other test in this suite tests our own rule
// about naming; this one tests the reason the rule exists, and it is the only test here that
// can, because the framework that refuses the file is Apple's.
//
// Compiled only where AVFoundation is, so the Linux run in `bin/ci` skips it entirely. It
// runs on the `ios` job's macOS runner, which calls the same `ios/bin/ci-swift`. macOS rather
// than a simulator is a real limitation and a small one: this is the format-identification
// path, which is AVFoundation's and not the platform's.
#if canImport(AVFoundation)
import AVFoundation
import XCTest
@testable import MotetKit

final class AVFoundationExtensionTests: XCTestCase {
    /// A complete, decodable, silent 8 kHz mono WAV — the shape the pipeline's own
    /// synthesizer emits, byte-for-byte in its header but for the two size fields.
    private static func silentWav(frames: Int = 8000) -> Data {
        let payload = Data(repeating: 0, count: frames * 2)
        var data = Data("RIFF".utf8)
        data.append(contentsOf: withUnsafeBytes(of: UInt32(36 + payload.count).littleEndian) {
            Array($0)
        })
        data.append(contentsOf: Array("WAVEfmt ".utf8))
        for value in [UInt32(16)] {
            data.append(contentsOf: withUnsafeBytes(of: value.littleEndian) { Array($0) })
        }
        for value in [UInt16(1), UInt16(1)] {
            data.append(contentsOf: withUnsafeBytes(of: value.littleEndian) { Array($0) })
        }
        for value in [UInt32(8000), UInt32(16000)] {
            data.append(contentsOf: withUnsafeBytes(of: value.littleEndian) { Array($0) })
        }
        for value in [UInt16(2), UInt16(16)] {
            data.append(contentsOf: withUnsafeBytes(of: value.littleEndian) { Array($0) })
        }
        data.append(contentsOf: Array("data".utf8))
        data.append(contentsOf: withUnsafeBytes(of: UInt32(payload.count).littleEndian) {
            Array($0)
        })
        data.append(payload)
        return data
    }

    private func write(_ data: Data, named name: String) throws -> URL {
        let directory = Fixture.temporaryDirectory(self)
        let url = directory.appendingPathComponent(name)
        try data.write(to: url)
        return url
    }

    private func isPlayable(_ url: URL) async -> Bool {
        let asset = AVURLAsset(url: url)
        // A refused extension can surface either way — `false`, or a thrown
        // `AVError.fileFormatNotRecognized`. Both are "this will not play".
        return (try? await asset.load(.isPlayable)) ?? false
    }

    /// The same bytes, two names, opposite outcomes. This is the bug.
    func testIdenticalBytesPlayAsWavAndAreRefusedAsDotAudio() async throws {
        let wav = Self.silentWav()

        let named = try write(wav, named: "episode.wav")
        let misnamed = try write(wav, named: "episode.audio")

        let namedPlays = await isPlayable(named)
        let misnamedPlays = await isPlayable(misnamed)

        XCTAssertTrue(
            namedPlays, "a complete WAV named .wav has to open, or this proves nothing"
        )
        XCTAssertFalse(
            misnamedPlays,
            """
            AVFoundation opened a file whose extension it should not recognise. If this \
            starts passing, the reason OfflineLibrary names files by their sniffed format \
            has changed — the naming is still right, but this test's claim needs rewriting.
            """
        )
    }

    /// And the error really is the one Tadas's phone reported.
    func testTheRefusalIsTheErrorTheDeviceShowed() async throws {
        let misnamed = try write(Self.silentWav(), named: "episode.audio")
        let asset = AVURLAsset(url: misnamed)

        do {
            let playable = try await asset.load(.isPlayable)
            XCTAssertFalse(playable, "no reader, so nothing to play")
        } catch let error as NSError {
            XCTAssertEqual(error.domain, AVFoundationErrorDomain)
            XCTAssertEqual(
                error.code, AVError.Code.fileFormatNotRecognized.rawValue,
                "-11828 is what the Now playing sheet printed"
            )
        }
    }

    /// What the library now produces, opened by the framework that refused the old name.
    func testWhatTheOfflineLibraryNamesAFileIsWhatAVFoundationCanOpen() async throws {
        let directory = Fixture.temporaryDirectory(self)
        let downloader = FakeDownloader()
        downloader.payload = Self.silentWav()
        let library = try OfflineLibrary(
            store: InMemoryKeyValueStore(),
            directory: directory,
            downloader: downloader,
            clock: TestClock()
        )

        let local = try await library.download(
            episodeId: "ep-1", from: URL(string: "https://example.invalid/a")!
        )

        let playable = await isPlayable(local)
        XCTAssertTrue(playable, "the whole point: a downloaded episode opens")
    }
}
#endif
