import XCTest

@testable import MotetKit

/// Where an episode is between "make it" and a file to play, on the phone.
///
/// The port of the SPA's `episodeProgress.test.tsx`, assertion for assertion, because the
/// two clients read one server field and a drift between them is a drift a person sees.
final class EpisodeProgressTests: XCTestCase {
    private func progress(
        step: String? = "assemble",
        stage: String = "queued",
        stepsDone: Int = 0,
        newsItems: Int = 0,
        claims: Int = 0,
        segmentsRendered: Int = 0,
        elapsedMs: Int = 12_000,
        estimateMs: Int? = nil,
        estimateSamples: Int = 0,
        attempt: Int = 0,
        error: String? = nil,
        waitingOnWorker: Bool = false,
        workerStarting: Bool? = false
    ) -> EpisodeBuildProgress {
        EpisodeBuildProgress(
            attempt: attempt, claims: claims, elapsedMs: elapsedMs, error: error,
            estimateMs: estimateMs, estimateSamples: estimateSamples, maxAttempts: 5,
            newsItems: newsItems, nextAttemptAt: nil, segmentsRendered: segmentsRendered,
            stage: stage, step: step, stepsDone: stepsDone, stepsTotal: 3,
            waitingOnWorker: waitingOnWorker, workerStarting: workerStarting
        )
    }

    // MARK: - The clock

    func testTheClockReadsAsTimeRatherThanAMillisecondCount() {
        XCTAssertEqual(EpisodeProgress.elapsed(0), "just started")
        XCTAssertEqual(EpisodeProgress.elapsed(12_000), "12s")
        XCTAssertEqual(EpisodeProgress.elapsed(93_000), "1m 33s")
        XCTAssertEqual(EpisodeProgress.elapsed(3_900_000), "1h 05m")
    }

    // MARK: - The steps

    func testAQueuedStepIsNamedRatherThanReportedAsABareQueued() {
        let expected = [
            ("assemble", "Waiting for a worker to start"),
            ("script", "Queued to write the script"),
            ("tts", "Queued to record the audio"),
        ]
        for (step, headline) in expected {
            XCTAssertEqual(EpisodeProgress.describe(progress(step: step)).headline, headline)
        }
    }

    func testAQueuedStepSaysWhichOfThreeItIs() {
        let shown = EpisodeProgress.describe(progress(step: "script", stepsDone: 1))
        XCTAssertEqual(shown.detail, "Step 2 of 3 · writing the script")
    }

    func testScriptingCountsTheStoriesAssemblyChose() {
        let shown = EpisodeProgress.describe(
            progress(step: "script", stage: "running", stepsDone: 1, newsItems: 8)
        )
        XCTAssertEqual(shown.headline, "Writing the script for 8 stories")
        XCTAssertEqual(shown.count, "8 stories")
    }

    func testTheRenderIsCountedBecauseItIsTheOneStepWithAnInside() {
        let shown = EpisodeProgress.describe(
            progress(
                step: "tts", stage: "running", stepsDone: 2, newsItems: 9, segmentsRendered: 4
            )
        )
        XCTAssertEqual(shown.headline, "Recording the audio")
        XCTAssertEqual(shown.count, "Recorded 4 of 9 segments · 5 left")
        XCTAssertEqual(shown.fraction ?? 0, 4.0 / 9.0, accuracy: 0.0001)
    }

    func testTheBarIsIndeterminateWhereThereIsNothingHonestToCount() {
        XCTAssertNil(EpisodeProgress.describe(progress(stage: "running")).fraction)
        XCTAssertNil(
            EpisodeProgress.describe(progress(step: "script", stage: "running")).fraction
        )
    }

    // MARK: - The clock, and the estimate

    func testTooFewFinishedEpisodesShowsElapsedTimeAlone() {
        XCTAssertEqual(EpisodeProgress.describe(progress(elapsedMs: 45_000)).timing, "45s so far")
    }

    func testAnEstimateIsLabelledAsOneAndSaysWhatItIsMadeOf() {
        let shown = EpisodeProgress.describe(
            progress(elapsedMs: 45_000, estimateMs: 93_000, estimateSamples: 5)
        )
        XCTAssertEqual(
            shown.timing,
            "45s so far · usually about 1m 33s (an estimate, from the last 5 episodes)"
        )
    }

