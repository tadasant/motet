"""Label sync's settings, its cached label catalog, and name-to-id resolution — motet#96.

Per Gmail source, two optional label *names*: one to remove from a message and one to add
to it, applied when — and only when — the owner deliberately ingests the item. The owner's
own pair is ``Newsletters → Completed``; someone else's might be ``INBOX →`` (archive), or a
single label to add with nothing to remove. Names rather than ids, because a name is what a
person chooses and an id is what Gmail happens to have assigned it.

**Where each half lives is the ``config`` / ``sync_state`` split the sources table already
draws.** The names are the owner's intent, so they are ``sources.config[CONFIG_KEY]``. The
name-to-id catalog is our bookmark of the mailbox, so it is ``sources.sync_state
[CATALOG_KEY]`` — written by the poll (one ``labels.list`` per poll, a read under the
readonly scope) and by the write-back when a name misses. Keyed by name, so changing a
setting is simply a cache miss rather than something to invalidate.

Pure functions over plain dicts, so the API — which reads the settings and serves the
catalog to the label pickers — and the worker — which resolves and writes — cannot
disagree about the shape, and neither needs a database or a mailbox to be tested.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final

from .interfaces import Label

#: ``sources.config`` key holding the owner's two label names.
CONFIG_KEY: Final = "labels_on_ingest"

#: ``sources.sync_state`` key holding the last label catalog read from the mailbox.
CATALOG_KEY: Final = "label_catalog"

#: ``sources.sync_state`` key: the mailbox address a source's grant was first seen to reach.
MAILBOX_ADDRESS_KEY: Final = "mailbox_address"

#: ``sources.sync_state`` key: *which* refresh grant the mailbox was last checked for — the
#: grant's ``updated_at``, which every consent rewrites. A grant this does not match is
#: checked before the worker polls or writes with it. Bound to the credential rather than a
#: flag a consent sets, so a stale ``sync_state`` write can only ever cause a re-check,
#: never skip one. A re-consent for label sync replaces an existing source's grant, and
#: Google's account chooser returns a different account's grant without complaint — this
#: is what stops a source reading, or writing labels in, a mailbox it was never connected to.
MAILBOX_VERIFIED_FOR_KEY: Final = "mailbox_verified_for"

#: How old the cached label catalog may get before a poll reads it again. The pickers are
#: suggestions and the write-back re-reads on a miss, so a day is plenty — and it keeps a
#: read-only mailbox that never uses label sync to one extra read a day.
CATALOG_MAX_AGE: Final = timedelta(days=1)

#: Gmail's own limit on a label name. A longer one cannot exist, so it is refused at input.
MAX_LABEL_NAME_CHARS: Final = 225

#: The system labels a message may be moved into or out of, and nothing else.
#:
#: ``gmail.modify`` can also add ``TRASH`` and ``SPAM``, and the one thing this feature
#: must never do is make a newsletter disappear somewhere its owner would not look for it.
#: ``INBOX`` is here because removing it *is* archiving — the ``Inbox → Archive`` workflow —
#: and archiving is reversible from All Mail. ``SENT`` and ``DRAFT`` Gmail refuses anyway;
#: the ``CATEGORY_*`` tabs are Gmail's classification, not the owner's bookkeeping.
WRITABLE_SYSTEM_LABELS: Final = frozenset({"INBOX", "UNREAD", "STARRED", "IMPORTANT"})

#: Every system label id Gmail defines. A name matching one of these that is not in
#: :data:`WRITABLE_SYSTEM_LABELS` is refused before it is ever stored — Gmail does not let a
#: user label take a reserved name, so there is no user label this could be shadowing.
GMAIL_SYSTEM_LABELS: Final = frozenset(
    {
        "INBOX",
        "UNREAD",
        "STARRED",
        "IMPORTANT",
        "SENT",
        "DRAFT",
        "SPAM",
        "TRASH",
        "CHAT",
        "CATEGORY_PERSONAL",
        "CATEGORY_SOCIAL",
        "CATEGORY_PROMOTIONS",
        "CATEGORY_UPDATES",
        "CATEGORY_FORUMS",
    }
)


#: The system label ids no write may touch, whatever a cached catalog claims about them.
FORBIDDEN_LABEL_IDS: Final = GMAIL_SYSTEM_LABELS - WRITABLE_SYSTEM_LABELS


class LabelSettingsError(ValueError):
    """A label name that cannot be used — too long, reserved, or both halves the same."""


@dataclass(frozen=True)
class LabelSettings:
    """The owner's two label names for one source. Either may be ``None``; not both."""

    remove: str | None = None
    add: str | None = None

    @classmethod
    def from_config(cls, config: Mapping[str, Any]) -> LabelSettings | None:
        """The settings stored on a source, or ``None`` when label sync is off.

        Lenient on read — a value that is not a string is treated as absent — because the
        write path is where names are validated, and a malformed row must turn the feature
        off rather than crash the poll or the source list.
        """
        raw = config.get(CONFIG_KEY)
        if not isinstance(raw, Mapping):
            return None
        remove = _clean(raw.get("remove"))
        add = _clean(raw.get("add"))
        if remove is None and add is None:
            return None
        return cls(remove=remove, add=add)

    @classmethod
    def parse(cls, *, remove: str | None, add: str | None) -> LabelSettings | None:
        """Validate what a person typed. ``None`` means "turn label sync off"."""
        remove, add = _clean(remove), _clean(add)
        for name in (remove, add):
            if name is None:
                continue
            if len(name) > MAX_LABEL_NAME_CHARS:
                raise LabelSettingsError(
                    f"A Gmail label name is at most {MAX_LABEL_NAME_CHARS} characters."
                )
            upper = name.upper()
            if upper in GMAIL_SYSTEM_LABELS and upper not in WRITABLE_SYSTEM_LABELS:
                raise LabelSettingsError(
                    f"{name!r} is a Gmail system label Motet will not move mail into or out "
                    f"of. Allowed system labels: {', '.join(sorted(WRITABLE_SYSTEM_LABELS))}."
                )
        if remove is not None and add is not None and remove.casefold() == add.casefold():
            raise LabelSettingsError("The label to remove and the label to add are the same.")
        if remove is None and add is None:
            return None
        return cls(remove=remove, add=add)

    def to_config(self) -> dict[str, str | None]:
        return {"remove": self.remove, "add": self.add}

    def names(self) -> tuple[str, ...]:
        return tuple(name for name in (self.remove, self.add) if name is not None)


