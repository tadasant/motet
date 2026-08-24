"""The contract gate.

``openapi.yaml`` is generated from the app and committed. If this test fails, run
``bin/generate-openapi`` (and then ``bin/generate-client``) and commit the result — do not
edit the YAML.
"""

from __future__ import annotations

from motet_api.openapi import OPENAPI_PATH, document, render


def test_committed_openapi_matches_the_app() -> None:
    assert OPENAPI_PATH.exists(), "openapi.yaml is missing — run bin/generate-openapi"
    assert OPENAPI_PATH.read_text() == render(), (
        "openapi.yaml is stale — run bin/generate-openapi and commit the result"
    )


def test_render_is_deterministic() -> None:
    """Byte-stability is what makes the drift check trustworthy."""
    assert render() == render()


def test_every_route_is_in_the_document() -> None:
    paths = document()["paths"]
    for expected in (
        "/internal/health",
        "/v1/sources/paste",
        "/v1/news-items",
        "/v1/episodes",
        "/v1/episodes/{episode_id}",
        "/feed.xml",
    ):
        assert expected in paths
