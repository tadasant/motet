"""The private RSS feed — Phase 1's listening surface.

RSS rather than a player in the SPA, deliberately. The thesis is newsletters on a dog
walk, and a browser cannot do background audio or offline. A feed gets background
playback, offline download, the lockscreen, CarPlay, and speed control for free, with zero
iOS code.

That only pays off if real podcast clients accept the document, so this module is written
against what clients actually require rather than against the RSS specification alone:

* **The enclosure URL must be absolute**, and it must be fetchable by a client that sends
  no cookies and no headers of ours. Hence the token in the query string.
* **``<guid isPermaLink="false">``** — an episode id, stable forever. A client uses it to
  decide what it has already played; a guid that changed would replay the backlog.
* **``pubDate`` in RFC 2822**, which is not the format anything else in this system uses.
* **``<itunes:duration>``**, because that is where the lockscreen gets the length before
  the file is downloaded.
* **``<itunes:explicit>``, ``<itunes:category>``, and an ``<atom:link rel="self">``** on
  the channel, because a client that cannot find them either nags or refuses. There is
  deliberately no ``<itunes:image>``: artwork is brand, and brand is Phase 3 — a feed with
  a placeholder image looks broken in a way a feed with none does not.

Built with ``ElementTree`` rather than string formatting: an unescaped ``&`` in a
newsletter title produces a document that a client rejects with no useful error, and
hand-rolled escaping is exactly the kind of thing that works until the first ampersand.
"""

from __future__ import annotations

import secrets
import xml.etree.ElementTree as ET
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import format_datetime
from urllib.parse import quote, urlencode

from motet_db import StoredEpisode

from .shownotes import chapters, show_notes_html, show_notes_text

#: Apple's namespace URI, and the trailing ``.dtd`` is load-bearing. A feed that declares
#: ``.../podcast-1.0/`` instead is still well-formed XML and still parses — the itunes
#: elements simply belong to a namespace nobody recognises, so Apple Podcasts and Overcast
#: silently ignore duration, category, and explicit. Nothing errors; the episode just has
#: no length on the lockscreen. Caught by parsing our own output with gPodder's parser.
ITUNES_NS = "http://www.itunes.com/dtds/podcast-1.0.dtd"
ATOM_NS = "http://www.w3.org/2005/Atom"

#: Podcasting 2.0. Where `<podcast:transcript>` and `<podcast:chapters>` live, and the
#: namespace Fountain, Podverse, and Podcast Addict look for them in.
PODCAST_NS = "https://podcastindex.org/namespace/1.0"

#: Podlove Simple Chapters — inline chapter markers, read by Overcast and Podcast Addict.
#: Emitted alongside the 2.0 tag rather than instead of it: the two are read by different
#: clients, and the inline form also works for a client that will not make a second
#: authenticated request just to find out an episode has chapters.
PSC_NS = "http://podlove.org/simple-chapters"

#: RSS 1.0's content module, for show notes as markup. Apple reads `<description>`; most
#: third-party clients prefer `<content:encoded>` where it exists.
CONTENT_NS = "http://purl.org/rss/1.0/modules/content/"

#: Registering the prefixes globally is what makes ``ElementTree`` emit `itunes:duration`
#: rather than `ns0:duration`. Clients parse both, but a feed a human cannot read in a
#: browser is a feed nobody can debug.
ET.register_namespace("itunes", ITUNES_NS)
ET.register_namespace("atom", ATOM_NS)
ET.register_namespace("podcast", PODCAST_NS)
ET.register_namespace("psc", PSC_NS)
ET.register_namespace("content", CONTENT_NS)


#: Placeholder for markup that must survive as markup.
#:
#: ``ElementTree`` has no CDATA support and escapes every character it writes, which is
#: exactly wrong for ``content:encoded`` — the point of that element is that its HTML
#: reaches the client as HTML. So the element gets an opaque token as its text, and the
#: token is swapped for a real CDATA section after serialization.
#:
#: The token contains no ``<``, ``>`` or ``&``, so the writer passes it through unchanged
#: and the substitution can be an exact match. It carries a random suffix per document, so
#: a newsletter title that happened to contain the literal prefix cannot be mistaken for
#: one.
_CDATA_TOKEN_PREFIX = "motet-cdata-placeholder"


@dataclass(frozen=True)
class FeedMetadata:
    """Everything about the feed that is not an episode."""

    title: str
    description: str
    author: str
    base_url: str
    token: str


def feed_url(base_url: str, token: str) -> str:
    return f"{base_url.rstrip('/')}/feed.xml?{urlencode({'token': token})}"