@dataclass(frozen=True)
class Resolved:
    """Label ids for one write, or the names that did not resolve."""

    add: tuple[str, ...]
    remove: tuple[str, ...]
    missing: tuple[str, ...]
    #: Names that resolved to a system label outside :data:`WRITABLE_SYSTEM_LABELS`.
    refused: tuple[str, ...] = ()


def catalog_to_sync_state(labels: Iterable[Label], *, fetched_at: datetime) -> dict[str, Any]:
    """The JSON a catalog is cached as, under :data:`CATALOG_KEY`."""
    return {
        "fetched_at": fetched_at.isoformat(),
        "labels": [
            {"id": label.id, "name": label.name, "system": label.system} for label in labels
        ],
    }


def catalog_from_sync_state(sync_state: Mapping[str, Any]) -> tuple[Label, ...]:
    """The cached catalog, or an empty one. Lenient for :meth:`LabelSettings.from_config`'s
    reason: a malformed cache is a cache miss, never a crash."""
    raw = sync_state.get(CATALOG_KEY)
    if not isinstance(raw, Mapping):
        return ()
    out = []
    for item in raw.get("labels") or []:
        if isinstance(item, Mapping) and isinstance(item.get("id"), str):
            out.append(
                Label(
                    id=item["id"],
                    name=str(item.get("name") or item["id"]),
                    system=bool(item.get("system")),
                )
            )
    return tuple(out)


def catalog_fetched_at(sync_state: Mapping[str, Any]) -> datetime | None:
    raw = sync_state.get(CATALOG_KEY)
    if not isinstance(raw, Mapping) or not isinstance(raw.get("fetched_at"), str):
        return None
    try:
        return datetime.fromisoformat(raw["fetched_at"])
    except ValueError:
        return None


def pickable(catalog: Sequence[Label]) -> list[str]:
    """The names a label picker offers: every user label, and the writable system ones.

    User labels first, alphabetically, because they are what someone's bookkeeping is made
    of; the four system labels after, because ``INBOX`` is the one an archive workflow needs.
    """
    user = sorted((label.name for label in catalog if not label.system), key=str.casefold)
    system = sorted(
        label.name for label in catalog if label.system and label.id in WRITABLE_SYSTEM_LABELS
    )
    return [*user, *system]


def resolve(settings: LabelSettings, catalog: Sequence[Label]) -> Resolved:
    """Turn the owner's names into this mailbox's ids.

    Exact match first, then case-insensitive — Gmail treats label names case-insensitively
    for uniqueness, and a person typing ``Inbox`` means ``INBOX``. A name that resolves to a
    system label outside :data:`WRITABLE_SYSTEM_LABELS` is *refused* rather than missing,
    which is the second guard behind the one in :meth:`LabelSettings.parse`: the settings
    route refuses the name, and this refuses the id, so a row written some other way still
    cannot move mail into the trash.
    """
    by_exact = {label.name: label for label in catalog}
    by_folded: dict[str, Label] = {}
    for label in catalog:
        by_folded.setdefault(label.name.casefold(), label)

    missing: list[str] = []
    refused: list[str] = []

    def one(name: str | None) -> tuple[str, ...]:
        if name is None:
            return ()
        label = by_exact.get(name) or by_folded.get(name.casefold())
        if label is None:
            missing.append(name)
            return ()
        # By id as well as by the cached flag: a catalog is data a poll wrote, and "TRASH
        # with system=false" must not be the one way past this.
        if label.id in FORBIDDEN_LABEL_IDS or (
            label.system and label.id not in WRITABLE_SYSTEM_LABELS
        ):
            refused.append(name)
            return ()
        return (label.id,)

    remove = one(settings.remove)
    add = one(settings.add)
    return Resolved(add=add, remove=remove, missing=tuple(missing), refused=tuple(refused))


def _clean(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None
