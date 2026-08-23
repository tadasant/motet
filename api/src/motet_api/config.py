"""Process configuration, read from the environment.

The API never learns *where* it is deployed. Project ids, bucket names, hostnames, and
connection strings arrive as environment variables set by infrastructure that lives in the
private repo — none of them belong in this tree.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    database_url: str | None
    inference_mode: str

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            database_url=os.environ.get("DATABASE_URL"),
            inference_mode=os.environ.get("MOTET_INFERENCE_MODE", "fake"),
        )
