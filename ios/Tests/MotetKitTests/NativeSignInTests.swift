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
        // Where the deployment serves an app-site-association file naming this app, the
        // handoff comes back on a verified https link instead — one no other app can be
        // handed, because the entitlement is what Apple checks before allowing it.
        let link = try XCTUnwrap(URL(string: "https://app.example.invalid/app/signed-in?code=one-time"))
        let code = try NativeSignIn.handoffCode(from: link, appLinkHost: "app.example.invalid")
        XCTAssertEqual(code, "one-time")
    }

    func testAnHttpsHandoffIsRefusedUnlessThisSignInAskedForOne() throws {
        // A sign-in started on the custom scheme must not accept an https link: the app is
        // not entitled for that host, so nothing verified it.
        let link = try XCTUnwrap(URL(string: "https://app.example.invalid/app/signed-in?code=one-time"))
        XCTAssertThrowsError(try NativeSignIn.handoffCode(from: link)) { error in
            XCTAssertEqual(error as? NativeSignIn.Failure, .notAHandoff)
        }
        XCTAssertThrowsError(
            try NativeSignIn.handoffCode(from: link, appLinkHost: "somewhere.else.invalid")
        ) { error in
            XCTAssertEqual(error as? NativeSignIn.Failure, .notAHandoff)
        }
    }

    func testALinkThatIsNotTheHandoffIsRefused() throws {
        for raw in [
            "https://app.example.invalid/signed-in?code=x",
            "https://app.example.invalid/app/signed-in/elsewhere?code=x",
            "http://app.example.invalid/app/signed-in?code=x",
            "motet://elsewhere?code=x",
        ] {
            let link = try XCTUnwrap(URL(string: raw))
            XCTAssertThrowsError(
                try NativeSignIn.handoffCode(from: link, appLinkHost: "app.example.invalid")
            ) { error in
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
        // No domain declared: this build cannot receive an https handoff, and says so rather
        // than leaving the server to guess. The server then commits to the scheme.
        XCTAssertEqual(body, ["code_challenge": "the-challenge"])
    }

    func testAnEntitledBuildDeclaresItsDomainWhenStarting() async throws {
        // The server has to commit to one shape of handoff before the sign-in opens, and the
        // browser that later calls the callback knows nothing about this phone — so the app
        // says up front which callback it can receive. See AGENTS.md, "The handoff comes back
        // on a verified https link".
        let transport = StubTransport()
        transport.enqueueJSON(#"""
        {"authorization_url":"https://accounts.example.invalid/o","callback_scheme":"motet",
         "callback_host":"app.example.invalid","callback_path":"/app/signed-in"}
        """#)
        let client = MotetHTTPClient(configuration: MotetConfiguration(baseURL: base), transport: transport)

        let started = try await client.startNativeSignIn(
            codeChallenge: "the-challenge", appLinkDomain: "app.example.invalid"
        )

        XCTAssertEqual(started.callbackHost, "app.example.invalid")
        XCTAssertEqual(started.callbackPath, "/app/signed-in")
        let request = try XCTUnwrap(transport.recordedRequests().first)
        let body = try JSONSerialization.jsonObject(with: XCTUnwrap(request.body)) as? [String: String]
        XCTAssertEqual(body, ["code_challenge": "the-challenge", "app_link_domain": "app.example.invalid"])
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
