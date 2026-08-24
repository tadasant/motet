import Foundation

/// Small durable storage: the outbox, the listening positions, the offline manifest.
///
/// A protocol rather than `UserDefaults` directly, because every one of these is a fact the
/// app must not lose when it is killed mid-walk, and because a test that has to clean up
/// `UserDefaults` is a test that fails when it runs beside another one.
public protocol KeyValueStore: Sendable {
    func data(forKey key: String) throws -> Data?
    func set(_ data: Data?, forKey key: String) throws
    func keys(withPrefix prefix: String) throws -> [String]
}

extension KeyValueStore {
    public func value<T: Decodable>(_ type: T.Type, forKey key: String) throws -> T? {
        guard let data = try data(forKey: key) else { return nil }
        return try MotetDate.makeDecoder().decode(T.self, from: data)
    }

    public func setValue<T: Encodable>(_ value: T?, forKey key: String) throws {
        guard let value else {
            try set(nil, forKey: key)
            return
        }
        try set(try MotetDate.makeEncoder().encode(value), forKey: key)
    }
}

/// A directory of files, one per key. Durable across launches and across a crash.
///
/// Files rather than `UserDefaults` because two of the three users of this store — the
/// outbox and the position store — are written on every playback tick, and because the
/// values are already `Codable` JSON. Writes are atomic, so a kill mid-write leaves the
/// previous value rather than half of the new one.
public struct FileKeyValueStore: KeyValueStore {
    private let directory: URL

    public init(directory: URL) throws {
        self.directory = directory
        try FileManager.default.createDirectory(
            at: directory, withIntermediateDirectories: true
        )
    }

    private func url(forKey key: String) -> URL {
        directory.appendingPathComponent(Self.encode(key) + ".json")
    }

    public func data(forKey key: String) throws -> Data? {
        let url = url(forKey: key)
        guard FileManager.default.fileExists(atPath: url.path) else { return nil }
        return try Data(contentsOf: url)
    }

    public func set(_ data: Data?, forKey key: String) throws {
        let url = url(forKey: key)
        guard let data else {
            if FileManager.default.fileExists(atPath: url.path) {
                try FileManager.default.removeItem(at: url)
            }
            return
        }
        try data.write(to: url, options: .atomic)
    }

    public func keys(withPrefix prefix: String) throws -> [String] {
        let contents = try FileManager.default.contentsOfDirectory(
            at: directory, includingPropertiesForKeys: nil
        )
        return contents
            .filter { $0.pathExtension == "json" }
            .compactMap { Self.decode($0.deletingPathExtension().lastPathComponent) }
            .filter { $0.hasPrefix(prefix) }
            .sorted()
    }

    /// Keys carry server ids, which are opaque; percent-encode so one cannot escape the
    /// directory or collide with another key's filename.
    private static func encode(_ key: String) -> String {
        var allowed = CharacterSet.alphanumerics
        allowed.insert(charactersIn: "-_")
        return key.addingPercentEncoding(withAllowedCharacters: allowed) ?? key
    }

    private static func decode(_ name: String) -> String? {
        name.removingPercentEncoding
    }
}

/// For tests, and for a first run before any directory exists.
public final class InMemoryKeyValueStore: KeyValueStore, @unchecked Sendable {
    private var storage: [String: Data] = [:]
    private let lock = NSLock()

    public init() {}

    public func data(forKey key: String) throws -> Data? {
        lock.withLock { storage[key] }
    }

    public func set(_ data: Data?, forKey key: String) throws {
        lock.withLock { storage[key] = data }
    }

    public func keys(withPrefix prefix: String) throws -> [String] {
        lock.withLock { storage.keys.filter { $0.hasPrefix(prefix) }.sorted() }
    }
}
