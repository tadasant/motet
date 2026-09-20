import XCTest
@testable import MotetKit

/// What a file is, from its first bytes — the question AVFoundation answers from the path
/// extension instead, which is why the extension has to be right.
final class AudioFileFormatTests: XCTestCase {
    func testAnMpegFrameHeaderIsMp3() {
        XCTAssertEqual(AudioFileFormat.sniff(Audio.mp3), .mp3)
    }

    func testAnId3TagAheadOfTheFramesIsMp3() {
        var data = Data("ID3".utf8)
        data.append(contentsOf: [0x04, 0x00, 0x00, 0x00, 0x00, 0x00, 0x0A])
        data.append(Data(repeating: 0, count: 10))
        data.append(Audio.mp3Frames(3))
        XCTAssertEqual(AudioFileFormat.sniff(data), .mp3)
    }

    func testAnId3TagWithNothingBehindItIsNotAudio() {
        var data = Data("ID3".utf8)
        data.append(contentsOf: [0x04, 0x00, 0x00, 0x00, 0x00, 0x00, 0x0A])
        data.append(Data(repeating: 0, count: 10))
        XCTAssertNil(AudioFileFormat.sniff(data))
    }

    /// The worker admits a segment by resynchronising past bytes that are not a frame, and
    /// a player does the same; a phone that demanded a sync word at byte zero would refuse a
    /// download the server was right to publish, on every sync, with nothing saying why.
    func testLeadingBytesThatAreNotAFrameAreSteppedPast() {
        for junk in [
            Data(repeating: 0, count: 7),
            Data("APETAGEX".utf8) + Data(repeating: 0, count: 24),
            Data([0xFF, 0xFF, 0xFF, 0x00]),
            Data(repeating: 0x41, count: 3000),
        ] {
            XCTAssertEqual(AudioFileFormat.sniff(junk + Audio.mp3Frames(3)), .mp3, "\(junk.count) bytes of junk")
        }
    }

    func testTheWindowIsBounded() {
        let junk = Data(repeating: 0, count: AudioFileFormat.headLength + 1)
        XCTAssertNil(AudioFileFormat.sniff(junk + Audio.mp3Frames(3)))
    }

    /// One header is a byte pair; a header whose *length* lands on another header is audio.
    func testASyncShapedPairNotFollowedByAFrameIsChance() {
        let stray = Audio.mp3FrameHeader + Data(repeating: 0, count: 100)  // claims 417 bytes
        XCTAssertNil(AudioFileFormat.sniff(stray + Data(repeating: 0, count: 400)))
        XCTAssertEqual(AudioFileFormat.sniff(stray), .mp3, "ends inside the frame: nothing to contradict it")
    }

    func testARiffWaveHeaderIsWav() {
        XCTAssertEqual(AudioFileFormat.sniff(Audio.wav), .wav)
    }

    func testAnIsoBaseMediaBoxIsM4a() {
        var data = Data([0x00, 0x00, 0x00, 0x20])
        data.append(contentsOf: Array("ftypM4A ".utf8))
        XCTAssertEqual(AudioFileFormat.sniff(data), .m4a)
    }

    /// ADTS AAC shares MPEG audio's sync word and is told apart by the reserved layer bits.
    /// Getting this backwards would name an AAC file `.mp3`, which is the same bug again.
    func testAdtsAacIsNotMistakenForMp3() {
        // Two ADTS frames: 7-byte headers, each declaring a 20-byte frame.
        let frame = Data([0xFF, 0xF1, 0x50, 0x80, 0x02, 0x80, 0x00]) + Data(repeating: 0, count: 13)
        XCTAssertEqual(AudioFileFormat.sniff(frame + frame), .aac)
        let frameV2 = Data([0xFF, 0xF9, 0x50, 0x80, 0x02, 0x80, 0x00]) + Data(repeating: 0, count: 13)
        XCTAssertEqual(AudioFileFormat.sniff(frameV2 + frameV2), .aac)
    }

    /// Bytes 4–7 of an MP3 that happen to spell "ftyp" must not make it an M4A.
    func testTheFtypBoxCannotClaimAnMp3() {
        var data = Audio.mp3FrameHeader
        data.append(contentsOf: Array("ftyp".utf8))
        data.append(Data(repeating: 0, count: 417 - 8))
        data.append(Audio.mp3Frames(1))
        XCTAssertEqual(AudioFileFormat.sniff(data), .mp3)
    }

    func testAnHtmlPageIsNotAudio() {
        XCTAssertNil(AudioFileFormat.sniff(Audio.signInPage))
    }

    func testAJsonErrorBodyIsNotAudio() {
        XCTAssertNil(AudioFileFormat.sniff(Data(#"{"detail":"Not authenticated"}"#.utf8)))
    }

    func testAnObjectStoreAccessDeniedDocumentIsNotAudio() {
        let xml = "<?xml version='1.0'?><Error><Code>AccessDenied</Code></Error>"
        XCTAssertNil(AudioFileFormat.sniff(Data(xml.utf8)))
    }

    func testNothingIsNotAudio() {
        XCTAssertNil(AudioFileFormat.sniff(Data()))
        XCTAssertNil(AudioFileFormat.sniff(Data([0xFF])))
    }

    /// A reserved MPEG version or layer cannot occur in a real frame, so a byte pair that
    /// merely starts 0xFF must not be waved through as audio.
    func testAReservedMpegVersionIsNotAudio() {
        XCTAssertNil(AudioFileFormat.sniff(Data([0xFF, 0xEB, 0x90, 0x44])))
    }

    func testEveryFormatRoundTripsThroughItsExtension() {
        for format in AudioFileFormat.allCases {
            XCTAssertEqual(AudioFileFormat(pathExtension: format.pathExtension), format)
            XCTAssertEqual(AudioFileFormat(pathExtension: format.pathExtension.uppercased()), format)
        }
    }

    /// The extension that shipped, and the reason nothing downloaded ever played.
    func testTheExtensionOlderBuildsUsedIsNotOneAVFoundationKnows() {
        XCTAssertNil(AudioFileFormat(pathExtension: "audio"))
        XCTAssertNil(AudioFileFormat(pathExtension: ""))
        XCTAssertNil(AudioFileFormat(pathExtension: "download"))
    }
}
