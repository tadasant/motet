import CarPlay
import MotetKit
import UIKit

/// CarPlay: the same episodes, the same commands, no screen worth looking at.
///
/// **Written, not runnable.** A CarPlay audio app needs the
/// `com.apple.developer.carplay-audio` entitlement, which Apple grants by manual review
/// *after* Developer Program enrolment — so this scene cannot be launched, on a device or in
/// the simulator, until that request is approved. It is here because the shape of the app
/// depends on it: the reason every control is a `PlaybackCommand` rather than a method on a
/// view model is that this file and the lockscreen have to reach the same set.
///
/// Two rules that CarPlay enforces and a phone does not:
///
/// * **The template hierarchy is at most five deep, and lists are capped.** A list template
///   with hundreds of rows is rejected, so the episode list is trimmed to what a driver would
///   plausibly pick from.
/// * **Nothing may require reading.** The list rows are the episode title and one line of
///   status; there is no transcript, and there are no claims — those belong to the phone,
///   where the trust surface can actually be read.
@MainActor
final class CarPlaySceneDelegate: UIResponder, CPTemplateApplicationSceneDelegate {
    private var interfaceController: CPInterfaceController?
    private var refreshTask: Task<Void, Never>?

    /// How many episodes to offer in the car. A driver scrolling a long list is the failure
    /// mode CarPlay's own limits exist to prevent.
    private static let maximumRows = 12

    func templateApplicationScene(
        _ templateApplicationScene: CPTemplateApplicationScene,
        didConnect interfaceController: CPInterfaceController
    ) {
        self.interfaceController = interfaceController
        interfaceController.setRootTemplate(makeLoadingTemplate(), animated: false, completion: nil)
        refresh()
    }

    func templateApplicationScene(
        _ templateApplicationScene: CPTemplateApplicationScene,
        didDisconnectInterfaceController interfaceController: CPInterfaceController
    ) {
        refreshTask?.cancel()
        self.interfaceController = nil
    }

    // MARK: - Templates

    private func makeLoadingTemplate() -> CPListTemplate {
        let template = CPListTemplate(title: "Motet", sections: [])
        template.emptyViewSubtitleVariants = ["Loading your episodes…"]
        return template
    }

    private func refresh() {
        refreshTask?.cancel()
        refreshTask = Task {
            let environment = AppEnvironment.shared
            // Offline first: the car is where the signal is worst, so a cached list plus the
            // downloaded audio is the expected case rather than the fallback.
            let episodes =
                (try? await environment.library.episodes())
                ?? (try? await environment.library.cachedEpisodes())
                ?? []
            let downloaded = (try? await environment.library.downloadedEpisodeIds()) ?? []
            guard !Task.isCancelled else { return }
            let template = makeListTemplate(episodes: episodes, downloaded: downloaded)
            interfaceController?.setRootTemplate(template, animated: true, completion: nil)
        }
    }

    private func makeListTemplate(
        episodes: [EpisodeResponse], downloaded: Set<String>
    ) -> CPListTemplate {
        let playable = episodes.filter { $0.episodeState.isPlayable }.prefix(Self.maximumRows)
        let items: [CPListItem] = playable.map { episode in
            let detail = downloaded.contains(episode.id)
                ? "\(Format.duration(episode.durationMs)) · on this phone"
                : Format.duration(episode.durationMs)
            let item = CPListItem(text: episode.title, detailText: detail)
            item.handler = { [weak self] _, completion in
                Task { @MainActor in
                    await self?.play(episode: episode)
                    // CarPlay wants to know the tap has been dealt with; not calling this
                    // leaves the row spinning forever.
                    completion()
                }
            }
            return item
        }

        let template = CPListTemplate(
            title: "Motet", sections: [CPListSection(items: items)]
        )
        template.emptyViewSubtitleVariants = ["Nothing to play yet"]
        return template
    }

    /// Start playback and hand the driver to the system's Now Playing screen, which is where
    /// the transport controls live — CarPlay renders those from `MPNowPlayingInfoCenter` and
    /// `MPRemoteCommandCenter`, the same two the lockscreen uses.
    private func play(episode: EpisodeResponse) async {
        let environment = AppEnvironment.shared
        do {
            try environment.audioSession.activate()
            let source = try await environment.library.source(forEpisode: episode)
            try await environment.controller.load(
                episode: episode, source: source, autoplay: true
            )
            interfaceController?.pushTemplate(
                CPNowPlayingTemplate.shared, animated: true, completion: nil
            )
        } catch {
            let alert = CPAlertTemplate(
                titleVariants: ["Could not play that"],
                actions: [CPAlertAction(title: "OK", style: .default) { [weak self] _ in
                    self?.interfaceController?.dismissTemplate(animated: true, completion: nil)
                }]
            )
            interfaceController?.presentTemplate(alert, animated: true, completion: nil)
        }
    }
}
