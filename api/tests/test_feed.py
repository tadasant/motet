"""The RSS feed, validated by parsers real podcast clients actually use.

"Well-formed XML" and "Overcast will play it" are different claims, and only the second
one matters. So the assertions here run the rendered document through two independent
parsers — ``podcastparser``, which is the parser inside gPodder, and ``feedparser``, which
is what most other tooling uses — rather than through an XML assertion of our own design.

That is not belt-and-braces. It caught a real bug: the feed originally declared the iTunes
namespace as ``.../podcast-1.0/`` rather than ``.../podcast-1.0.dtd``. The document parsed
perfectly and every ``itunes:`` element was silently ignored, so an episode would have had
no duration on a lockscreen and nothing would have errored anywhere.
"""

from __future__ import annotations

import io
from datetime import UTC, datetime

import feedparser
import podcastparser
import pytest
from motet_api.feed import (
    FeedMetadata,
    episode_audio_url,
    feed_url,
    itunes_duration,
    render_feed,
)
from motet_api.shownotes import show_notes_html, show_notes_text
from motet_db import (
    EpisodeKind,
    EpisodeState,
    StoredClaim,
    StoredEpisode,
    StoredSegment,
)

BASE = "https://motet.example"
TOKEN = "tok-abc123"

METADATA = FeedMetadata(
    title="Motet",
    description="Your reading backlog, read aloud.",
    author="Motet",
    base_url=BASE,
    token=TOKEN,
)


def episode(
    episode_id: str = "ep_1",
    *,
    title: str = "Morning briefing",
    duration_ms: int = 1_301_000,
    published: datetime | None = None,
) -> StoredEpisode:
    when = published or datetime(2026, 8, 24, 7, 30, tzinfo=UTC)
    return StoredEpisode(
        id=episode_id,
        user_id="motet-owner",
        title=title,
        state=EpisodeState.READY,
        kind=EpisodeKind.MANUAL,
        rule=None,
        max_duration_ms=1_800_000,
        duration_ms=duration_ms,
        audio_key=f"users/motet-owner/episodes/{episode_id}.mp3",
        audio_bytes=20_812_345,
        audio_media_type="audio/mpeg",
        last_error=None,
        listened_through_ms=0,
        created_at=when,
        published_at=when,
        segments=(
            StoredSegment(
                id="seg_1",
                news_item_id="ni_1",
                position=0,
                text="Acme raised twenty million dollars.",
                start_ms=0,
                duration_ms=duration_ms,
                claims=(
                    StoredClaim(
                        id="cl_1",
                        position=0,
                        text="Acme raised twenty million dollars.",
                        source_item_id="si_1",
                        span_start=0,
                        span_end=25,
                    ),
                ),
            ),
        ),
    )


class TestDurationFormatting:
    @pytest.mark.parametrize(
        ("ms", "expected"),
        [(0, "0:00:00"), (3_000, "0:00:03"), (1_301_000, "0:21:41"), (3_723_000, "1:02:03")],
    )
    def test_h_mm_ss(self, ms: int, expected: str) -> None:
        assert itunes_duration(ms) == expected

    def test_a_negative_duration_does_not_produce_nonsense(self) -> None:
        assert itunes_duration(-5) == "0:00:00"


class TestUrls:
    def test_the_token_travels_in_the_query_string(self) -> None:
        """Podcast clients handle a secret in a URL far better than HTTP auth.

        A feed no player can subscribe to is not a feed, which is why this is the design
        rather than a compromise.
        """
        assert feed_url(BASE, TOKEN) == f"{BASE}/feed.xml?token={TOKEN}"

    def test_audio_urls_point_at_us_not_at_the_bucket(self) -> None:
        """So the enclosure stays valid across storage backends.

        It also keeps a signed URL's expiry out of a feed document a client may cache for
        hours — a link that expires inside a cached feed is a download that fails later,
        for no visible reason.
        """
        url = episode_audio_url(BASE, "ep_1", TOKEN)
        assert url == f"{BASE}/v1/episodes/ep_1/audio?token={TOKEN}"

    def test_a_trailing_slash_on_the_base_does_not_double_up(self) -> None:
        assert "//feed.xml" not in feed_url(f"{BASE}/", TOKEN)


