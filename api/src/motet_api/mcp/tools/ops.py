"""Whether the pipeline is moving, and who this connection is."""

from __future__ import annotations

from ...deps import drain_trigger, slack_alerter
from ...schemas import (
    HealthResponse,
    ProcessingStatusResponse,
    SessionResponse,
    VoiceStatusResponse,
)
from ..context import routes, run


def get_processing_status() -> ProcessingStatusResponse:
    """Say whether workers are draining the queues, and how much work is waiting per queue.

    A queued item looks the same whether a worker is busy or none has ever run; this is
    what tells them apart. Use it when an episode or a paste seems stuck.
    """
    return run(
        "get_processing_status",
        lambda c: routes.processing_status(conn=c.conn, user_id=c.user_id),
    )


def get_health() -> HealthResponse:
    """Report this deployment's health: its revision, inference mode, and what is configured.

    Telemetry, sign-in, the credential vault, the drain trigger, voice, and MCP
    authorization each say whether they are actually wired. The same answer as
    `GET /internal/health`.
    """
    return run(
        "get_health",
        lambda c: routes.health(config=c.config, trigger=drain_trigger(), alerter=slack_alerter()),
    )


def get_voice_status() -> VoiceStatusResponse:
    """Say whether Play Live (talking to an episode) is available here, and if not, why."""
    return run(
        "get_voice_status",
        lambda c: routes.voice_status(user_id=c.user_id, config=routes.voice_config()),
    )


def whoami() -> SessionResponse:
    """Say who this connection acts as: a signed-in person (and their email), or the API token."""
    return run("whoami", lambda c: routes.current_session(caller=c.caller, config=c.config))


TOOLS = (get_processing_status, get_health, get_voice_status, whoami)
