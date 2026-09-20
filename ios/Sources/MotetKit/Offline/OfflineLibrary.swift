import Foundation

/// Fetch an episode's audio to a file on the device.
///
/// A protocol because the real one is a *background* `URLSession` download — which only
/// exists on iOS, only behaves correctly on a device, and cannot be exercised in a unit
/// test — while every rule about *what* to download and what to throw away is decided by
/// `DownloadPolicy` and tested here.
public protocol EpisodeDownloader: Sendable {
    /// Download `url` and place it at `destination`, replacing whatever was there.
    func download(from url: URL, to destination: URL) async throws
}

/// What the device is holding.
public struct DownloadedEpisode: Codable, Hashable, Sendable {
    public let episodeId: String
    public let fileName: String
    public let byteCount: Int
    public let downloadedAt: Date

    public init(episodeId: String, fileName: String, byteCount: Int, downloadedAt: Date) {
        self.episodeId = episodeId
        self.fileName = fileName
        self.byteCount = byteCount
        self.downloadedAt = downloadedAt
    }
}

/// Which episodes should be on the device, and which should not be any more.
///
/// Pure, and separate from the downloader, because this is the offline product decision —
/// *the best listening happens where the signal is worst*, so the newest ready episodes are
/// already on the phone before the walk starts, rather than being fetched when tapped.
public enum DownloadPolicy {
    public struct Plan: Hashable, Sendable {
        public var toDownload: [String]
        public var toEvict: [String]

        public var isEmpty: Bool { toDownload.isEmpty && toEvict.isEmpty }
    }

    /// - Parameters:
    ///   - episodes: every episode the server knows about, newest first.
    ///   - downloaded: what is on the device now.
    ///   - keep: how many of the newest ready episodes to hold.
    ///   - pinned: episodes never to evict — what is playing, and anything asked for by hand.
    public static func plan(
        episodes: [EpisodeResponse],
        downloaded: Set<String>,
        keep: Int,
        pinned: Set<String> = []
    ) -> Plan {
        let ready = episodes
            .filter { $0.episodeState.isPlayable }
            .sorted { ($0.publishedAt ?? $0.createdAt) > ($1.publishedAt ?? $1.createdAt) }
        let wanted = Set(ready.prefix(max(0, keep)).map(\.id)).union(pinned)

        let toDownload = ready.map(\.id).filter { wanted.contains($0) && !downloaded.contains($0) }
        // Evict what is no longer wanted, plus anything held for an episode the server no
        // longer lists at all: a deleted episode's bytes are dead weight.
        let known = Set(episodes.map(\.id))
        let toEvict = downloaded
            .filter { !wanted.contains($0) || !known.contains($0) }
            .sorted()
        return Plan(toDownload: toDownload, toEvict: toEvict)
    }
}