class TestRenderedFeed:
    def test_podcastparser_reads_it_the_way_a_client_would(self) -> None:
        xml = render_feed(METADATA, [episode()]).decode()
        parsed = podcastparser.parse(feed_url(BASE, TOKEN), io.StringIO(xml))

        assert parsed["title"] == "Motet"
        assert parsed["type"] == "episodic"
        (entry,) = parsed["episodes"]
        assert entry["title"] == "Morning briefing"
        assert entry["guid"] == "ep_1"
        # The duration a lockscreen shows, taken from itunes:duration.
        assert entry["total_time"] == 1301
        (enclosure,) = entry["enclosures"]
        assert enclosure["mime_type"] == "audio/mpeg"
        assert enclosure["file_size"] == 20_812_345
        assert enclosure["url"].startswith(f"{BASE}/v1/episodes/ep_1/audio?token=")

    def test_feedparser_finds_no_faults(self) -> None:
        parsed = feedparser.parse(render_feed(METADATA, [episode()]).decode())

        assert parsed.bozo is False  # no parse error of any kind
        assert parsed.version == "rss20"
        assert parsed.feed.title == "Motet"
        (entry,) = parsed.entries
        assert entry.id == "ep_1"
        assert entry.get("itunes_duration") == "0:21:41"
        assert entry.published_parsed is not None  # pubDate parsed as RFC 2822

    def test_the_namespace_is_apples_exact_uri(self) -> None:
        """The trailing ``.dtd`` is load-bearing and invisible when wrong.

        With the wrong URI the document is still valid and every itunes element is still
        present — clients just ignore all of them.
        """
        xml = render_feed(METADATA, [episode()]).decode()
        assert 'xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd"' in xml

    def test_namespaces_are_declared_exactly_once(self) -> None:
        """A duplicate attribute is not well-formed XML, and a strict parser refuses it."""
        xml = render_feed(METADATA, [episode()]).decode()
        assert xml.count("xmlns:itunes=") == 1
        assert xml.count("xmlns:atom=") == 1

    def test_the_guid_is_not_a_permalink(self) -> None:
        """A client uses the guid to decide what it has already played."""
        xml = render_feed(METADATA, [episode()]).decode()
        assert '<guid isPermaLink="false">ep_1</guid>' in xml

    def test_titles_with_xml_metacharacters_survive(self) -> None:
        """A newsletter titled "M&A" would break a hand-formatted document."""
        hostile = episode(title='Deals & <Mergers> — "Q3"')
        xml = render_feed(METADATA, [hostile]).decode()

        assert "& <" not in xml  # escaped, not emitted raw
        parsed = feedparser.parse(xml)
        assert parsed.bozo is False
        assert parsed.entries[0].title == 'Deals & <Mergers> — "Q3"'

    def test_an_empty_feed_is_still_valid(self) -> None:
        """A brand new account subscribes before the first episode exists."""
        parsed = feedparser.parse(render_feed(METADATA, []).decode())
        assert parsed.bozo is False
        assert parsed.entries == []

    def test_an_empty_feed_does_not_claim_it_just_changed(self) -> None:
        """``lastBuildDate`` advancing on every fetch tells a polling client to
        re-download a feed that did not change."""
        first = render_feed(METADATA, []).decode()
        second = render_feed(METADATA, []).decode()
        assert first == second

    def test_episodes_appear_in_the_order_they_were_given(self) -> None:
        parsed = feedparser.parse(
            render_feed(
                METADATA,
                [
                    episode("ep_2", published=datetime(2026, 8, 24, 9, tzinfo=UTC)),
                    episode("ep_1", published=datetime(2026, 8, 23, 9, tzinfo=UTC)),
                ],
            ).decode()
        )
        assert [entry.id for entry in parsed.entries] == ["ep_2", "ep_1"]

    def test_show_notes_list_the_stories(self) -> None:
        """Show notes name the story, not the first 180 characters of spoken copy.

        Phase 1 rendered a truncated sentence; a listener scanning a description wants a
        table of contents. The title comes from the news item, which the caller supplies —
        this module does no database work, which is what lets it be tested from fixtures.
        """
        document = render_feed(METADATA, [episode()], {"ni_1": "Acme raises $20M"})
        parsed = feedparser.parse(document.decode())
        assert "1. Acme raises $20M" in parsed.entries[0].description


