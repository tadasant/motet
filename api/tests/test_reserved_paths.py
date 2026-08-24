"""The guard that stops motet#16 coming back.

`/healthz` was declared by this app, generated into `openapi.yaml`, asserted on by the
container smoke test, and **unreachable in production**: Google's Cloud Run frontend
answers that path with its own HTML 404 before the request reaches the container, on the
`run.app` URL and on a custom domain, over HTTP/1.1 and HTTP/2 alike.

Nothing in this repo could see it. `bin/build-images` runs the image under `docker run`,
where there is no Google frontend in the way, so the assertion passed while the deployed
reality 404'd — which is the only reason it shipped.

**This test cannot see it either**, and that is worth being blunt about: no test that runs
in CI can prove what Google's frontend does, because the frontend is not there. What it
*can* do is make the mistake un-repeatable — if anyone re-declares a route under a path the
platform reserves, this fails at the point the route is written rather than after a deploy.
The list is knowledge, not detection.
"""

from __future__ import annotations

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from motet_api import app
from motet_api.main import HEALTH_PATH, PLATFORM_RESERVED_PATHS
from motet_api.openapi import document


def declared_paths() -> list[str]:
    return [route.path for route in app.routes if isinstance(route, APIRoute)]


@pytest.mark.parametrize("reserved", PLATFORM_RESERVED_PATHS)
def test_no_route_lives_under_a_platform_reserved_path(reserved: str) -> None:
    offenders = [
        path for path in declared_paths() if path == reserved or path.startswith(reserved + "/")
    ]
    assert not offenders, (
        f"{offenders} sits under {reserved!r}, which the Cloud Run frontend answers "
        "itself — the route would 404 in every deployed environment while passing every "
        "check in this repo. See motet#16."
    )


def test_health_is_not_served_on_a_reserved_path() -> None:
    assert HEALTH_PATH not in PLATFORM_RESERVED_PATHS
    assert HEALTH_PATH == "/internal/health"


def test_the_reserved_path_is_not_answered_at_all() -> None:
    """Not even as a courtesy alias.

    An alias would answer on a laptop and 404 in production, which is precisely the
    difference that hid the bug: a path that works everywhere except where it is checked
    is worse than no path, because it teaches the reader it works.
    """
    with TestClient(app) as client:
        response = client.get("/healthz")
        assert response.status_code == 404
        # FastAPI's own 404, not Google's HTML one — locally there is no frontend to
        # produce the latter. Asserting on the body keeps this honest about which 404
        # it is looking at.
        assert response.json() == {"detail": "Not Found"}


def test_the_generated_document_advertises_the_health_path() -> None:
    """The document is the contract the SPA and the iOS client are generated from.

    A route renamed in the app but not regenerated here would ship a client calling the
    old path — the same class of silent breakage, one layer out.
    """
    paths = document()["paths"]
    assert HEALTH_PATH in paths
    for reserved in PLATFORM_RESERVED_PATHS:
        assert reserved not in paths
