"""Ingestion sources: the Gmail seam, behind an interface with a fake.

`Poll -> Extract -> Source Item`, held there until a person asks for the dedup/integrate
stage (motet#91). The same
shape as ``motet_inference``: Protocols in :mod:`~motet_sources.interfaces`, a
deterministic fake in :mod:`~motet_sources.fakes`, a real adapter in
:mod:`~motet_sources.gmail`, and a registry that picks between them from the one mode
variable.

**Gmail is dormant.** The Google OAuth client is a one-time human-owned provisioning step
and has not happened, so ``MOTET_INFERENCE_MODE=fake`` — the default everywhere — serves
fixture newsletters instead. Turning it on is configuration, not a refactor.
"""

from .extract import (
    MAX_TEXT_CHARS,
    MIN_TEXT_CHARS,
    ExtractedMessage,
    ExtractionError,
    extract_newsletter,
    html_to_text,
)
from .fakes import FAKE_LABELS, FakeMailClient, FakeOAuthClient, load_fixture_messages
from .gmail import (
    DEFAULT_FIRST_SYNC_DAYS,
    DEFAULT_QUERY,
    FIRST_SYNC_DAYS_CONFIG_KEY,
    GMAIL_MODIFY_SCOPE,
    GMAIL_READONLY_SCOPE,
    LABEL_SYNC_SCOPES,
    MAX_FIRST_SYNC_DAYS,
    PROVIDER,
    GmailConfigError,
    GmailMailClient,
    GmailOAuthClient,
    clamp_first_sync_days,
    config_first_sync_days,
    first_sync_days,
)
from .interfaces import (
    Label,
    MailClient,
    MessagePage,
    MessageRef,
    OAuthClient,
    RawMessage,
    SourceAuthError,
    SourceError,
    StaleReferenceError,
    TokenGrant,
)
from .labels import LabelSettings, LabelSettingsError
from .registry import build_mail_client, build_oauth_client, new_oauth_state, new_pkce_pair

__all__ = [
    "DEFAULT_FIRST_SYNC_DAYS",
    "DEFAULT_QUERY",
    "FIRST_SYNC_DAYS_CONFIG_KEY",
    "FAKE_LABELS",
    "GMAIL_MODIFY_SCOPE",
    "GMAIL_READONLY_SCOPE",
    "LABEL_SYNC_SCOPES",
    "MAX_FIRST_SYNC_DAYS",
    "MAX_TEXT_CHARS",
    "MIN_TEXT_CHARS",
    "PROVIDER",
    "ExtractedMessage",
    "ExtractionError",
    "FakeMailClient",
    "FakeOAuthClient",
    "GmailConfigError",
    "GmailMailClient",
    "GmailOAuthClient",
    "Label",
    "LabelSettings",
    "LabelSettingsError",
    "MailClient",
    "MessagePage",
    "MessageRef",
    "OAuthClient",
    "RawMessage",
    "SourceAuthError",
    "SourceError",
    "StaleReferenceError",
    "TokenGrant",
    "clamp_first_sync_days",
    "config_first_sync_days",
    "first_sync_days",
    "build_mail_client",
    "build_oauth_client",
    "extract_newsletter",
    "html_to_text",
    "load_fixture_messages",
    "new_oauth_state",
    "new_pkce_pair",
]