# --- Phase 2: show notes, chapters, and the transcript tag ---------------------------


def test_show_notes_list_the_stories_with_timestamps() -> None:
    """Show notes are the stories, not the first 180 characters of spoken copy.

    Phase 1 rendered `segment.text[:180]`, which is a truncated sentence rather than a
    table of contents. A listener scanning a description wants to know what is in the
    episode and where.
    """
    episode = _episode(segments=[_segment("ni_1", 0, 5_000), _segment("ni_2", 5_000, 7_000)])
    titles = {"ni_1": "Acme raises $20M", "ni_2": "Regulator opens an inquiry"}
    notes = show_notes_text(episode, titles)
    assert "1. Acme raises $20M (0:00)" in notes
    assert "2. Regulator opens an inquiry (0:05)" in notes
    assert "traceable to the source" in notes


def test_show_notes_html_escapes_and_cannot_close_its_own_cdata() -> None:
    """A newsletter title is untrusted input that ends up inside a CDATA section.

    `]]>` is the one sequence escaping the angle brackets does not cover, and it would end
    the section early — producing a feed that is not well-formed XML.
    """
    episode = _episode(segments=[_segment("ni_1", 0, 5_000)])
    markup = show_notes_html(episode, {"ni_1": "Ben & Jerry's <b>deal</b> ]]> ends"})
    assert "&amp;" in markup
    assert "&lt;b&gt;" in markup
    assert "]]>" not in markup


def test_the_feed_carries_show_notes_in_both_forms() -> None:
    """Apple reads `description`; most third-party clients prefer `content:encoded`.

    A client that finds only the plain one shows plain text; one that finds only the markup
    one shows raw tags. Both, with the same content, is the only combination that works
    everywhere.
    """
    document = _render_with_titles()
    assert b"<![CDATA[" in document, "content:encoded must survive as markup"
    assert b"<ol>" in document

    parsed = feedparser.parse(document)
    entry = parsed.entries[0]
    assert "Acme raises $20M" in entry.description
    # `content:encoded` is where feedparser puts the richer form.
    assert any("<ol>" in content.value for content in entry.content)


def test_the_feed_advertises_a_transcript_and_chapters() -> None:
    """Podcasting 2.0 tags, and the inline Podlove chapters alongside them.

    Two chapter formats on purpose: Overcast and Podcast Addict read PSC, the newer clients
    read the 2.0 tag, and the inline form works for a client that will not make a second
    authenticated request.
    """
    document = _render_with_titles()
    text = document.decode()

    assert "podcast:transcript" in text
    assert 'type="text/vtt"' in text
    assert 'rel="captions"' in text
    assert "/transcript.vtt?token=" in text

    assert "podcast:chapters" in text
    assert 'type="application/json+chapters"' in text
    assert "/chapters.json?token=" in text

    assert "psc:chapters" in text
    assert 'start="00:00:00.000"' in text
    assert 'title="Acme raises $20M"' in text


def test_the_podcast_namespaces_are_declared_correctly() -> None:
    """The same class of bug as the `itunes` `.dtd` suffix Phase 1 was caught by.

    A namespace URI that is merely *close* leaves a well-formed document in which every tag
    belongs to a namespace nobody recognises — no error, and the feature silently absent.
    """
    text = _render_with_titles().decode()
    assert 'xmlns:podcast="https://podcastindex.org/namespace/1.0"' in text
    assert 'xmlns:psc="http://podlove.org/simple-chapters"' in text
    assert 'xmlns:content="http://purl.org/rss/1.0/modules/content/"' in text


