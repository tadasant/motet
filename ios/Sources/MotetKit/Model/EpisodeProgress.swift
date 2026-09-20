import Foundation

/// The pure half of "where is my episode": `build_progress` turned into the four things a
/// person waiting for one needs — which step, how far through it, how long it has taken,
/// and what to do if it stopped.
///
/// A port of the SPA's `screens/episodeProgress.ts`, rule for rule and word for word, kept
/// in MotetKit so the rules are tested on Linux and the two clients give one reading per
/// episode. Where the SPA's copy changes, change this with it.
///
/// Nothing here infers anything from how long the screen has been watching. An episode that
/// takes twenty minutes is reported step by step, and one nothing will build says so.
public enum EpisodeProgress {
    /// What a build is doing, as the pieces a view lays out. The SPA's `StageDescription`.
    public struct Description: Hashable, Sendable {
        public enum Tone: Hashable, Sendable { case working, stalled, done, error }
        /// The step. Never a bare "Working…".
        public var headline: String
        /// The work done against the work there is, once there is a number for it.
        public var count: String?
        /// How long it has taken, and how long one usually takes.
        public var timing: String?
        /// Why it is retrying, why it stopped, or what happens next.
        public var detail: String?
        /// A determinate bar; nil is an indeterminate one, for the steps before anything
        /// can be counted. A bar with no denominator would be a made-up number.
        public var fraction: Double?
        public var tone: Tone
    }

    /// Stages in which something is still happening, so the screen keeps asking.
    static let inFlightStages: Set<String> = ["queued", "running", "retrying"]

    /// Whether the build is still going: the screen polls and the row reads as working.
    public static func inFlight(_ progress: EpisodeBuildProgress?) -> Bool {
        guard let progress else { return false }
        return inFlightStages.contains(progress.stage)
    }

    /// Whether it is worth re-reading every two seconds: in flight, and something is on it.
    /// A build no worker will run moves when a worker appears, which is not a two-second
    /// question, so the watch slows rather than polling a stall forever. `SourceStatus`'
    /// `syncMoving`, one pipeline along.
    public static func moving(_ progress: EpisodeBuildProgress?) -> Bool {
        inFlight(progress) && progress?.waitingOnWorker == false
    }

    /// What each step is called where a sentence needs its name rather than its verb.
    public static func stepName(_ step: String?) -> String? {
        switch step {
        case "assemble": return "choosing the stories"
        case "script": return "writing the script"
        case "tts": return "recording the audio"
        default: return nil
        }
    }

    /// `1,480`, as the SPA's `toLocaleString('en-US')` writes it. `SourceStatus`' own
    /// hand-rolled separator, reused rather than copied — by hand on both sides so the
    /// Linux build and the phone cannot disagree about it.
    static func number(_ value: Int) -> String { SourceStatus.number(value) }

    static let noWorker =
        "No worker has run in the last five minutes, so this will not move until one does."

    /// A worker has been asked for and its container has not appeared yet. The sync
    /// panel's own sentence (`SourceStatus.workerStarting`, motet#137), reused rather than
    /// rephrased: the two panels sit on one phone and must not disagree about what a
    /// starting worker is called.
    static var workerStarting: String { SourceStatus.workerStarting }

    /// `1m 33s`, `12s`, `1h 04m`. Short enough to sit inside a sentence, and never `0s` for
    /// something that has only just started — `just started` is what that actually means.
    public static func elapsed(_ ms: Int) -> String {
        let seconds = max(0, Int((Double(ms) / 1000).rounded()))
        if seconds < 1 { return "just started" }
        if seconds < 60 { return "\(seconds)s" }
        let minutes = seconds / 60
        if minutes < 60 { return "\(minutes)m \(pad(seconds % 60))s" }
        return "\(minutes / 60)h \(pad(minutes % 60))m"
    }

    private static func pad(_ value: Int) -> String {
        value < 10 ? "0\(value)" : "\(value)"
    }

    /// How long it has taken and — only where the server had a basis for one — how long it
    /// usually takes.
    ///
    /// **Labelled as an estimate every time, and never as a countdown.** The server sends
    /// the median of the last few finished episodes and how many that was; the honest way
    /// to show that is "usually about 1m 30s", not "about 18s remaining", because
    /// subtracting one from the other produces a number that goes negative and sits there.
    public static func describeTiming(_ progress: EpisodeBuildProgress) -> String? {
        guard inFlight(progress) else { return nil }
        let so_far = "\(elapsed(progress.elapsedMs)) so far"
        guard let estimate = progress.estimateMs else { return so_far }
        let of = progress.estimateSamples == 1 ? "episode" : "episodes"
        return so_far
            + " · usually about \(elapsed(estimate)) "
            + "(an estimate, from the last \(progress.estimateSamples) \(of))"
    }

    /// "Step 2 of 3 · writing the script", the coarse position that is always available.
    static func stepLine(_ progress: EpisodeBuildProgress) -> String? {
        guard inFlight(progress), progress.step != nil else { return nil }
        let where_ = "Step \(progress.stepsDone + 1) of \(progress.stepsTotal)"
        guard let name = stepName(progress.step) else { return where_ }
        return "\(where_) · \(name)"
    }