def episode_asset_url(base_url: str, episode_id: str, asset: str, token: str) -> str:
    """Where a client fetches one of an episode's side documents.

    Same shape and same token as the audio URL: transcripts and chapter files are fetched
    by the podcast client, not by the app, so they need a credential a client will actually
    send — which for a podcast client means one in the query string.
    """
    query = urlencode({"token": token})
    return f"{base_url.rstrip('/')}/v1/episodes/{quote(episode_id)}/{asset}?{query}"


def episode_audio_url(base_url: str, episode_id: str, token: str) -> str:
    """Where a client fetches the audio.

    Always our own URL, never the storage backend's. The route behind it redirects to a
    signed URL or serves the bytes, depending on the backend — so an enclosure URL stays
    valid when storage changes, and a signed URL's expiry never leaks into a feed a client
    may have cached for hours.
    """
    query = urlencode({"token": token})
    return f"{base_url.rstrip('/')}/v1/episodes/{quote(episode_id)}/audio?{query}"


def itunes_duration(duration_ms: int) -> str:
    """``H:MM:SS``, which is the form every client parses without complaint."""
    total_seconds = max(0, duration_ms) // 1000
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}"


def render_feed(
    metadata: FeedMetadata,
    episodes: Sequence[StoredEpisode],
    titles: Mapping[str, str] | None = None,
) -> bytes:
    """Build the whole document. Returns UTF-8 bytes, declaration included.

    ``titles`` maps a news item id to its title, for show notes and chapter markers. Passed
    in rather than looked up here because this module does no database work — which is what
    lets the feed be rendered from fixtures in a test.
    """
    story_titles = dict(titles or {})
    # Token -> markup, filled by `_cdata` and substituted after serialization.
    pending: dict[str, str] = {}
    nonce = secrets.token_hex(8)
    # The `xmlns:` declarations are NOT set as attributes here. `ElementTree` emits one
    # for every namespace it actually uses, and setting them by hand as well produces a
    # root element carrying `xmlns:itunes` twice — a duplicate attribute, which is not
    # well-formed XML and which a strict feed parser rejects outright.
    rss = ET.Element("rss", {"version": "2.0"})
    channel = ET.SubElement(rss, "channel")

    _text(channel, "title", metadata.title)
    _text(channel, "description", metadata.description)
    _text(channel, "link", metadata.base_url)
    _text(channel, "language", "en-us")
    _text(channel, "generator", "Motet")
    _text(channel, "lastBuildDate", format_datetime(_latest(episodes)))
    _text(channel, f"{{{ITUNES_NS}}}author", metadata.author)
    _text(channel, f"{{{ITUNES_NS}}}summary", metadata.description)
    # A private feed is by definition not for a general audience, and a client that cannot
    # tell will nag about it. "no" is a statement about content, not about privacy.
    _text(channel, f"{{{ITUNES_NS}}}explicit", "no")
    _text(channel, f"{{{ITUNES_NS}}}type", "episodic")
    ET.SubElement(channel, f"{{{ITUNES_NS}}}category", {"text": "News"})
    ET.SubElement(
        channel,
        f"{{{ATOM_NS}}}link",
        {
            "href": feed_url(metadata.base_url, metadata.token),
            "rel": "self",
            "type": "application/rss+xml",
        },
    )
    owner = ET.SubElement(channel, f"{{{ITUNES_NS}}}owner")
    _text(owner, f"{{{ITUNES_NS}}}name", metadata.author)

    for episode in episodes:
        _episode_item(channel, metadata, episode, story_titles, pending, nonce)

    document: bytes = ET.tostring(rss, encoding="utf-8", xml_declaration=True)
    # Swap each placeholder for a real CDATA section. Done on the serialized bytes because
    # `ElementTree` offers no other way; the tokens survive serialization untouched because
    # they contain no character the writer would escape.
    for token, markup in pending.items():
        document = document.replace(token.encode(), b"<![CDATA[" + markup.encode() + b"]]>")
    return document