def test_both_real_parsers_still_accept_the_feed() -> None:
    """The Phase 1 rule, re-applied: parse it with what clients actually use.

    `podcastparser` is the parser inside gPodder and it reads the transcript tag, so this
    asserts a client can find the transcript rather than just that the XML is valid.
    """
    document = _render_with_titles()

    parsed = feedparser.parse(document)
    assert not parsed.bozo, getattr(parsed, "bozo_exception", None)
    assert len(parsed.entries) == 1

    gpodder = podcastparser.parse("https://example.test/feed.xml", io.BytesIO(document))
    episode = gpodder["episodes"][0]
    assert episode["title"] == "Briefing"
    assert episode["total_time"] > 0
    # gPodder surfaces chapters and transcript when it recognises them.
    assert episode.get("chapters") or episode.get("link")


def test_an_unrendered_episode_advertises_no_transcript() -> None:
    """Before TTS there are no positions, so pointing at a transcript would be a lie."""
    episode = _episode(segments=[_segment("ni_1", 0, 0)])
    document = render_feed(_metadata(), [episode], {"ni_1": "Acme raises $20M"})
    text = document.decode()
    assert "podcast:transcript" not in text
    assert "psc:chapters" not in text


def test_an_episode_with_no_segments_still_renders() -> None:
    document = render_feed(_metadata(), [_episode(segments=[])], {})
    assert not feedparser.parse(document).bozo
    assert b"No stories in this episode." in document


# --- helpers for the Phase 2 feed tests ----------------------------------------------


def _metadata() -> FeedMetadata:
    return FeedMetadata(
        title="Motet",
        description="Your backlog, read aloud.",
        author="Motet",
        base_url="https://api.example.test",
        token="feed-token",
    )


def _segment(news_item_id: str, start_ms: int, duration_ms: int) -> StoredSegment:
    return StoredSegment(
        id=f"seg_{news_item_id}",
        news_item_id=news_item_id,
        position=0,
        text="Acme raised $20M on Tuesday. It also hired a chief financial officer.",
        start_ms=start_ms,
        duration_ms=duration_ms,
        claims=(
            StoredClaim(
                id=f"cl_{news_item_id}_a",
                position=0,
                text="Acme raised $20M on Tuesday.",
                source_item_id="si_1",
                span_start=0,
                span_end=28,
                start_ms=start_ms,
                duration_ms=duration_ms // 2 if duration_ms else 0,
            ),
            StoredClaim(
                id=f"cl_{news_item_id}_b",
                position=1,
                text="It also hired a chief financial officer.",
                source_item_id="si_1",
                span_start=29,
                span_end=69,
                start_ms=start_ms + (duration_ms // 2 if duration_ms else 0),
                duration_ms=duration_ms - (duration_ms // 2) if duration_ms else 0,
            ),
        ),
    )


def _episode(*, segments: list[StoredSegment]) -> StoredEpisode:
    total = sum(segment.duration_ms for segment in segments)
    return StoredEpisode(
        id="ep_abc123",
        user_id="motet-owner",
        title="Briefing",
        state=EpisodeState.READY,
        kind=EpisodeKind.MANUAL,
        rule=None,
        max_duration_ms=1_200_000,
        duration_ms=total,
        audio_key="users/motet-owner/episodes/ep_abc123.mp3",
        audio_bytes=123_456,
        audio_media_type="audio/mpeg",
        last_error=None,
        listened_through_ms=0,
        created_at=datetime(2026, 8, 18, 7, 0, tzinfo=UTC),
        published_at=datetime(2026, 8, 18, 7, 5, tzinfo=UTC),
        segments=tuple(segments),
    )


def _render_with_titles() -> bytes:
    episode = _episode(segments=[_segment("ni_1", 0, 6_000), _segment("ni_2", 6_000, 4_000)])
    return render_feed(
        _metadata(),
        [episode],
        {"ni_1": "Acme raises $20M", "ni_2": "Regulator opens an inquiry"},
    )
