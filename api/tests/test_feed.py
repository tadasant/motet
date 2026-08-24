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
from motet_db import EpisodeState, StoredClaim, StoredEpisode, StoredSegment

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
        max_duration_ms=1_800_000,
        duration_ms=duration_ms,
        audio_key=f"users/motet-owner/episodes/{episode_id}.mp3",
        audio_bytes=20_812_345,
        audio_media_type="audio/mpeg",
        last_error=None,
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
        parsed = feedparser.parse(render_feed(METADATA, [episode()]).decode())
        assert "1. Acme raised twenty million dollars." in parsed.entries[0].description