def _episode_item(
    channel: ET.Element,
    metadata: FeedMetadata,
    episode: StoredEpisode,
    titles: dict[str, str],
    pending: dict[str, str],
    nonce: str,
) -> None:
    item = ET.SubElement(channel, "item")
    _text(item, "title", episode.title)

    notes = show_notes_text(episode, titles)
    _text(item, "description", notes)
    # Apple reads `itunes:summary` where it exists and `description` otherwise; several
    # clients do the reverse. Both, with the same text, is the only combination that shows
    # the same thing everywhere.
    _text(item, f"{{{ITUNES_NS}}}summary", notes)
    _cdata(
        item,
        f"{{{CONTENT_NS}}}encoded",
        show_notes_html(episode, titles),
        pending,
        nonce,
    )

    url = episode_audio_url(metadata.base_url, episode.id, metadata.token)
    _text(item, "link", url)
    guid = _text(item, "guid", episode.id)
    # Not a URL, and saying so matters: a client that treats a guid as a permalink will
    # try to open it, and — worse — some treat a changed permalink as a new episode.
    guid.set("isPermaLink", "false")
    _text(item, "pubDate", format_datetime(episode.published_at or episode.created_at))
    ET.SubElement(
        item,
        "enclosure",
        {
            "url": url,
            "length": str(episode.audio_bytes or 0),
            "type": episode.audio_media_type or "audio/mpeg",
        },
    )
    _text(item, f"{{{ITUNES_NS}}}duration", itunes_duration(episode.duration_ms))
    _text(item, f"{{{ITUNES_NS}}}explicit", "no")
    _text(item, f"{{{ITUNES_NS}}}episodeType", "full")

    _transcript_and_chapters(item, metadata, episode, titles)


def _transcript_and_chapters(
    item: ET.Element,
    metadata: FeedMetadata,
    episode: StoredEpisode,
    titles: dict[str, str],
) -> None:
    """Point at the subtitles and the chapters, and inline the chapters as well.

    Only for an episode that has actually been rendered: before TTS runs, every claim's
    timing is zero, so a transcript would be a pile of cues at 00:00 and the chapters would
    all point at the start. An absent transcript reads as "not available"; a wrong one
    reads as broken.
    """
    markers = chapters(episode, titles)
    if not markers:
        return

    ET.SubElement(
        item,
        f"{{{PODCAST_NS}}}transcript",
        {
            "url": episode_asset_url(
                metadata.base_url, episode.id, "transcript.vtt", metadata.token
            ),
            "type": "text/vtt",
            # `captions` rather than the default: these cues are timed to the audio, which
            # is what the attribute is for. A client that wants a readable transcript
            # renders captions fine; one that wants captions cannot use an untimed document.
            "rel": "captions",
            "language": "en",
        },
    )
    ET.SubElement(
        item,
        f"{{{PODCAST_NS}}}chapters",
        {
            "url": episode_asset_url(
                metadata.base_url, episode.id, "chapters.json", metadata.token
            ),
            "type": "application/json+chapters",
        },
    )

    # Inline Podlove chapters as well. `start` is `HH:MM:SS.mmm`, and the element is
    # self-closing with the title in an attribute — not as text, which is the mistake that
    # makes a chapter list render as a column of blanks.
    inline = ET.SubElement(item, f"{{{PSC_NS}}}chapters", {"version": "1.2"})
    for marker in markers:
        ET.SubElement(
            inline,
            f"{{{PSC_NS}}}chapter",
            {"start": _psc_timestamp(marker.start_ms), "title": marker.title},
        )


def _psc_timestamp(milliseconds: int) -> str:
    total = max(0, milliseconds) // 1000
    millis = max(0, milliseconds) % 1000
    hours, remainder = divmod(total, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{millis:03d}"


def _cdata(
    parent: ET.Element, tag: str, markup: str, pending: dict[str, str], nonce: str
) -> ET.Element:
    """Write markup that must survive as markup.

    The element gets an opaque token; :func:`render_feed` swaps it for a real CDATA section
    after serialization. :func:`~motet_api.shownotes.show_notes_html` has already
    neutralized any ``]]>`` in the content, which is the one sequence that could close the
    section early and produce a document that is not well-formed.
    """
    token = f"{_CDATA_TOKEN_PREFIX}-{nonce}-{len(pending)}"
    pending[token] = markup
    element = ET.SubElement(parent, tag)
    element.text = token
    return element


def _text(parent: ET.Element, tag: str, value: str) -> ET.Element:
    element = ET.SubElement(parent, tag)
    element.text = value
    return element


def _latest(episodes: Sequence[StoredEpisode]) -> datetime:
    """When the feed last changed.

    Falls back to the epoch rather than to "now" for an empty feed: a `lastBuildDate` that
    advances on every fetch tells a client the feed changed when it did not, which is how
    a polling client ends up re-downloading nothing forever.
    """
    stamps = [e.published_at or e.created_at for e in episodes]
    return max(stamps) if stamps else datetime.fromtimestamp(0, UTC)
