"""Whether this process may talk to a vendor. One variable, parsed in one place.

``MOTET_INFERENCE_MODE`` is ``fake`` everywhere except staging and production. Two things
read it — the stage registry, which picks fake or real adapters, and the LLM provider
seam, which picks a fake or real model client — and it lives here, depended on by both,
rather than in either of them.

That is not tidiness. When the two read it separately they can disagree, and the
disagreement is silent: an exact ``== "real"`` on one side and a normalizing parse on the
other means ``MOTET_INFERENCE_MODE=Real`` selects *real stage adapters wired to a fake
model*. The revision boots clean, skips the credential check, and feeds fabricated text
into grounding validation and then into audio. One parser, one answer.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Final, Literal

MODE_ENV_VAR: Final = "MOTET_INFERENCE_MODE"
Mode = Literal["fake", "real"]


def current_mode(env: Mapping[str, str] | None = None) -> Mode:
    """Read the mode, normalized, rejecting anything unrecognized.

    Defaults to ``fake``: a missing variable must fail toward the free, offline side.
    Case and surrounding whitespace are forgiven, because a YAML block scalar in a
    service definition supplies both; an unrecognized *value* is not, because guessing
    what someone meant is how the two-readings failure gets reinvented.
    """
    environ = os.environ if env is None else env
    raw = environ.get(MODE_ENV_VAR, "fake").strip().lower()
    if raw not in ("fake", "real"):
        raise ValueError(f"{MODE_ENV_VAR} must be 'fake' or 'real', got {raw!r}")
    return raw  # type: ignore[return-value]
