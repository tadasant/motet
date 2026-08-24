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

import xml.etree.ElementTree as ET
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import format_datetime
from urllib.parse import quote, urlencode

from motet_db import StoredEpisode

#: Apple's namespace URI, and the trailing ``.dtd`` is load-bearing. A feed that declares
#: ``.../podcast-1.0/`` instead is still well-formed XML and still parses — the itunes
#: elements simply belong to a namespace nobody recognises, so Apple Podcasts and Overcast
#: silently ignore duration, category, and explicit. Nothing errors; the episode just has
#: no length on the lockscreen. Caught by parsing our own output with gPodder's parser.
ITUNES_NS = "http://www.itunes.com/dtds/podcast-1.0.dtd"
ATOM_NS = "http://www.w3.org/2005/Atom"

#: Registering the prefixes globally is what makes ``ElementTree`` emit `itunes:duration`
#: rather than `ns0:duration`. Clients parse both, but a feed a human cannot read in a
#: browser is a feed nobody can debug.
ET.register_namespace("itunes", ITUNES_NS)
ET.register_namespace("atom", ATOM_NS)


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


def episode_description(episode: StoredEpisode) -> str:
    """Show notes: the stories in this episode, in the order they are spoken.

    Plain text rather than HTML — this is what a listener sees on a lockscreen, where
    markup is either stripped or shown raw depending on the client.
    """
    if not episode.segments:
        return "No stories in this episode."
    return "\n".join(
        f"{index + 1}. {segment.text.strip()[:180]}"
        for index, segment in enumerate(episode.segments)
    )


def render_feed(metadata: FeedMetadata, episodes: Sequence[StoredEpisode]) -> bytes:
    """Build the whole document. Returns UTF-8 bytes, declaration included."""
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
        _episode_item(channel, metadata, episode)

    document: bytes = ET.tostring(rss, encoding="utf-8", xml_declaration=True)
    return document


def _episode_item(channel: ET.Element, metadata: FeedMetadata, episode: StoredEpisode) -> None:
    item = ET.SubElement(channel, "item")
    _text(item, "title", episode.title)
    _text(item, "description", episode_description(episode))
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
