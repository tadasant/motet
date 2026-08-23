"""Render the app's OpenAPI document to ``openapi.yaml``.

The FastAPI app is the source of truth; the YAML is a committed artifact generated from
it, and ``web/src/api/schema.gen.ts`` is generated from the YAML in turn. ``bin/ci``
regenerates both and fails on any diff, so the three can never drift apart.

Output has to be **byte-stable** for that check to mean anything — hence the fixed dump
options and the absence of any timestamp or version stamp in the file.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

import yaml

from .main import app

REPO_ROOT = Path(__file__).resolve().parents[3]
OPENAPI_PATH = REPO_ROOT / "openapi.yaml"

BANNER = (
    "# Generated from the FastAPI app — do not edit by hand.\n"
    "# Regenerate with `bin/generate-openapi`; `bin/ci` fails if this file is stale.\n"
)


def document() -> dict[str, Any]:
    return app.openapi()


def render() -> str:
    body: str = yaml.safe_dump(document(), sort_keys=True, default_flow_style=False, width=100)
    return BANNER + body


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write openapi.yaml from the FastAPI app.")
    parser.add_argument("--output", type=Path, default=OPENAPI_PATH)
    args = parser.parse_args(argv)
    args.output.write_text(render())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
