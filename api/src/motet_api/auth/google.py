"""The real Google identity adapter: exchange a code, verify the ID token it returns.

**The same OAuth client as Gmail ingestion**, deliberately. `GOOGLE_OAUTH_CLIENT_ID` and
`GOOGLE_OAUTH_CLIENT_SECRET` are already provisioned, the three redirect URIs are already
registered on it, and requesting a second client would be a second one-time human-owned
provisioning step (invariant 9) to buy nothing. Sign-in adds `openid email profile`
alongside `gmail.readonly` on that one client.

**The verification here is not belt-and-braces.** The consent screen for this client is
published and unverified, so *anyone* on the internet can reach the end of the flow — which
means the only thing standing between a stranger and this API is the pair of checks below:
the ID token is genuinely Google's and genuinely about a verified address, and that address
is on the allowlist. The second lives in :mod:`motet_api.auth.allowlist`. This module owns
the first, and it is strict on purpose:

* the signature verifies against Google's published JWKS, over RS256 only;
* ``aud`` is exactly our client id — a token minted for some other application is not a
  token about our user;
* ``iss`` is Google;
* ``exp`` has not passed and ``iat`` is not in the future, both with a minute of leeway
  for clock skew;
* ``nonce`` matches the one this API generated for this authorization, so a token
  captured elsewhere cannot be replayed into our callback;
* ``email_verified`` is true, because an unverified address is a string somebody typed.

Raw REST over ``httpx`` and ``PyJWT``, for the same reason ``motet_sources.gmail`` avoids
``google-api-python-client``: two endpoints are needed, and the SDK brings its own auth
stack that wants to hold tokens itself.
"""

from __future__ import annotations

import logging
from typing import Any, Final
from urllib.parse import urlencode

from .interfaces import IdentityConfigError, IdentityError, VerifiedIdentity

logger = logging.getLogger("motet.api.auth")

PROVIDER: Final = "google"

#: What signing in asks for, and nothing more. `openid` and `email` are what produce a
#: verified address; `profile` is only so the UI can say a name instead of an address.
#: None of the three is a sensitive scope, so none of them changes this client's
#: verification status.
LOGIN_SCOPES: Final = ("openid", "email", "profile")

CLIENT_ID_ENV: Final = "GOOGLE_OAUTH_CLIENT_ID"
CLIENT_SECRET_ENV: Final = "GOOGLE_OAUTH_CLIENT_SECRET"
TIMEOUT_ENV: Final = "MOTET_GOOGLE_AUTH_TIMEOUT_SECONDS"

GOOGLE_AUTH_ENDPOINT: Final = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_ENDPOINT: Final = "https://oauth2.googleapis.com/token"
GOOGLE_JWKS_URI: Final = "https://www.googleapis.com/oauth2/v3/certs"

#: Both spellings Google uses in `iss`. Documented as either, and which one arrives is
#: not ours to depend on.
GOOGLE_ISSUERS: Final = ("https://accounts.google.com", "accounts.google.com")

DEFAULT_TIMEOUT_SECONDS: Final = 15.0

#: Clock skew tolerated on `exp` and `iat`. A minute: enough for two machines that are
#: roughly in step, short enough that an expired token is not usefully expired-but-working.
LEEWAY_SECONDS: Final = 60


def oauth_client_config(env: dict[str, str]) -> tuple[str, str]:
    """The client id and secret, or a clear statement of what is missing.

    Raises rather than returning blanks so a misconfigured deployment fails at the sign-in
    attempt naming the variable, instead of at Google's token endpoint with
    ``invalid_client``.
    """
    client_id = env.get(CLIENT_ID_ENV, "").strip()
    client_secret = env.get(CLIENT_SECRET_ENV, "").strip()
    missing = [
        name
        for name, value in ((CLIENT_ID_ENV, client_id), (CLIENT_SECRET_ENV, client_secret))
        if not value
    ]
    if missing:
        raise IdentityConfigError(
            f"{' and '.join(missing)} unset, so signing in with Google is not possible. "
            "The Google OAuth client is a one-time human-owned provisioning step."
        )
    return client_id, client_secret


