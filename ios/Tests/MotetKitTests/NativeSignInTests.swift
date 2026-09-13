import XCTest
@testable import MotetKit

/// The app's half of signing in through the web sign-in: the link it waits for, and what
/// it sends. The API's half is `api/tests/test_native_sign_in.py`.
final class NativeSignInTests: XCTestCase {
    private let base = URL(string: "https://api.example.invalid")!

    func testTheCodeIsReadOutOfTheAPIsHandoffLink() throws {
        let link = try XCTUnwrap(URL(string: "motet://signed-in?code=one-time"))
        XCTAssertEqual(try NativeSignIn.handoffCode(from: link), "one-time")
    }

    func testTheCodeIsReadOutOfAnHttpsHandoffLink() throws {
        // Where the deployment serves an app-site-association file, the handoff comes back
        // as a verified universal link instead — which no other app can be handed.
        let link = try XCTUnwrap(URL(string: "https://app.example.invalid/app/signed-in?code=one-time"))
        XCTAssertEqual(try NativeSignIn.handoffCode(from: link), "one-time")
    }

    func testALinkThatIsNotTheHandoffIsRefused() throws {
        for raw in [
            "https://example.invalid/signed-in?code=x",
            "https://example.invalid/app/signed-in/elsewhere?code=x",
            "http://example.invalid/app/signed-in?code=x",
            "motet://elsewhere?code=x",
        ] {
            let link = try XCTUnwrap(URL(string: raw))
            XCTAssertThrowsError(try NativeSignIn.handoffCode(from: link)) { error in
                XCTAssertEqual(error as? NativeSignIn.Failure, .notAHandoff, raw)
            }
        }
    }

    func testAHandoffWithoutACodeSaysSo() throws {
        for raw in ["motet://signed-in", "motet://signed-in?code="] {
            let link = try XCTUnwrap(URL(string: raw))
            XCTAssertThrowsError(try NativeSignIn.handoffCode(from: link)) { error in
                XCTAssertEqual(error as? NativeSignIn.Failure, .missingCode, raw)
            }
        }
    }

    func testStartingSendsTheChallengeAndNoCredential() async throws {
        let transport = StubTransport()
        transport.enqueueJSON(#"{"authorization_url":"https://accounts.example.invalid/o","callback_scheme":"motet"}"#)
        let client = MotetHTTPClient(configuration: MotetConfiguration(baseURL: base), transport: transport)

        let started = try await client.startNativeSignIn(codeChallenge: "the-challenge")

        XCTAssertEqual(started.callbackScheme, "motet")
        XCTAssertNil(started.callbackHost, "a deployment serving no association file reports none")
        let request = try XCTUnwrap(transport.recordedRequests().first)
        XCTAssertEqual(request.method, "POST")
        XCTAssertEqual(request.url.absoluteString, "https://api.example.invalid/v1/auth/native/start")
        XCTAssertNil(request.headers["Authorization"])
        let body = try JSONSerialization.jsonObject(with: XCTUnwrap(request.body)) as? [String: String]
        XCTAssertEqual(body, ["code_challenge": "the-challenge"])
    }

    func testRedeemingSendsTheCodeWithTheVerifierAndReturnsTheSession() async throws {
        let transport = StubTransport()
        transport.enqueueJSON("""
        {"token":"session-token","email":"owner@motet.test",
         "expires_at":"2026-10-13T00:00:00.123456Z","handoff_url":null}
        """)
        let client = MotetHTTPClient(configuration: MotetConfiguration(baseURL: base), transport: transport)

        let session = try await client.redeemNativeSignIn(code: "one-time", codeVerifier: "verifier")

        XCTAssertEqual(session.token, "session-token")
        XCTAssertEqual(session.email, "owner@motet.test")
        let request = try XCTUnwrap(transport.recordedRequests().first)
        XCTAssertEqual(request.url.absoluteString, "https://api.example.invalid/v1/auth/native/redeem")
        let body = try JSONSerialization.jsonObject(with: XCTUnwrap(request.body)) as? [String: String]
        XCTAssertEqual(body, ["code": "one-time", "code_verifier": "verifier"])
    }

    #if canImport(CryptoKit)
    func testTheChallengeIsRFC7636sS256() {
        // RFC 7636, Appendix B.
        XCTAssertEqual(
            PKCEPair.challenge(for: "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"),
            "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
        )
    }

    func testAGeneratedPairIsTheShapeTheAPIAccepts() {
        let first = PKCEPair.generate()
        let second = PKCEPair.generate()
        let shape = "^[A-Za-z0-9_-]{43}$"
        XCTAssertNotNil(first.verifier.range(of: shape, options: .regularExpression))
        XCTAssertNotNil(first.challenge.range(of: shape, options: .regularExpression))
        XCTAssertEqual(first.challenge, PKCEPair.challenge(for: first.verifier))
        XCTAssertNotEqual(first.verifier, second.verifier)
    }
    #endif
}
