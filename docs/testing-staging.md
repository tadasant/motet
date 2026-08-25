# Exercising staging as an authenticated agent

**The question this answers:** *"I am an agent session. How do I drive Motet's staging
environment as the signed-in owner, so I can test something end to end?"*

**The short answer:** ask the staging deploy to mint you a short-lived session token, and
send it as `Authorization: Bearer`. **Not** by signing in with Google — an agent cannot
complete Google's consent flow, and §4 is the evidence for that claim rather than an
assumption. **Not** with staging's `MOTET_API_TOKEN` either: no agent can read that value,
and putting a copy of it somewhere an agent could was considered and declined.

Everything below is about **staging**. Nothing here is a production procedure, and nothing
here should be made to work against production.

---

## 1. What you need, and where it comes from

**A session token, minted for you by the staging deploy on request, and short-lived.**
Not `MOTET_API_TOKEN` — that value is unreachable, deliberately, and no copy of it is kept
anywhere an agent can read.

| | |
|---|---|
| **Credential** | An ordinary `auth_sessions` token: 8 hours by default, 24 hours at the very most |
| **Where it comes from** | The `motet-mint-staging-session.yml` workflow in the private infrastructure repo, which executes the staging-only `motet-mint-session` Cloud Run job |
| **How you get it** | You give the workflow a public key; it gives you back ciphertext as a 1-day run artifact that only your session can decrypt |
| **What it authenticates as** | The single `motet-owner` account, carrying the allowlisted address — the same identity a signed-in browser gets |
| **How it goes inert** | `POST /v1/auth/logout` when you are done, or its TTL, whichever comes first |

