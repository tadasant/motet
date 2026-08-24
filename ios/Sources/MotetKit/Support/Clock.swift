import Foundation

/// The current time, injectable.
///
/// Every retry backoff and every throttle in this package reads the clock through this, so
/// the tests that cover them run in microseconds and do not flake on a slow machine.
public protocol MotetClock: Sendable {
    var now: Date { get }
}

public struct SystemClock: MotetClock {
    public init() {}
    public var now: Date { Date() }
}

/// A clock the tests move by hand.
public final class TestClock: MotetClock, @unchecked Sendable {
    private var current: Date
    private let lock = NSLock()

    public init(now: Date = Date(timeIntervalSince1970: 1_800_000_000)) {
        self.current = now
    }

    public var now: Date { lock.withLock { current } }

    public func advance(by interval: TimeInterval) {
        lock.withLock { current = current.addingTimeInterval(interval) }
    }
}
