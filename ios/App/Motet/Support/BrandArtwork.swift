import MediaPlayer
import UIKit

/// The mark, as artwork for the surfaces that show one: Now Playing and CarPlay list rows.
///
/// Deliberately not isolated to the main actor. MediaPlayer calls an artwork's request
/// handler on a queue of its own, and a closure formed inside a `@MainActor` context would
/// carry that isolation and trap when it is called from anywhere else.
enum BrandArtwork {
    /// The asset catalog name of the mark, which is also the app icon's image.
    static let markName = "MotetMark"

    static func mark() -> UIImage? {
        UIImage(named: markName)
    }

    static func nowPlaying() -> MPMediaItemArtwork? {
        guard let image = mark() else { return nil }
        return MPMediaItemArtwork(boundsSize: image.size) { _ in image }
    }
}
