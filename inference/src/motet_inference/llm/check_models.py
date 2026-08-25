"""Verify the committed model catalogue against OpenRouter's live model list.

Slugs move. A wrong one fails at runtime rather than at deploy, which is the worst place
to find out, so :data:`~motet_inference.llm.config.KNOWN_MODELS` exists to turn that into
a startup error — and this module exists to keep the catalogue honest against reality.

    bin/check-openrouter-models          # check every catalogued slug
    bin/check-openrouter-models sonnet   # also list live slugs matching a substring

**Deliberately not part of ``bin/ci``.** CI is offline, deterministic, and free by design
(invariant 7); a check that reaches the internet would make every run depend on a vendor
being up. Run it by hand when adding a model or when a slug is suspected stale.

It lives in the package rather than as a loose script in ``bin/`` so that ruff and mypy
see it like everything else — a file that lints itself out of the checks by not ending in
``.py`` is a file that rots. It needs no credential: the model list is public.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from typing import Any

from .config import KNOWN_MODELS

MODELS_URL = "https://openrouter.ai/api/v1/models"
TIMEOUT_SECONDS = 30


def fetch_live_slugs() -> dict[str, Any]:
    request = urllib.request.Request(MODELS_URL, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:  # noqa: S310
        payload = json.load(response)
    return {entry["id"]: entry for entry in payload["data"]}


def main(argv: list[str]) -> int:
    try:
        live = fetch_live_slugs()
    except (urllib.error.URLError, TimeoutError, ValueError, KeyError) as exc:
        print(f"could not reach OpenRouter's model list: {exc}", file=sys.stderr)
        return 2

    print(f"{len(live)} models live on OpenRouter\n")

    failures = 0
    for slug, spec in sorted(KNOWN_MODELS.items()):
        entry = live.get(slug)
        if entry is None:
            print(f"MISSING  {slug}  — not in OpenRouter's live list")
            failures += 1
            continue

        notes: list[str] = []
        context = entry.get("context_length")
        if isinstance(context, int) and context != spec.context_tokens:
            notes.append(f"context {spec.context_tokens} -> {context}")

        reasoning = entry.get("reasoning") or {}
        # Some providers list "none" among the efforts. We model "do not think" as no
        # reasoning config at all (`MOTET_LLM_EFFORT_<STAGE>=off`), not as an effort
        # level, so it is not a catalogue entry and its absence is not drift.
        live_efforts = tuple(e for e in (reasoning.get("supported_efforts") or ()) if e != "none")
        if set(live_efforts) != set(spec.efforts):
            notes.append(f"efforts {sorted(spec.efforts)} -> {sorted(live_efforts)}")

        pricing = entry.get("pricing") or {}
        live_ttl_1h = "input_cache_write_1h" in pricing
        if live_ttl_1h != spec.supports_cache_ttl_1h:
            notes.append(f"1h cache {spec.supports_cache_ttl_1h} -> {live_ttl_1h}")

        if notes:
            print(f"DRIFT    {slug}  — {'; '.join(notes)}")
            failures += 1
        else:
            print(f"ok       {slug}  ({entry.get('canonical_slug', slug)})")

    # `adaptive_thinking` is deliberately absent from the loop above: OpenRouter's model
    # list says which efforts a slug accepts, never what an effort *does* to it, and the
    # difference between "effort sets a thinking budget" and "effort sets
    # output_config.effort while Claude decides whether to think" is exactly what
    # motet#31 turned on. Nothing here can check it, so it is said out loud instead of
    # quietly passing.
    print(
        "\nNot checked here: adaptive_thinking. OpenRouter's model list does not carry it. "
        "Read the slug's migration guide (Claude 4.6 and later think adaptively) and set "
        "it by hand when adding a model."
    )

    for needle in argv[1:]:
        print(f"\nlive slugs matching {needle!r}:")
        for slug in sorted(s for s in live if needle in s):
            print(f"  {slug}")

    if failures:
        print(
            f"\n{failures} catalogue entr{'y is' if failures == 1 else 'ies are'} stale. "
            "Update KNOWN_MODELS in inference/src/motet_inference/llm/config.py.",
            file=sys.stderr,
        )
        return 1

    print("\nCatalogue matches OpenRouter's live list.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