class GoogleIdentityProvider:
    """Sign-in against Google's OAuth 2.0 / OpenID Connect endpoints."""

    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        authorization_endpoint: str = GOOGLE_AUTH_ENDPOINT,
        token_endpoint: str = GOOGLE_TOKEN_ENDPOINT,
        jwks_uri: str = GOOGLE_JWKS_URI,
        transport: Any | None = None,
        #: ``(id_token) -> key``. Injected by tests so the verifier itself is covered
        #: without a network — invariant 7 applies to Google exactly as it does to a model.
        signing_key: Any | None = None,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._timeout = timeout_seconds
        self._authorization_endpoint = authorization_endpoint
        self._token_endpoint = token_endpoint
        self._jwks_uri = jwks_uri
        self._transport = transport
        self._signing_key = signing_key
        self._jwk_client: Any | None = None

    def authorization_url(
        self, *, redirect_uri: str, state: str, nonce: str, code_challenge: str
    ) -> str:
        """Where to send the browser.

        Deliberately **not** the parameters the Gmail flow sends. There is no
        ``access_type=offline`` and no ``prompt=consent``: sign-in never wants a refresh
        token, and forcing the consent screen on every sign-in would be friction with no
        security value. ``prompt=select_account`` is there instead, so that a browser
        already signed into several Google accounts gets to choose rather than being
        silently taken through as whichever one is first.

        PKCE is sent even though this is a confidential client with a secret. It costs one
        parameter and closes the window where an authorization code intercepted in the
        redirect could be redeemed by anything that did not start the flow.
        """
        query = urlencode(
            {
                "client_id": self._client_id,
                "redirect_uri": redirect_uri,
                "response_type": "code",
                "scope": " ".join(LOGIN_SCOPES),
                "state": state,
                "nonce": nonce,
                "code_challenge": code_challenge,
                "code_challenge_method": "S256",
                "prompt": "select_account",
            }
        )
        return f"{self._authorization_endpoint}?{query}"

    def complete(
        self, *, code: str, redirect_uri: str, code_verifier: str, nonce: str
    ) -> VerifiedIdentity:
        id_token = self._exchange(code=code, redirect_uri=redirect_uri, code_verifier=code_verifier)
        claims = self._verify(id_token, nonce=nonce)
        return _identity(claims)

    def _exchange(self, *, code: str, redirect_uri: str, code_verifier: str) -> str:
        response = self._post(
            self._token_endpoint,
            {
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "code": code,
                "code_verifier": code_verifier,
                "grant_type": "authorization_code",
                "redirect_uri": redirect_uri,
            },
        )
        status = response.status_code
        if status >= 300:
            raise IdentityError(
                f"Google rejected the sign-in code ({status}): {_error_detail(response)}"
            )
        body = response.json()
        id_token = body.get("id_token")
        if not isinstance(id_token, str) or not id_token:
            # No ID token means the scopes did not include `openid` — the console edit
            # that adds it is the fix, and saying so beats a generic failure.
            raise IdentityError(
                "Google's token response carried no id_token, so there is nothing to "
                "verify an identity against. The OAuth client must grant the 'openid' "
                "scope."
            )
        return id_token

    def _verify(self, id_token: str, *, nonce: str) -> dict[str, Any]:
        """Every check in this module's docstring, in one place.

        ``options`` disables PyJWT's own issuer handling and requires the claims that
        matter to be present at all — a token missing ``exp`` must be rejected, not treated
        as one that never expires.
        """
        import jwt  # noqa: PLC0415 — fake mode never verifies a token

        try:
            claims: dict[str, Any] = jwt.decode(
                id_token,
                key=self._key_for(id_token),
                algorithms=["RS256"],
                audience=self._client_id,
                leeway=LEEWAY_SECONDS,
                options={"require": ["exp", "iat", "aud", "iss", "sub"]},
            )
        except Exception as exc:  # noqa: BLE001 — PyJWT raises a family, all meaning "no"
            # Never echo the token. An ID token is a bearer-ish credential for its lifetime
            # and this repo's logs go to a shared observability stack.
            raise IdentityError(f"Google's ID token did not verify: {type(exc).__name__}") from exc

        if claims.get("iss") not in GOOGLE_ISSUERS:
            raise IdentityError("Google's ID token was issued by someone else.")
        if nonce and claims.get("nonce") != nonce:
            raise IdentityError(
                "This sign-in's ID token does not match the request that started it."
            )
        if claims.get("email_verified") is not True:
            raise IdentityError(
                "Google has not verified that address, so it is not proof of anything."
            )
        return claims

    def _key_for(self, id_token: str) -> Any:
        if self._signing_key is not None:
            return self._signing_key
        from jwt import PyJWKClient  # noqa: PLC0415

        if self._jwk_client is None:
            # Caches the key set, so a signed-in browser's sign-in does not re-fetch
            # Google's certificates every time.
            self._jwk_client = PyJWKClient(self._jwks_uri, timeout=int(self._timeout) or 1)
        return self._jwk_client.get_signing_key_from_jwt(id_token).key

    def _post(self, url: str, form: dict[str, str]) -> Any:
        if self._transport is not None:
            return self._transport.post(url, data=form)
        import httpx  # noqa: PLC0415 — fake mode never pulls in an HTTP client

        with httpx.Client(timeout=self._timeout) as client:
            return client.post(url, data=form)


def _identity(claims: dict[str, Any]) -> VerifiedIdentity:
    email = claims.get("email")
    subject = claims.get("sub")
    if not isinstance(email, str) or not email:
        raise IdentityError("Google's ID token carried no email address.")
    if not isinstance(subject, str) or not subject:
        raise IdentityError("Google's ID token carried no subject.")
    name = claims.get("name")
    return VerifiedIdentity(
        email=email, subject=subject, name=name if isinstance(name, str) and name else None
    )


def _error_detail(response: Any) -> str:
    """A short, safe rendering of an error body — never the whole thing.

    An OAuth error response echoes request parameters back, and these logs are shared.
    """
    try:
        body = response.json()
    except Exception:  # noqa: BLE001 — an error body is frequently not JSON
        return str(response.text)[:200]
    if isinstance(body, dict):
        detail = body.get("error_description") or body.get("error") or ""
        return str(detail)[:200]
    return str(body)[:200]
