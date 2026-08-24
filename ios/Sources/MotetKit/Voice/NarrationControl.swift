import Foundation

/// The seam the voice path will attach to — and nothing more.
///
/// Phase 2's interactive half lives in `voice/` (a Pipecat service reached through our own
/// API, never a vendor SDK in the app — invariant 1). When barge-in arrives, it needs
/// exactly three things from the player: stop the narration, know where it stopped, and
/// start it again. That is this protocol.
///
/// It is declared here, unimplemented by anything voice-related, on purpose. Writing the
/// seam costs nothing and settles the shape; writing a stub *implementation* would be
/// pretending, and the barge-in path is another session's work.
///
/// Two properties the voice side must be able to rely on, which is why it is a protocol
/// over `PlaybackController` rather than over the audio engine:
///
/// * pausing for a question does not lose the position — the position is ours (invariant 4);
/// * whatever the listener says resolves to a `PlaybackCommand`, so an interactive command
///   can never do something a button could not.
public protocol NarrationControl: Sendable {
    /// Stop narration so the listener can be heard. Returns where it stopped.
    func suspendNarration() async -> Int
    /// Start again from where narration stopped.
    func resumeNarration() async
    /// What is being spoken right now, for the assistant's context.
    func narrationContext() async -> NarrationContext
}

/// What the voice session would need to know to answer "wait, what was that?".
public struct NarrationContext: Hashable, Sendable {
    public let episodeId: String?
    public let positionMs: Int
    public let currentNewsItemId: String?
    public let currentNewsItemTitle: String?

    public init(
        episodeId: String?,
        positionMs: Int,
        currentNewsItemId: String?,
        currentNewsItemTitle: String?
    ) {
        self.episodeId = episodeId
        self.positionMs = positionMs
        self.currentNewsItemId = currentNewsItemId
        self.currentNewsItemTitle = currentNewsItemTitle
    }
}

extension PlaybackController: NarrationControl {
    public func suspendNarration() async -> Int {
        await perform(.pause)
        return snapshot().positionMs
    }

    public func resumeNarration() async {
        await perform(.play)
    }

    public func narrationContext() async -> NarrationContext {
        let current = snapshot()
        return NarrationContext(
            episodeId: current.episodeId,
            positionMs: current.positionMs,
            currentNewsItemId: currentNewsItemId(),
            currentNewsItemTitle: current.currentSegmentTitle
        )
    }
}
