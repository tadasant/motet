"""Fixtures for the API tests: reset the process-global state a request can touch.

The root ``conftest.py`` owns the database. What is left here is the state that lives in
*this process* rather than in Postgres, and the failed-auth throttle is the one that bites:
it is one object for the whole process by design (``motet_api.throttle``), so a module that
asserts a 401 after another module has already spent the budget gets a 429 instead. That
is the throttle working — but between tests it is a leak, exactly as a cached object store
between tests is.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest


@pytest.fixture(autouse=True)
def _reset_failed_auth_throttle() -> Iterator[None]:
    from motet_api.throttle import failed_auth

    failed_auth.reset()
    yield
    failed_auth.reset()
