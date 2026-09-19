import Foundation
import MotetKit

#if canImport(Darwin)

/// Play Live's socket: `URLSessionWebSocketTask`, which queues sends in call order — the
/// property the microphone relies on when it hands frames over from the audio thread.
///
/// It sends no `Origin` header, and that is what the voice service expects of a client that
/// is not a browser: `origin_allowed` lets a request without one through, because the check
/// exists for the one cross-origin surface a *browser* reaches, and the token in the first
/// frame is the real control.
public final class URLSessionLiveTransport: LiveTransport, @unchecked Sendable {
    private let session: URLSession
    private let lock = NSLock()
    private var task: URLSessionWebSocketTask?

    /// A composed-arm reply arrives as one base64 MP3 inside one JSON frame; the default
    /// 1 MiB ceiling would close the socket on a long answer.
    static let maximumMessageSize = 16 * 1_024 * 1_024

    public init(session: URLSession = .shared) {
        self.session = session
    }

    public func open(url: URL) -> AsyncStream<LiveTransportEvent> {
        let task = session.webSocketTask(with: url)
        task.maximumMessageSize = Self.maximumMessageSize
        lock.withLock { self.task = task }
        return AsyncStream { continuation in
            @Sendable func receive() {
                task.receive { result in
                    switch result {
                    case .success(.string(let text)):
                        continuation.yield(.text(text))
                        receive()
                    case .success(.data(let data)):
                        continuation.yield(.text(String(decoding: data, as: UTF8.self)))
                        receive()
                    case .success:
                        receive()
                    case .failure(let error):
                        if task.closeCode != .invalid {
                            continuation.yield(.closed(code: task.closeCode.rawValue))
                        } else {
                            continuation.yield(.failed(error.localizedDescription))
                        }
                        continuation.finish()
                    }
                }
            }
            continuation.onTermination = { _ in
                task.cancel(with: .goingAway, reason: nil)
            }
            task.resume()
            receive()
        }
    }

    public func sendText(_ text: String) {
        lock.withLock { task }?.send(.string(text)) { _ in }
    }

    public func sendAudio(_ data: Data) {
        lock.withLock { task }?.send(.data(data)) { _ in }
    }

    public func close() {
        let task = lock.withLock { () -> URLSessionWebSocketTask? in
            defer { self.task = nil }
            return self.task
        }
        task?.cancel(with: .normalClosure, reason: nil)
    }
}
#endif
