import Foundation
import MotetKit

// Background sessions are a Darwin-only facility — on Linux `URLSessionConfiguration`
// has no `background(withIdentifier:)` at all — so the whole file is compiled only where
// it can exist. `MotetKit`'s `EpisodeDownloader` protocol is what keeps the offline logic
// testable everywhere.
#if canImport(Darwin)

/// Downloads episode audio with a background `URLSession`.
///
/// A background session rather than a plain one because the point of the offline library is
/// that the episodes are *already there*: the download is started when the app is opened
/// over breakfast and has to survive the app being backgrounded and the phone being
/// pocketed. iOS hands the transfer to `nsurlsessiond`, which finishes it and wakes the app.
///
/// `/v1/episodes/{id}/audio` may answer with the bytes or with a 307 to a signed URL; both
/// are followed transparently, which is the whole point of that route's design — a client
/// cannot tell which storage backend is behind it.
///
/// **Unverified here.** Background transfers do not behave like this on a simulator, and
/// completion while suspended cannot be exercised without a device. See `ios/README.md`.
public final class BackgroundEpisodeDownloader: NSObject, EpisodeDownloader, @unchecked Sendable {
    private let identifier: String
    private let lock = NSLock()
    private var continuations: [Int: CheckedContinuation<URL, Error>] = [:]
    private lazy var session: URLSession = {
        let configuration = URLSessionConfiguration.background(withIdentifier: identifier)
        configuration.allowsCellularAccess = true
        #if os(iOS)
        // Start the transfer now rather than when iOS thinks the moment is good — the
        // episodes have to be on the phone before the walk, not eventually.
        configuration.isDiscretionary = false
        // Wake the app when a transfer finishes while it is suspended.
        configuration.sessionSendsLaunchEvents = true
        #endif
        return URLSession(configuration: configuration, delegate: self, delegateQueue: nil)
    }()

    private var _backgroundCompletionHandler: (() -> Void)?

    /// Set by the app delegate so iOS can be told the app is done handling the events it was
    /// woken for. Read on the session's delegate queue, written on the main thread, so it
    /// goes through the same lock as the continuations.
    public var backgroundCompletionHandler: (() -> Void)? {
        get { lock.withLock { _backgroundCompletionHandler } }
        set { lock.withLock { _backgroundCompletionHandler = newValue } }
    }

    public init(identifier: String = "com.getmotet.app.downloads") {
        self.identifier = identifier
        super.init()
    }

    public func download(from url: URL, to destination: URL) async throws {
        let temporary: URL = try await withCheckedThrowingContinuation { continuation in
            let task = session.downloadTask(with: url)
            lock.withLock { continuations[task.taskIdentifier] = continuation }
            task.resume()
        }
        let fileManager = FileManager.default
        if fileManager.fileExists(atPath: destination.path) {
            try fileManager.removeItem(at: destination)
        }
        try fileManager.moveItem(at: temporary, to: destination)
    }

    private func finish(taskIdentifier: Int, with result: Result<URL, Error>) {
        let continuation = lock.withLock { continuations.removeValue(forKey: taskIdentifier) }
        guard let continuation else {
            // iOS woke a *fresh* process for a transfer that finished while the app was
            // dead, so nobody is waiting: the awaiting call died with the old process.
            // Throw the staged bytes away rather than leaking them into the temp directory
            // — the offline sync will simply fetch the episode again.
            if case .success(let staged) = result {
                try? FileManager.default.removeItem(at: staged)
            }
            return
        }
        continuation.resume(with: result)
    }
}

extension BackgroundEpisodeDownloader: URLSessionDownloadDelegate {
    public func urlSession(
        _ session: URLSession,
        downloadTask: URLSessionDownloadTask,
        didFinishDownloadingTo location: URL
    ) {
        // The delegate's file is deleted the moment this returns, so it is moved somewhere
        // this object owns before the continuation resumes on another thread.
        if let response = downloadTask.response as? HTTPURLResponse,
           !(200..<300).contains(response.statusCode) {
            finish(
                taskIdentifier: downloadTask.taskIdentifier,
                with: .failure(MotetError.http(status: response.statusCode, detail: nil))
            )
            return
        }
        let staging = FileManager.default.temporaryDirectory
            .appendingPathComponent("motet-download-\(UUID().uuidString)")
        do {
            try FileManager.default.moveItem(at: location, to: staging)
            finish(taskIdentifier: downloadTask.taskIdentifier, with: .success(staging))
        } catch {
            finish(taskIdentifier: downloadTask.taskIdentifier, with: .failure(error))
        }
    }

    public func urlSession(
        _ session: URLSession, task: URLSessionTask, didCompleteWithError error: Error?
    ) {
        guard let error else { return }
        finish(taskIdentifier: task.taskIdentifier, with: .failure(MotetError.transport(error)))
    }

    #if os(iOS)
    public func urlSessionDidFinishEvents(forBackgroundURLSession session: URLSession) {
        // iOS expects this on the main thread, and expects it exactly once per wake-up.
        guard let handler = backgroundCompletionHandler else { return }
        backgroundCompletionHandler = nil
        DispatchQueue.main.async { handler() }
    }
    #endif
}
#endif