    /// An episode being built, in words and a bar — `EpisodeBuildProgress` on the API,
    /// which joins the episode row to the three queues it travels through.
    ///
    /// The one bar with a real denominator is the render's: TTS is a loop over segments and
    /// the worker reports each one (migration 0025). Assembly and scripting are a single
    /// model call each, so there is nothing inside them to count and the bar is
    /// indeterminate rather than a fraction somebody invented.
    public static func describe(_ progress: EpisodeBuildProgress) -> Description {
        let stalled = progress.waitingOnWorker
        // Optional on the wire — an older API omits it — so absent is false.
        let starting = progress.workerStarting ?? false
        let total = progress.newsItems
        let stories = "\(number(total)) \(total == 1 ? "story" : "stories")"
        let rendering = progress.step == "tts" && total > 0
        var count: String?
        if rendering {
            count = "Recorded \(number(progress.segmentsRendered)) of \(number(total)) segments · "
                + "\(number(max(0, total - progress.segmentsRendered))) left"
        } else if progress.step == "script", total > 0 {
            count = stories
        }
        let fraction = rendering
            ? min(1, Double(progress.segmentsRendered) / Double(total)) : nil
        let timing = describeTiming(progress)

        func working(_ headline: String, _ detail: String?) -> Description {
            Description(
                headline: headline, count: count, timing: timing,
                detail: stalled ? noWorker : (starting ? workerStarting : detail),
                fraction: fraction, tone: stalled ? .stalled : .working
            )
        }

        switch progress.stage {
        case "ready":
            return Description(
                headline: "Ready to play · made in \(elapsed(progress.elapsedMs))",
                count: nil, timing: nil, detail: nil, fraction: 1, tone: .done
            )
        case "failed":
            let step = stepName(progress.step)
            return Description(
                headline: step.map { "Stopped while \($0)" } ?? "This episode could not be made",
                count: nil, timing: nil,
                detail: (progress.error.map { "\($0) " } ?? "")
                    + "Nothing will move it on its own — make the episode again.",
                // How far it got, as a *determinate* bar: nil would draw the indeterminate
                // one, and a moving bar over a build that has stopped is a lie.
                fraction: Double(progress.stepsDone) / Double(progress.stepsTotal),
                tone: .error
            )
        case "retrying":
            let step = stepName(progress.step) ?? "a step"
            let attempt = progress.attempt > 0
                ? " (attempt \(progress.attempt) of \(progress.maxAttempts))" : ""
            return working(
                "Trying again after a failure\(attempt)",
                progress.error.map { "Stopped while \(step). Last attempt: \($0)" }
            )
        case "queued":
            switch progress.step {
            case "assemble":
                return working(
                    starting ? "Starting a worker to build the episode" : "Waiting for a worker to start",
                    stepLine(progress)
                )
            case "script": return working("Queued to write the script", stepLine(progress))
            case "tts": return working("Queued to record the audio", stepLine(progress))
            default: return working("Queued", stepLine(progress))
            }
        case "running":
            switch progress.step {
            case "assemble":
                return working(
                    "Choosing which stories fit",
                    "Everything unread, oldest first, up to the length you asked for."
                )
            case "script":
                return working(
                    total > 0 ? "Writing the script for \(stories)" : "Writing the script",
                    "One pass over every story, with the source span behind each claim."
                )
            case "tts":
                return working("Recording the audio", stepLine(progress))
            default:
                return working("Working", stepLine(progress))
            }
        default:
            // A stage this build does not know — a newer API. Said plainly, not guessed at.
            return working("Working", stepLine(progress))
        }
    }

    /// The same reading squeezed into one clause, for a list row.
    ///
    /// A row is a glance and the detail is one tap in, so what a row gets is the step and
    /// the clock rather than the bar, the estimate and the remedy. The SPA's
    /// `describeRowProgress`.
    public static func describeRow(_ progress: EpisodeBuildProgress) -> String {
        let took = elapsed(progress.elapsedMs)
        if progress.stage == "ready" { return "made in \(took)" }
        if progress.stage == "failed" { return "stopped" }
        let step = stepName(progress.step)
        let where_: String
        switch progress.stage {
        case "queued": where_ = step.map { "queued · \($0)" } ?? "queued"
        case "retrying": where_ = "trying again · \(step ?? "a step")"
        default: where_ = step ?? "working"
        }
        let counted = progress.step == "tts" && progress.newsItems > 0
            ? "\(where_), \(number(progress.segmentsRendered)) of \(number(progress.newsItems))"
            : where_
        return "\(counted) · \(took)"
    }
}

extension EpisodeResponse {
    /// Where this episode is in the factory, or nil for one whose state is the whole answer.
    public var build: EpisodeBuildProgress? { buildProgress }

    /// Whether the app should keep polling it. `build_progress` is the server's own reading
    /// and outranks the state word, which cannot tell a queued step from a running one —
    /// but an API older than this field still answers through `episodeState`.
    public var isBuilding: Bool {
        guard let buildProgress else { return episodeState.isInProgress }
        return EpisodeProgress.inFlight(buildProgress)
    }
}