/// The device's copy of the audio, and the manifest that says what it holds.
public actor OfflineLibrary {
    private let store: any KeyValueStore
    private let directory: URL
    private let downloader: any EpisodeDownloader
    private let clock: any MotetClock
    private let fileManager: FileManager
    private var manifest: [String: DownloadedEpisode] = [:]
    private var loaded = false
    private var inFlight: Set<String> = []

    private static let manifestKey = "offline.manifest"

    public init(
        store: any KeyValueStore,
        directory: URL,
        downloader: any EpisodeDownloader,
        clock: any MotetClock = SystemClock(),
        fileManager: FileManager = .default
    ) throws {
        self.store = store
        self.directory = directory
        self.downloader = downloader
        self.clock = clock
        self.fileManager = fileManager
        try fileManager.createDirectory(at: directory, withIntermediateDirectories: true)
    }

    private func loadIfNeeded() throws {
        guard !loaded else { return }
        let entries = (try? store.value([DownloadedEpisode].self, forKey: Self.manifestKey)) ?? []
        manifest = Dictionary(uniqueKeysWithValues: entries.map { ($0.episodeId, $0) })
        // The manifest and the disk can disagree — iOS evicts files under storage pressure,
        // and a crash between the write and the manifest update leaves a phantom. Trust the
        // disk, which is what playback will actually open.
        manifest = manifest.filter { fileManager.fileExists(atPath: url(for: $0.value).path) }
        repairFileNames()
        sweepStagingFiles()
        loaded = true
        try persist()
    }

    /// Remove any `.download` a transfer left behind by dying between landing the bytes and
    /// naming them. Such a file is outside the manifest — uncounted, unevicted, and only
    /// ever overwritten by a download of the same episode — so nothing else would touch
    /// it. Safe here and only here: load runs once, before any download can be in flight.
    private func sweepStagingFiles() {
        guard let names = try? fileManager.contentsOfDirectory(atPath: directory.path) else {
            return
        }
        for name in names where name.hasSuffix(".\(Self.stagingExtension)") {
            try? fileManager.removeItem(at: directory.appendingPathComponent(name))
        }
    }

    /// Give a file an extension AVFoundation recognises, where an older build did not.
    ///
    /// Every download used to be named `<episode-id>.audio`, which no player can open — see
    /// `AudioFileFormat`. Renaming rather than evicting is the point: the bytes are good, and
    /// making a phone re-fetch a walk's worth of episodes over cellular to correct a filename
    /// would be a worse answer than reading the first few kilobytes of each. A file that is
    /// not audio at all is dropped instead, and the next sync fetches the episode again.
    private func repairFileNames() {
        for (episodeId, entry) in Array(manifest) {
            let current = url(for: entry)
            guard AudioFileFormat(pathExtension: current.pathExtension) == nil else { continue }
            guard let format = try? Self.audioFormat(ofFileAt: current, fileManager: fileManager)
            else {
                try? fileManager.removeItem(at: current)
                manifest[episodeId] = nil
                continue
            }
            let renamed = Self.fileName(for: episodeId, format: format)
            do {
                try replaceItem(at: directory.appendingPathComponent(renamed), movingFrom: current)
            } catch {
                try? fileManager.removeItem(at: current)
                manifest[episodeId] = nil
                continue
            }
            manifest[episodeId] = DownloadedEpisode(
                episodeId: episodeId,
                fileName: renamed,
                byteCount: entry.byteCount,
                downloadedAt: entry.downloadedAt
            )
        }
    }

    private func url(for entry: DownloadedEpisode) -> URL {
        directory.appendingPathComponent(entry.fileName)
    }

    private func persist() throws {
        try store.setValue(manifest.values.sorted { $0.episodeId < $1.episodeId }, forKey: Self.manifestKey)
    }

    public func downloadedEpisodeIds() throws -> Set<String> {
        try loadIfNeeded()
        return Set(manifest.keys)
    }

    public func localURL(forEpisode episodeId: String) throws -> URL? {
        try loadIfNeeded()
        guard let entry = manifest[episodeId] else { return nil }
        return url(for: entry)
    }

    public func totalBytes() throws -> Int {
        try loadIfNeeded()
        return manifest.values.reduce(0) { $0 + $1.byteCount }
    }

    /// Fetch one episode's audio. Idempotent, and a no-op if it is already held.
    @discardableResult
    public func download(episodeId: String, from url: URL) async throws -> URL {
        try loadIfNeeded()
        if let existing = try localURL(forEpisode: episodeId) { return existing }
        guard !inFlight.contains(episodeId) else { throw MotetError.transport(CancellationError()) }
        inFlight.insert(episodeId)
        defer { inFlight.remove(episodeId) }

        // Downloaded to a staging name first, because the name it has to end up with depends
        // on what came back: AVFoundation reads a local file's format off its extension, so
        // the extension has to be the one the bytes actually are.
        let staging = directory.appendingPathComponent(Self.stagingFileName(for: episodeId))
        try? fileManager.removeItem(at: staging)
        try await downloader.download(from: url, to: staging)

        let format: AudioFileFormat
        do {
            format = try Self.audioFormat(ofFileAt: staging, fileManager: fileManager)
        } catch {
            // An unplayable file must not enter the library. Held, it would shadow the API
            // for this episode forever — `source(forEpisode:)` prefers the device — and the
            // listener would get -11828 on every tap with no way to clear it. Dropped, the
            // next tap streams, and the next sync tries the download again.
            try? fileManager.removeItem(at: staging)
            throw error
        }

        let fileName = Self.fileName(for: episodeId, format: format)
        let destination = directory.appendingPathComponent(fileName)
        try replaceItem(at: destination, movingFrom: staging)

        let attributes = try? fileManager.attributesOfItem(atPath: destination.path)
        let size = attributes?[.size] as? Int
        let entry = DownloadedEpisode(
            episodeId: episodeId,
            fileName: fileName,
            byteCount: size ?? 0,
            downloadedAt: clock.now
        )
        manifest[episodeId] = entry
        try persist()
        return destination
    }

    public func remove(episodeId: String) throws {
        try loadIfNeeded()
        guard let entry = manifest[episodeId] else { return }
        let target = url(for: entry)
        if fileManager.fileExists(atPath: target.path) {
            try fileManager.removeItem(at: target)
        }
        manifest[episodeId] = nil
        try persist()
    }

    /// Apply a plan: download what is missing, drop what is no longer wanted.
    ///
    /// Failures are collected rather than thrown: one episode that will not download is not
    /// a reason to leave the other four undownloaded before a walk.
    @discardableResult
    public func apply(
        _ plan: DownloadPolicy.Plan,
        audioURL: @Sendable (String) throws -> URL
    ) async -> [String: Error] {
        var failures: [String: Error] = [:]
        for episodeId in plan.toEvict {
            do { try remove(episodeId: episodeId) } catch { failures[episodeId] = error }
        }
        for episodeId in plan.toDownload {
            do {
                try await download(episodeId: episodeId, from: try audioURL(episodeId))
            } catch {
                failures[episodeId] = error
            }
        }
        return failures
    }

    /// Move `source` onto `target`, replacing whatever is there.
    private func replaceItem(at target: URL, movingFrom source: URL) throws {
        if fileManager.fileExists(atPath: target.path) {
            try fileManager.removeItem(at: target)
        }
        try fileManager.moveItem(at: source, to: target)
    }

    /// What kind of audio the file holds, or `UnplayableAudioError` if it is not audio.
    private static func audioFormat(
        ofFileAt url: URL, fileManager: FileManager
    ) throws -> AudioFileFormat {
        let head = try head(ofFileAt: url)
        guard let format = AudioFileFormat.sniff(head) else {
            let attributes = try? fileManager.attributesOfItem(atPath: url.path)
            throw UnplayableAudioError(
                byteCount: attributes?[.size] as? Int ?? head.count, head: head
            )
        }
        return format
    }

    /// The first bytes of a file — enough to identify it, and never the whole episode.
    private static func head(ofFileAt url: URL) throws -> Data {
        let handle = try FileHandle(forReadingFrom: url)
        defer { try? handle.close() }
        return try handle.read(upToCount: AudioFileFormat.headLength) ?? Data()
    }

    /// Episode ids are opaque server strings; keep them out of the filename.
    ///
    /// The extension is the format's own, because AVFoundation opens a local file by its
    /// extension and by nothing else. It used to be a constant `.audio`, which is why no
    /// downloaded episode played — see `AudioFileFormat`.
    private static func fileName(for episodeId: String, format: AudioFileFormat) -> String {
        "\(safeName(for: episodeId)).\(format.pathExtension)"
    }

    /// Where a download lands before its format is known. Deliberately not an audio
    /// extension: a half-written transfer must never look like something playable.
    private static func stagingFileName(for episodeId: String) -> String {
        "\(safeName(for: episodeId)).\(stagingExtension)"
    }

    private static let stagingExtension = "download"

    private static func safeName(for episodeId: String) -> String {
        var allowed = CharacterSet.alphanumerics
        allowed.insert(charactersIn: "-_")
        return episodeId.addingPercentEncoding(withAllowedCharacters: allowed) ?? "episode"
    }
}
