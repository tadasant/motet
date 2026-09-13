"""The Dockerfile's Playwright image and the lockfile's Playwright have to be one version.

A mismatch is not a build failure. The image builds, the service starts, health says
``toolchain_ready: true`` because a ``chromium-*`` directory is there — and the first real
fetch dies with ``Executable doesn't exist at /ms-playwright/chromium-####/...`` because
the driver the lockfile installed wants a build the image does not carry. That is a whole
class of bug whose only symptom is a failed enrichment on a real item, so it is a test.

Read out of both files rather than restated: a constant here would be a third copy.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

IMAGE_RE = re.compile(r"mcr\.microsoft\.com/playwright:v([0-9]+\.[0-9]+\.[0-9]+)-")


def _image_versions() -> set[str]:
    return set(IMAGE_RE.findall((ROOT / "Dockerfile").read_text(encoding="utf-8")))


def _lockfile_version() -> str:
    lock = json.loads((ROOT / "enrich" / "harness" / "package-lock.json").read_text())
    return str(lock["packages"]["node_modules/playwright"]["version"])


def test_every_playwright_image_tag_matches_the_lockfile() -> None:
    versions = _image_versions()
    assert versions, "the Dockerfile no longer names a Playwright base image"
    assert versions == {_lockfile_version()}, (
        f"the Dockerfile pins Playwright {sorted(versions)} and "
        f"enrich/harness/package-lock.json resolves {_lockfile_version()}. The browser "
        "build and the driver must match, or the first fetch fails at run time."
    )


def test_the_toolchain_is_pinned_exactly_rather_than_by_range() -> None:
    """Third-party code that drives a browser over untrusted pages inside a container
    holding the owner's session cookies. A caret here would mean the image's contents
    change without a commit."""
    manifest = json.loads((ROOT / "enrich" / "harness" / "package.json").read_text())
    for name, spec in manifest["dependencies"].items():
        assert re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", spec), (
            f"{name} is not pinned exactly: {spec}"
        )
