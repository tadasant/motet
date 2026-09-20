import Foundation

extension NewsItemResponse {
    /// What a backlog row should call this story.
    ///
    /// The server decides it (`display_title`): a story with one source is called what that
    /// newsletter called itself, verbatim; a merged one wears the title dedup wrote, which
    /// is the only one that can name several write-ups at once. The field is absent on an
    /// API older than it — and on this app's own cache of one — so the story's stored title
    /// is the fallback, which is what every row said before.
    public var listTitle: String {
        guard let displayTitle, !displayTitle.isEmpty else { return title }
        return displayTitle
    }

    /// How many write-ups were deduped into this story. One needs no affordance; more do.
    public var sourceCount: Int { sources.count }
}
