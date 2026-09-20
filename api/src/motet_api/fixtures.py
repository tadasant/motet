"""The staging test harness: one flag, one boot refusal, and three capabilities.

An agent has to be able to drive Motet end to end on staging with nobody in the loop —
connect the test mailbox, sync it, build an episode, check the result, and put the account
back to a defined baseline so the next run starts where the last one did. Three things
stood between that and the API as shipped, and each is here:

1. **Seeding a Gmail credential without a consent click.** There is no machine-to-machine
   OAuth for a consumer ``@gmail.com`` account — Google's only mechanism for a service
   account to act as a mailbox is domain-wide delegation, which needs Workspace. So the
   consent is a human step performed once (invariant 9's human half, exactly as written),
   its refresh token goes into Secret Manager, and :func:`seed_gmail_source` re-establishes
   the connected state from it on demand.
2. **Reset.** :func:`motet_db.fixtures.reset_user`. Without it "the assertions after a run"
   and "whatever the last run left behind" are the same thing.
3. **Triggering a job and watching it.** The product already has "poll this source" and
   "make an episode"; what it has no surface for is *which job that was* and *whether
   anything is draining the queue it went on*. That pair is the difference between slow and
   broken, and the reason sessions #19159 and #19202 had to exist.

**Sealing widens nothing, and that is why this lives in the API.** The credential is
written by exactly the call the OAuth callback makes —
:func:`motet_db.phase2.store_source_credential`, over ``deps.dek_wrapper``'s encrypt-only
:class:`~motet_vault.DekWrapper` — so the record is envelope-encrypted with a per-record
DEK under the same KEK, carrying the same ``user_id:source_id:provider`` AAD, and the API's
service account still holds ``useToEncrypt`` and not ``useToDecrypt``. Invariant 8 is
untouched in both directions: nothing here can open a credential, and nothing new can
either. Routing the write through the worker was the fallback if sealing had needed
decrypt; it does not, and a second process in the path would have bought coupling rather
than safety.

**The refresh token itself is the one thing that is different, and it is worth saying
out loud.** It reaches this process as a plaintext environment variable injected from
Secret Manager, which is how every other secret reaches these services and is *not* how a
user's mailbox token reaches them. What makes that acceptable is bounded and stated rather
than assumed: it is one throwaway test inbox (invariant 13 — staging's secrets are the
non-sensitive kind by construction), it exists in staging alone, and the flag below is what
stops the variable being read anywhere else. It is not a pattern to copy for a real user's
mailbox.

**What the flag turns on, so it can be reviewed on exactly that basis.** With
``MOTET_TEST_FIXTURES=1`` an authenticated ``/v1`` caller gains four routes: seed a Gmail
source from the staging refresh token, delete their own sources/items/episodes, enqueue a
poll or an episode and get the job id back, and read any job of theirs. Two of those
destroy data. With the flag unset every one of them answers 503 and reads nothing.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from typing import Final

from motet_obs import resolve_deployment_environment

logger = logging.getLogger("motet.api.fixtures")

#: The interlock. Must be exactly ``1`` in the process environment, or the routes are off.
#:
#: Exactly ``1`` rather than "truthy", which is :data:`motet_db.mint_session.MINT_ENABLED_ENV`'s
#: rule and for its reason: ``0``, ``false`` and ``no`` all have to mean no, and a value
#: somebody meant well by but this code cannot parse must refuse rather than guess. Unset
#: means off, like an unset allowlist means nobody.
#:
#: Deliberately **not** run through ``config._truthy``, which logs an unrecognised value and
#: reads it as off. That is the right trade for a feature flag whose failure is a fallback;
#: it is the wrong one here, where the symmetric mistake — a value that switches a
#: destructive surface on by accident — is the one worth being unable to make.
TEST_FIXTURES_ENV: Final = "MOTET_TEST_FIXTURES"

#: Where the test mailbox's refresh token arrives, injected from Secret Manager by the
#: staging service definition. The secret is named for the variable, which is this estate's
#: convention; the value, the project and the grant are the private repo's business and are
#: named nowhere here.
#:
#: **Unset means "not provisioned yet", which is a state rather than a fault.** Cloud Run
#: refuses to create a revision whose secret has no enabled version, so the mount stays off
#: until a human places the value once (invariant 9's human half — the consent that produced
#: the token is a person's click, and so is putting it where the deploy can reach it). The
#: seed route's 503 says exactly that, because "the fixture is not set up yet" and "seeding
#: is broken" are otherwise the same red line.
GMAIL_REFRESH_TOKEN_ENV: Final = "MOTET_STAGING_GMAIL_REFRESH_TOKEN"

#: Which mailbox that refresh token reaches, as a plain non-secret variable set beside it.
#:
#: An address is not a credential, so it is configuration rather than a secret — and having
#: it means the seed can record the expected account **without a profile call**, which the
#: API has no business making (it speaks to no vendor; invariant 9's split puts every vendor
#: call in the worker). Recording it is what makes the worker's own account check bite: a
#: refresh token for some other inbox disconnects the source on the first poll instead of
#: quietly ingesting the wrong mail. A request may override it; unset and unnamed means the
#: source records whatever the first poll sees.
GMAIL_ADDRESS_ENV: Final = "MOTET_STAGING_GMAIL_ADDRESS"

#: What :func:`check_startup` refuses to boot beside — exact matches, plus anything whose
#: name *contains* :data:`PRODUCTION_MARKER`.
#:
#: **A denylist over a string written in a repo this one cannot read is the wrong shape,
#: and the substring is how far it can be pushed from here.** The value arrives as an OTel
#: resource attribute set by the private infrastructure repo's service definitions, so this
#: repo can neither read it nor constrain it; an allowlist ("refuse unless the environment
#: is a known-safe one") would be the safe shape and would refuse a laptop, CI, and any
#: environment somebody adds later — which is the whole population that has to keep working.
#: So: the two exact spellings, and a substring that also catches ``prod-eu``,
#: ``production-us`` and ``motet-production``.
#:
#: **The residue is stated rather than closed**: a production environment named without the
#: string — ``prd``, ``live`` — boots with the harness on. What stops that is the same thing
#: that stops it today, which is that turning the flag on in production is a diff in the
#: private repo a reviewer sees.
PRODUCTION_ENVIRONMENTS: Final = frozenset({"production", "prod"})

#: Any environment whose name contains this is production, however it is spelled around it.
PRODUCTION_MARKER: Final = "prod"

#: The default name a seeded mailbox is created under, and the key re-seeding matches on.
DEFAULT_FIXTURE_SOURCE_NAME: Final = "Staging test inbox"


class FixturesRefused(RuntimeError):
    """This deployment may not serve the test harness, and must not start pretending to.

    Raised out of the API's lifespan, where Cloud Run reports a failed revision and never
    shifts traffic to it — the same shape ``config.ConfigError`` describes, and the reason
    the guard is at startup rather than inside the routes. A permission check deep in a
    request path is a check somebody has to reach in order to discover; a refusal to boot
    is one that cannot be missed and that cannot be the thing a mistake gets past.
    """


def fixtures_enabled(env: Mapping[str, str] | None = None) -> bool:
    """Whether the test-harness routes do anything in this process."""
    environ = os.environ if env is None else env
    return environ.get(TEST_FIXTURES_ENV, "").strip() == "1"


def check_startup(env: Mapping[str, str] | None = None) -> None:
    """Refuse to start if the harness is switched on in production.

    "Production" is read from the ``deployment.environment`` attribute the deploy already
    stamps on every telemetry record (:func:`motet_obs.resolve_deployment_environment`),
    rather than from a variable invented for this: a second name for the environment is a
    second thing that can disagree about which one this is, and the one that already exists
    is set unconditionally by the service definitions rather than opted into per feature.

    **An unknown environment is allowed, and that is the honest reading rather than a gap
    papered over.** A laptop, CI and a bare ``docker run`` set no resource attributes at
    all, and the harness has to work in the first two — so "no environment attribute"
    cannot be made to mean production without taking the tests with it. What bounds the
    risk is that reaching production through that gap needs *two* independent changes, both
    of them diffs in the private infrastructure repo that a reviewer sees: the fixtures
    flag added to the production service, and that same service losing the attribute every
    other environment carries. It is :mod:`motet_db.mint_session`'s argument — three
    interlocks, of which the one in this repo is deliberately the smallest — and the
    warning below is so that the gap is never silent.
    """
    environ = os.environ if env is None else env
    if not fixtures_enabled(environ):
        return
    environment = (resolve_deployment_environment(environ) or "").strip().lower()
    if environment in PRODUCTION_ENVIRONMENTS or PRODUCTION_MARKER in environment:
        raise FixturesRefused(
            f"{TEST_FIXTURES_ENV}=1 in the {environment!r} environment. The test harness "
            "seeds credentials and deletes a user's sources, items and episodes; it exists "
            "for staging and must never be reachable in production. Unset the variable on "
            "this service."
        )
    if not environment:
        logger.warning(
            "%s=1 and no deployment.environment is set, so this process cannot tell which "
            "environment it is in. Expected on a laptop and in CI; on a deployed service it "
            "means OTEL_RESOURCE_ATTRIBUTES lost the attribute that would refuse this.",
            TEST_FIXTURES_ENV,
        )
    else:
        logger.warning(
            "%s=1: the staging test harness is serving in %r. Four authenticated routes "
            "under /v1/testing are live, two of which delete data.",
            TEST_FIXTURES_ENV,
            environment,
        )


def staging_refresh_token(env: Mapping[str, str] | None = None) -> str | None:
    """The test mailbox's refresh token, or None when the deployment was not given one.

    Never logged, never returned by a route, and never compared — the one thing done with
    it is sealing it (invariant 8), and the plaintext exists only as a local in the caller.
    """
    environ = os.environ if env is None else env
    return environ.get(GMAIL_REFRESH_TOKEN_ENV, "").strip() or None


def staging_mailbox(env: Mapping[str, str] | None = None) -> str | None:
    """Which mailbox :func:`staging_refresh_token` reaches, when the deployment names one."""
    environ = os.environ if env is None else env
    return environ.get(GMAIL_ADDRESS_ENV, "").strip() or None