    func testTheEstimateIsNeverACountdownThatCouldGoNegative() {
        let shown = EpisodeProgress.describe(
            progress(elapsedMs: 600_000, estimateMs: 93_000, estimateSamples: 5)
        )
        XCTAssertEqual(shown.timing?.contains("10m 00s so far"), true)
        XCTAssertEqual(shown.timing?.contains("remaining"), false)
        XCTAssertEqual(shown.timing?.contains("-"), false)
    }

    // MARK: - Going wrong

    func testABuildNothingWillRunSaysSoRatherThanSpinning() {
        let shown = EpisodeProgress.describe(progress(waitingOnWorker: true))
        XCTAssertEqual(shown.tone, .stalled)
        XCTAssertEqual(shown.detail, EpisodeProgress.noWorker)
        XCTAssertFalse(EpisodeProgress.moving(progress(waitingOnWorker: true)))
        XCTAssertTrue(EpisodeProgress.inFlight(progress(waitingOnWorker: true)))
    }

    func testAWorkerJustAskedForIsStartingNotMissing() {
        let shown = EpisodeProgress.describe(progress(workerStarting: true))
        XCTAssertEqual(shown.headline, "Starting a worker to build the episode")
        XCTAssertEqual(shown.detail, SourceStatus.workerStarting)
        XCTAssertEqual(shown.tone, .working)
    }

    func testAnAPIWithoutWorkerStartingReadsAsNotStarting() {
        let shown = EpisodeProgress.describe(progress(workerStarting: nil))
        XCTAssertEqual(shown.headline, "Waiting for a worker to start")
    }

    func testARetryReportsTheAttemptTheStepAndWhatTheLastOneSaid() {
        let shown = EpisodeProgress.describe(
            progress(step: "script", stage: "retrying", attempt: 3, error: "OpenRouter 429")
        )
        XCTAssertEqual(shown.headline, "Trying again after a failure (attempt 3 of 5)")
        XCTAssertEqual(shown.detail, "Stopped while writing the script. Last attempt: OpenRouter 429")
        XCTAssertEqual(shown.tone, .working)
    }

    func testAFailureNamesTheStepItStoppedAtAndWhatToDo() {
        let shown = EpisodeProgress.describe(
            progress(
                step: "tts", stage: "failed", stepsDone: 2, error: "Cartesia refused the text"
            )
        )
        XCTAssertEqual(shown.headline, "Stopped while recording the audio")
        XCTAssertEqual(shown.detail?.contains("Cartesia refused the text"), true)
        XCTAssertEqual(shown.detail?.contains("make the episode again"), true)
        XCTAssertEqual(shown.tone, .error)
        // Determinate, so the bar stands still rather than sweeping over a stopped build.
        XCTAssertEqual(shown.fraction ?? 0, 2.0 / 3.0, accuracy: 0.0001)
    }

    func testAFinishedEpisodeSaysHowLongItTook() {
        let shown = EpisodeProgress.describe(
            progress(step: nil, stage: "ready", stepsDone: 3, elapsedMs: 93_000)
        )
        XCTAssertEqual(shown.headline, "Ready to play · made in 1m 33s")
        XCTAssertEqual(shown.tone, .done)
        XCTAssertFalse(EpisodeProgress.inFlight(progress(step: nil, stage: "ready")))
    }

    func testAStageThisBuildDoesNotKnowIsSaidPlainlyRatherThanGuessedAt() {
        // The app ships through App Store review and the API does not.
        let shown = EpisodeProgress.describe(progress(stage: "transcoding"))
        XCTAssertEqual(shown.headline, "Working")
    }

    // MARK: - The row

    func testARowIsOneClauseOfStepCountAndClock() {
        XCTAssertEqual(
            EpisodeProgress.describeRow(progress(elapsedMs: 12_000)),
            "queued · choosing the stories · 12s"
        )
        XCTAssertEqual(
            EpisodeProgress.describeRow(
                progress(
                    step: "tts", stage: "running", newsItems: 9, segmentsRendered: 4,
                    elapsedMs: 93_000
                )
            ),
            "recording the audio, 4 of 9 · 1m 33s"
        )
        XCTAssertEqual(EpisodeProgress.describeRow(progress(stage: "failed")), "stopped")
    }
}