> **Why the staging `MOTET_API_TOKEN` is not the answer, and why it is not in 1Password
> either.** The deploy identity holds Secret Manager admin *minus*
> `secretmanager.versions.access`, so an agent asking Google for that value gets a 403.
> Copying it into the estate's shared secret store was considered and **declined** (Tadas,
> 2026-08-25): it is a non-expiring, owner-equivalent credential, unwinding it later costs
> a Terraform apply plus three document corrections, and it would exist only to serve
> agents. The mint deletes the human step instead of documenting it, which is what
> [invariant 9](../AGENTS.md#9-one-time-setup-boundaries-are-human-owned-everything-inside-them-is-not)
> asks for. The bearer still works for everything it worked for before — the RSS feed, the
> iOS app, a script with the value already in hand. It simply is not obtainable from here.

### Minting one

```bash
# 1. An ephemeral keypair, in the durable scratch directory rather than /tmp.
cd "$AO_SESSION_SCRATCH_DIR"
openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:3072 -out mint.key
openssl pkey -in mint.key -pubout -out mint.pub

# 2. Dispatch the mint. `email` is optional and defaults to staging's allowlisted address.
#    Note the timestamp FIRST — step 3 depends on it.
SINCE=$(date -u +%Y-%m-%dT%H:%M:%SZ)
gh workflow run motet-mint-staging-session.yml \
  --repo tadasant/tadasant-internal \
  -f recipient_public_key="$(base64 -w0 mint.pub)" \
  -f ttl_hours=8

# 3. Find YOUR run — the one created after $SINCE. `gh workflow run` returns BEFORE the
#    run exists, so a bare `--limit 1` routinely picks up the PREVIOUS run, whose artifact
#    is very likely still there (retention is a day). That decrypts to garbage with your
#    key and surfaces much later as a 401, so filter on the dispatch time instead. If this
#    prints nothing the run has not appeared yet: run the same command again, rather than
#    wrapping it in a sleep loop — Zimmer denies `sleep`.
RUN=$(gh run list --repo tadasant/tadasant-internal \
  --workflow motet-mint-staging-session.yml --limit 20 --json databaseId,createdAt \
  --jq "[.[] | select(.createdAt > \"$SINCE\")] | .[0].databaseId // empty")
gh run watch "$RUN" --repo tadasant/tadasant-internal
gh run download "$RUN" --repo tadasant/tadasant-internal -n staging-session-token

# 4. Decrypt. This is the first moment the plaintext exists outside the workflow's shell.
TOKEN=$(base64 -d token.b64 | openssl pkeyutl -decrypt -inkey mint.key \
  -pkeyopt rsa_padding_mode:oaep -pkeyopt rsa_oaep_md:sha256)
```

Everything after that is §2 and §3 unchanged: the same `Authorization: Bearer` header, the
same `localStorage['motet.apiToken']` seeding. `motet_api.deps.require_caller` accepts a
session token in the slot the shared token goes in, so **no client, SPA, or request-path
code knows the difference** — which is the finding that made this cheap to build.

**The keypair is the access control, not the artifact's permissions.** An agent session's
`gh` credential *is* the owner's `gh` credential, so "only I can download it" is not a
boundary between the owner and an agent — it is the boundary between the estate and the
outside world. Encrypting to a key the requesting session generated is stronger than the
thing it replaces: no cleartext exists in Actions at all, not in a log, not in an output,
not in the artifact, and a second reader cannot use the artifact even once. A public key
is not a secret, so handing it over as a `workflow_dispatch` input leaks nothing.

**The token's entropy is the workflow's to get right, and nothing downstream can check
it.** The job validates that `--token-sha256` is a well-formed digest — a *shape* — and
that is all it can do, because the plaintext is generated upstream and the digest is
published in the job's execution record. A token drawn from a small space would therefore
be brute-forceable offline by anyone who can list that history. At least 32 bytes from a
CSPRNG, which is what the workflow's `openssl rand -base64 32` gives and what
`motet_db.auth.new_session_token` gives the sign-in path. This is the one guarantee the
digest split hands to the caller.

**Only the digest is ever an argument.** The workflow generates the token as a shell local,
passes `--token-sha256` to the job, and encrypts the plaintext to your key. A Cloud Run
execution records its own arguments, and anyone who can list the project's job history can
read them — so what is recorded there is a SHA-256, which cannot be presented to `/v1`. The
job (`motet_db.mint_session`) has no argument that takes a plaintext token and no code that
could derive one; `db/tests/test_mint_session.py` asserts both.

Two things this repo deliberately does not tell you, because it is public and
[AGENTS.md](../AGENTS.md#repo-split--read-this-before-you-put-a-file-anywhere) forbids both:
staging's **hostnames**, and the allowlist's **contents**. Both are in the private
infrastructure repo. Read them from there and set them as shell variables; the snippets
below assume `$MOTET_API` (the staging API origin) and `$MOTET_APP` (the staging SPA
origin).

### When the mint itself fails

| Symptom | What it means |
|---|---|
| The job execution fails with `MOTET_STAGING_SESSION_MINT is not set to 1` | The job definition lost its interlock, or you dispatched against an environment that has no mint job. Staging only, by construction. |
| The job execution fails with `it is not on MOTET_ALLOWED_EMAILS` | The `email` input is not staging's allowlisted address. The mint deliberately cannot admit anyone Google sign-in would refuse. |
| The job execution fails with `MOTET_ALLOWED_EMAILS is empty or unset in this process` | A different fault with the same shape: the *job definition* never injected the variable. The mint job is a separate container from the API service, so it needs its own copy. Fix the job's environment, not the list. |
| `--ttl-seconds must be at most 86400` | `ttl_hours` above 24. The cap is in the image, not only in the workflow. |
| The job execution fails with `No module named motet_db.mint_session` | Staging is running an image that predates the mint. The pin lags this repo's `main` by however long the last bump was ago; bumping it is the private infrastructure repo's business. |
| The job execution fails with `a session already exists for this digest` | A retried task, re-running with the same arguments. The *first* attempt committed a live session, so a token is out there that you do not hold: revoke it with `POST /v1/auth/logout-all` (it takes `MOTET_API_TOKEN`) and dispatch a fresh mint. |
| The artifact is missing | The run failed before the upload, or it is more than a day old — `retention-days: 1`. Dispatch another; nothing is reused. If the *job* had already succeeded when the run failed, a live session exists that nobody can revoke by id — `/v1/auth/logout-all` is the lever. |
| `401` on `/v1` with a freshly minted token | Decryption produced something other than the token — check for a trailing newline — or the TTL has passed, or you downloaded an artifact from a run that predates your dispatch (see step 3). `GET /v1/auth/session` is the cheap discriminator. |

## 2. Driving the API

```bash
# $TOKEN is the session token minted in §1. Nothing below knows how it was obtained.

# Liveness and wiring, no credential needed. Check this first — it tells you which
# capabilities the running image actually has.
curl -s "$MOTET_API/internal/health"

# Anything under /v1 takes the bearer.
curl -s -H "Authorization: Bearer $TOKEN" "$MOTET_API/v1/news-items"
curl -s -H "Authorization: Bearer $TOKEN" "$MOTET_API/v1/auth/session"
```

`/internal/health` is the right first call every time. It reports `inference_mode`,
`authenticated`, `login_configured`, and whether telemetry is actually exporting — which is
how you tell a staging deployment that is missing a variable from one that is broken. It is
unauthenticated on purpose; `/healthz` does not exist and never will, because Cloud Run's
frontend eats that path.

`GET /v1/auth/session` answers `how` — `session` for a session token, whether it came from
Google or from the mint; `token` for the shared `MOTET_API_TOKEN`; and `open` for a
deployment with no `MOTET_API_TOKEN` set at all. That is the cheapest way to confirm what
you are actually authenticating as, and `open` on a deployed environment is a finding, not
a convenience. A minted session also reports the `email` it was minted for and its
`expires_at`, which is how you tell how much of your window is left.

**Nothing happens on the request thread.** `/v1` writes a row and enqueues; every stage —
integrate, assemble, script, TTS — is a separate Cloud Run job draining a Postgres queue
(`workers/`). So a paste that returns `201` has not been processed yet, and an episode sits
in its queue until a job runs. Triggering those jobs in staging is a workflow dispatch in
the private infrastructure repo, which an agent can do; the workflow's name is recorded
there. Budget for the round trip rather than treating a `201` as "done".

## 3. Driving the SPA

The SPA accepts the same token. Its "API token" disclosure in the header — not a Settings
screen; there isn't one — writes to `localStorage` under the same key a Google session
occupies, so a browser-driving agent seeds it directly rather than typing into the field:

```javascript
await page.goto(`${MOTET_APP}/`);
await page.evaluate(t => localStorage.setItem('motet.apiToken', t), TOKEN);
await page.reload();
```

> Check the key name against [`web/src/api/client.ts`](../web/src/api/client.ts) rather than
> trusting the line above — it is the one detail here that lives in code and can move.

This exercises every screen the way a human sees it — and with a minted session it does so
including the header's address and "Sign out" button, because a minted session *is* a
session and carries the address it was minted for. Seeded with the shared
`MOTET_API_TOKEN` instead, those two are absent: the bearer belongs to no person, so
`/v1/auth/session` has no `email` to report.

## 4. Why you cannot sign in with Google instead

This was tested directly, on 2026-08-25, from a Zimmer session holding a Playwright browser
and read-only Gmail access to one of the owner's Google accounts. **It does not work, and it
is not a matter of trying harder.** Three independent blockers, any one of which is fatal:

**Google refuses the automated browser before it asks for a password.** Navigating to the
sign-in page and submitting an address lands on `accounts.google.com/v3/signin/rejected` —
*"Couldn't sign you in. This browser or app may not be secure."* This happened twice: once on
the default headless context, which Google downgraded to its no-JavaScript `WebLiteSignIn`
flow, and again on a context with a realistic user agent, viewport, locale, timezone, and the
usual `navigator.webdriver` masking, which reached the normal flow and was rejected at the
same step. The rejection is at the *identifier* step — no password was ever requested, so no
amount of credential plumbing changes the outcome.

**There is no password to supply, and there should not be.** Driving a real consent screen
needs a human's primary Google account password plus whatever second factor that account
carries, and Google's step-up for a new device on a datacenter IP is frequently a push prompt
to a phone. No MCP server answers a phone prompt. Storing a human's Google password somewhere
an agent can read it would also be a far larger blast radius than the entire staging
environment it was meant to test.

**The account an agent has Gmail access to is not on staging's allowlist anyway.** They are
two different Google identities: `MOTET_ALLOWED_EMAILS` for staging names one, and the
read-only Gmail MCP server is scoped to the other. A sign-in as the latter would verify at
Google and then be refused with a 403 by the sign-in route, on
[`motet_db.allowlist`](../db/src/motet_db/allowlist.py)'s answer — working
exactly as designed. And because the mailbox an agent can read is not the allowlisted one, it
is not a route to an emailed verification code either. The allowlist's contents are personal
data and live in the private infrastructure repo; check there before assuming this changed.

**Do not spend session time re-testing this.** If you think it has changed, the cheap check is
the first blocker alone: drive a browser to Google's sign-in page and see whether it still
lands on `/signin/rejected`.

## 5. What this does and does not prove

Be precise about this when you report a green run, because the gap is not small.

**What a minted session does exercise:** every `/v1` route that does product work, the whole
ingestion → dedup → assemble → script → grounding → TTS → storage pipeline, the RSS feed, the
SPA's screens including the signed-in header, and the real vendors behind them in staging's
`real` inference mode. It also exercises the session machinery itself against a real
deployment — a row created, resolved on every request, re-checked against the allowlist,
and revoked by `/v1/auth/logout`. Expiry is *configured* rather than exercised: an 8-hour
session will not lapse during a session that holds it, so the TTL is a claim CI covers and
staging does not. That aside, this is what the mint bought over the shared bearer, and it
is *creation by CI* rather than creation by the sign-in route.

**What it does not exercise, at all:**

- Google's consent screen, and whether this environment's redirect URI is still registered on
  the OAuth client. That registration lives in the private repo and **nothing in this repo can
  tell you it drifted** — a wrong redirect URI fails only at the moment a human clicks the
  button.
- The authorization-code exchange, the ID-token verification against Google's JWKS, the
  `nonce` binding, and the `email_verified` check — the code in
  [`motet_api/auth/google.py`](../api/src/motet_api/auth/google.py). In staging these run
  against the real Google; under the bearer they do not run at all.
- **Session creation *through the sign-in route*.** The mint writes the row directly, so
  `POST /v1/auth/google/callback` — the allowlist check on the sign-in path, the state
  consumption, the 30-day TTL that path chooses — is not on trial. This is the one gap a
  redeemable one-time token (§7's variant B) would have closed, and it was judged not worth
  a new authentication route in the deployed API.

CI covers more of that than you might expect, and it is worth knowing which parts.
[`api/tests/test_auth.py`](../api/tests/test_auth.py) exercises the third bullet against
[`FakeIdentityProvider`](../api/src/motet_api/auth/fakes.py), and the **second directly
against the real `GoogleIdentityProvider`** — a locally generated RSA key injected as its
signing key, and a stub transport for the token endpoint. So the verifier's own logic is
genuinely covered. What no test can reach is Google: the live JWKS fetch is stubbed, the
consent screen is never rendered, and the redirect-URI registration is not ours to read.

> **So: a green agent run does not prove a human can sign in.** After any change to
> `api/src/motet_api/auth/`, to `web/src/oauth.ts`, or to the OAuth client's registered
> redirect URIs, a human still has to click the real "Sign in with Google" button once per
> environment. That is the residual manual step, and it is a legitimate one — completing an
> OAuth consent is a human-owned boundary under invariant 9, not an operation to design out.

## 6. When it fails

| Symptom | What it means |
|---|---|
| `401` on `/v1/*` | An expired, revoked or wrong token — the two environments share nothing, and a minted session lapses on its TTL. `GET /v1/auth/session` is the one that tells you what you actually authenticated as; §1's table covers the mint-specific causes. |
| `/internal/health` returns `login_configured: false` | Either `MOTET_ALLOWED_EMAILS` is unset or, in `real` mode, the Google OAuth client is not configured. Sign-in is off. A minted session still works — it does not go through the sign-in route — but an unset allowlist means the mint refuses too. |
| `/v1/auth/*` returns `404` | The running image predates Google Sign-In. Check the deployed image pin — this is not something to fix from here. |
| A paste or episode never leaves its queue | Nothing drained it. `/v1` only enqueues; the stage jobs are what do the work. Dispatch them, then check the obs stack. |
| `/internal/health` fine, every `/v1` call times out | Check the obs stack before assuming the API is down; a worker queue backing up looks like this from the outside. |
| `404` on `/healthz` | Expected. Health is at `/internal/health`; Cloud Run's frontend answers `/healthz` itself. |
| Anything needing a shell on the box | There isn't one, and building one is not the fix. Invariant 10. |

## 7. How the mint is kept out of production

§1 is the procedure; this is the reasoning behind it, recorded so that neither half is
re-derived from scratch. It was settled on the design in
[tadasant-internal#1620](https://github.com/tadasant/tadasant-internal/issues/1620), and
approved by Tadas on 2026-08-25.

**Three independent things would have to change for a session to be mintable against
production**, and two of them are diffs a reviewer sees:

1. **The job does not exist there.** `motet-mint-session` is created in staging and
   nowhere else. In the production project there is nothing to execute.
2. **The workflow reaches staging and nothing else.** `motet-mint-staging-session.yml`
   authenticates to one environment, and it is not production.
3. **`MOTET_STAGING_SESSION_MINT=1`** must be set on the job's own definition, or
   `motet_db.mint_session` refuses and exits non-zero.

The first two are enforced in the private infrastructure repo's `motet/` — the Terraform
predicate and the workflow's deploy identity are recorded there, not here, because this
repo is public and [AGENTS.md](../AGENTS.md#repo-split--read-this-before-you-put-a-file-anywhere)
keeps deployment topology out of it. What is stated here is the *property*, which is what
a reader of this runbook needs; the mechanism is one repo over.

The third is the smallest of the three deliberately: it is an interlock on a door in a
building that has not been built in that town. **This is isolation by structure rather than
by configuration** — the distinction matters, because the earlier sketch this section used
to describe (a `POST /v1/auth/staging/session` route, gated on a staging-only secret) had
isolation that was one unset variable deep, and one variable is one mistake.

**What it is not: a new authentication path.** Nothing was added to the API — no route, no
header, no second way to present a credential. `motet_api.deps.require_caller` already
accepted a session token in the `Authorization: Bearer` slot, because that is how a
signed-in browser works. What changed is that a *second writer* of the `auth_sessions`
table exists, and it is a Cloud Run job with `DATABASE_URL` and nothing else.

**The honest widening, stated rather than glossed: CI can now write an `auth_sessions` row
directly, bypassing sign-in.** In staging that is not new reach — CI already applies every
migration and replaces every service revision there, so it could always have written that
row with `psql` — but it is a real change in what CI *does*, and it is the reason the
allowlist check is in the job rather than only in the workflow: even CI cannot mint a
session for an address Google sign-in would refuse.

**Blast radius, if a minted token leaked.** The holder could do anything the owner can do
*on staging*, for the remainder of a session that lives hours rather than days: ingest text,
spend staging's vendor budget, read staging's test data, rotate staging's feed token. It
reaches no production data, no Google account, and no user mailbox — the vault's decrypt is
scoped to the worker service account and the API cannot unseal a credential (invariant 8) —
and no GCP credential. `POST /v1/auth/logout` kills exactly that session;
`/v1/auth/logout-all`, which takes the shared token, kills every session at once.

**Variant B, declined.** The alternative was a redeemable one-time token: CI writes a
`staging_mint_tokens` row and the agent trades it at a new `POST /v1/auth/staging/session`,
with redemption as one atomic `UPDATE ... WHERE redeemed_at IS NULL` so a race has a
deterministic loser. It buys the one thing §5 still lists as unexercised — session creation
*through the API* — at the cost of a new authentication route in the deployed **production**
API, guarded only by a secret being unset there. It also would not exercise Google, so it
would not shrink the manual smoke test by a single click. Not worth it; if it is ever
revisited, that is the shape it takes.

---

## Related

- [AGENTS.md](../AGENTS.md) — the settled decisions, including why the allowlist rather than
  Google is the security control, and why a session is a bearer token rather than a cookie.
- [CONTRIBUTING.md](../CONTRIBUTING.md) — local setup and `bin/ci`.
- The private infrastructure repo's `motet/` — hostnames, secret names, the allowlist's
  contents, and the manual-setup runbook.
