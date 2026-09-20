import XCTest
@testable import MotetKit

/// What a file is, from its first bytes — the question AVFoundation answers from the path
/// extension instead, which is why the extension has to be right.
final class AudioFileFormatTests: XCTestCase {
    func testAnMpegFrameHeaderIsMp3() {
        XCTAssertEqual(AudioFileFormat.sniff(Audio.mp3), .mp3)
    }

    func testAnId3TagIsMp3() {
        var data = Data("ID3".utf8)
        data.append(contentsOf: [0x04, 0x00, 0x00, 0x00, 0x00, 0x00, 0x0A])
        XCTAssertEqual(AudioFileFormat.sniff(data), .mp3)
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
        XCTAssertEqual(AudioFileFormat.sniff(Data([0xFF, 0xF1, 0x50, 0x80, 0x00])), .aac)
        XCTAssertEqual(AudioFileFormat.sniff(Data([0xFF, 0xF9, 0x50, 0x80, 0x00])), .aac)
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
