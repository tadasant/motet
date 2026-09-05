# AGENTS.md — Motet

*(`CLAUDE.md` is a symlink to this file.)*

> *A motet layers several different texts sung simultaneously into one coherent piece —
> many sources, one thing worth hearing.*

Motet (`getmotet.com`) turns a reading backlog — newsletters, X bookmarks — into an
interactive podcast you listen to on a dog walk and can interrupt with your voice.

**This file holds the settled decisions.** It exists so that agents working on this repo
do not re-litigate them. If you find yourself about to argue with something below, the
bar is not "I have a better idea" — it is "the reason this was decided no longer holds,
and here is why." Say that out loud in your PR rather than quietly building the other
thing.

---

## Repo split — read this before you put a file anywhere

| | |
|---|---|
| **`tadasant/motet`** (this repo, **public**) | Application code. API, workers, inference adapters, SPA, voice service, iOS app, migrations, the golden set. |
| **`tadasant/tadasant-internal`**, under `motet/` (**private**) | Infrastructure. Terraform/IaC, staging and production config, deploy workflows, environment topology, secret *names* and wiring. |

**Never put a secret, a GCP project id, a bucket name, a service-account address, a
hostname of internal infrastructure, or any topology detail in this repo.** It is public.
Application code reads configuration from the environment and does not know what is
behind it. If a change seems to require an infrastructure fact in this repo, that is the
signal it belongs in the private repo instead — say so and stop rather than inlining it.

Deploy workflows live in the private repo. CI in *this* repo runs on the shared
self-hosted runner pool behind a fork guard, with one job on a GitHub-hosted macOS runner
because `xcodebuild` needs a Mac — see [Runner policy](#runner-policy).

---

## Product invariants

These come from `target-system-design.md`. They are the load-bearing shape of the system;
almost every design question that comes up is already answered by one of them.

1. **The client never speaks a vendor protocol.** No client — iOS, web, or anything else —
   talks to OpenAI, Cartesia, Anthropic, or any other provider directly. Everything goes
   through our own API and our own session contract. This is what makes a provider swap a
   service change instead of a client rewrite.

2. **The voice service never touches the news DB.** It receives a session config
   (persona, tools, MCP servers, context, turn policy) and calls tools. It has no database
   credentials and no schema knowledge. This is what lets the voice service be reused —
   by Zimmer, among others — rather than being welded to Motet's data model.

3. **Every reported claim carries a source span, validated before TTS — a gate when Motet
   reports, advisory when it converses.** A briefing that invents a funding number is dead.
   The two halves are *not* symmetric, and the asymmetry is the decision (Tadas,
   2026-08-24, motet#10) rather than an implementation that has not caught up:

   | | **Narration** — the briefing | **Conversation** — answering a question |
   |---|---|---|
   | Where | `workers/` script → grounding → Cartesia | `voice/` `VoiceSession.respond_to_text` |
   | Grounding | **hard gate.** A claim that fails validation is not synthesized. | **advisory.** The reply is spoken, then checked. |
   | Checked by | `GroundingValidator`, a max-effort model call | `motet_voice.grounding`, ours, local, deterministic |
   | If it fails | nothing gets spoken | it was already spoken; a counter, a warning and a `grounding` event record it |

   **The narration half is not negotiable.** It is where a briefing is *made*, it is
   asynchronous, and it has all the time in the world. Do not weaken it.

   **The conversational half is advisory because a gate there is a silence.** The reply is
   generated inside a spoken turn with a listener standing on a pavement waiting for it,
   and the batch validator cannot live in that budget. So the check runs *behind* the
   reply — off the critical path — and never blocks the audio.

   **Advisory is not absent, and the difference is entirely what survives the turn.**
   Every conversational reply is checked for fabricated specifics — a number, a name or a
   quotation the session's material does not contain — and every verdict is recorded:
   `motet.voice.conversational_replies{grounded="false"}` on the obs stack, a warning
   carrying the offending text, a `grounding` event to the client, and a count in the
   session summary. "How often does Motet say something it cannot source out loud?" has an
   answer, in Grafana. A change that removes the recording removes the invariant, whatever
   it leaves behind.

   The conversational check does **not** judge paraphrase or entailment; it catches
   invented specifics, which is invariant 3's own named failure mode. A model-backed
   entailment check drops in behind `ConversationGroundingChecker` when the reply path
   grows a *new* source of material — research results, a second corpus, memory across
   sessions — because that is when the risk stops being paraphrase over already-grounded
   text. See the tripwire below: none of this is deferrable.

4. **`spoken_through_ms` is tracked by us, not the provider.** We own playback position.
   Never read it back out of a vendor SDK and never trust a provider's notion of where the
   user is in the audio.

5. **Read state is per News Item, and syncs across audio and visual.** Not per episode, not
   per segment, not per source item. Marking something read on the web backlog must be the
   same fact as having listened past it in an episode.

6. **Ingestion is serialized per user.** Two ingestion runs for the same user never
   overlap. Dedup/integrate compares a new source item against the current window of news
   items, so concurrent runs would race and produce duplicate news items.

7. **Every inference stage sits behind an interface with a fake for tests.** Dedup/integrate,
   script generation, grounding validation, and TTS each have a Protocol in
   `inference/` and a deterministic fake alongside the real adapter. Tests and CI use the
   fakes. No test in this repo may make a real vendor call.

8. **Source credentials are never plaintext at rest; only workers can decrypt.** Envelope
   encryption, Cloud KMS KEK, per-record DEK, AAD bound to `user_id:source_id:provider`.
   The decrypt permission is scoped to the worker service account — that IAM boundary is
   the actual control, not the encryption.

---

## Operating invariants

Settled with Tadas in Zimmer session 8241. These govern how the system is built and run,
not what it does.

### 9. One-time setup boundaries are human-owned; everything inside them is not

Some steps happen **once**, at the edge of the system, and a human does them:

- provisioning a vendor account
- minting a *first* API key
- completing an OAuth consent
- registering a domain
- creating a developer identity (App Store Connect, and the like)

Agents never automate across that boundary. It is the boundary on purpose — it is where a
human decides the system may spend money, hold an identity, or accept a terms-of-service.

**Everything inside that boundary is the opposite, and this half is the one that gets
violated.** Deploying, rotating an *already provisioned* secret, adding a DNS record,
scaling a service, running a migration, reading logs and metrics — all of it must be
reachable by an agent through CI, an API, or an MCP tool, with no human in the loop.

> **A routine operation that needs a human is a defect to be designed out**, not a runbook
> step to write more clearly. If you build a feature whose operation implies "ask Tadas to
> go click something," you have not finished the feature.

When you hit a genuine one-time boundary mid-task, do not improvise around it: write it
down as a provisioning step (what to create, where the credential goes) and hand it back.

### 10. No production box access, ever

There is no shell on a production host in the supported path. The promotion path is:

```
experiment in staging → bake the learning into CI-driven code that deploys staging
→ verify in staging → promote to production
```

**Production is only ever changed by CI.** Not by an agent with a terminal, not by a human
with a terminal. If the only way to fix something is to log into the box, the fix is to
build the deploy/job/API surface that makes logging in unnecessary — and to say plainly,
at the place the manual step is written down, that it is a workaround rather than the
procedure.

Staging exists at every layer that is not an external service. For external dependencies,
staging uses a throwaway account, a read-only scope, or a fake — never a credential whose
leak would matter.

### 11. Observability goes to the self-hosted obs stack, not GCP Cloud Logging

Telemetry — metrics, logs, errors — goes to the existing self-hosted stack at
`obs.tadasant.com` (Grafana / VictoriaMetrics / VictoriaLogs / GlitchTip). **Not** GCP
Cloud Logging, Cloud Monitoring, or Error Reporting.

This is load-bearing rather than a preference: **there is deliberately no GCP MCP server**,
so the obs stack is the *only* way an agent can see how production is behaving. Telemetry
that lands in Cloud Logging is telemetry no agent can read, which means a whole class of
bug becomes undebuggable without a human. Any component that emits telemetry emits it
there.

The wiring follows the contract the rest of the estate already uses — the standard OTel
SDK environment variables, plus a GlitchTip DSN:

| Variable | Carries |
|---|---|
| `OTEL_EXPORTER_OTLP_ENDPOINT` | the obs stack's OTLP ingest base |
| `OTEL_EXPORTER_OTLP_HEADERS` | the ingest bearer token |
| `OTEL_SERVICE_NAME` | `motet-api`, `motet-worker`, … |
| `SENTRY_DSN_BACKEND` / `SENTRY_DSN_FRONTEND` | GlitchTip projects |

The endpoint values and tokens live in the private repo; this repo only ever reads the
names. Every exporter **no-ops cleanly when its variable is unset**, so local development
and CI need no obs stack at all.

> **The trap that comes with that**, learned on Zimmer: a silent no-op is indistinguishable
> from a healthy, quiet service. **Never infer "no errors" from "no data."** Ask the app
> instead — `motet_obs.status()` reports which exporters are actually configured, and it is
> exposed on the API's health surface.

**The wiring lives in `obs/` (`motet-obs`), not in `api/`, and that is structural.** The
worker is the process that makes every vendor call, has no health route to ask, and is the
one whose silence costs money — and it could not reach a module inside `api/`, because
`motet-api` depends on `motet-workers` and the arrow only goes one way. Telemetry that only
the API can reach is telemetry the interesting half of the system does not have.
`motet-obs` therefore depends on **no `motet-*` package** and must not start: every
deployable imports it, and each passes its own fallback service name
(`motet-api`, `motet-worker`) because that label is what an operator filters on.

**"Configured" and "exporting" are two questions.** `telemetry_configured` says somebody
set the variables; `telemetry_exporting` says this process built a provider and is batching
data out of it. The first was true for months while the second was false — the SDKs were
not a dependency at all — which is exactly how a service looks monitored and emits nothing.
The health route reports both, and `obs/tests/test_export.py` asserts the second by running
a real process against a local OTLP collector and decoding what arrived, because that is
the one claim no flag can support.

Only **`http/protobuf`** is installed: the obs stack ingests OTLP over HTTP, so the gRPC
exporter would buy nothing and cost a second transport to reason about. It used to also be
the argument that `grpcio` was in neither image, and that half has expired —
`google-cloud-kms` brings `google-api-core[grpc]` and `grpcio` with it, unconditionally and
whichever transport KMS is asked for. The conclusion is unchanged; one of its reasons is
not. A different
`OTEL_EXPORTER_OTLP_PROTOCOL` is logged as an error at startup rather than silently
half-honoured.

**Health is served at `/internal/health`, never at `/healthz`.** Google's Cloud Run frontend
answers `/healthz` with its own 404 *before the request reaches the container*, so an
endpoint there is unreadable from everywhere health is actually checked — and a container-
local smoke test cannot see that, because there is no frontend in front of `docker run`.
That is how it shipped (motet#16). `/_ah/*` is reserved on the same infrastructure. Both
are listed in `motet_api.main.PLATFORM_RESERVED_PATHS`, copied in `motet_voice.app` and in
`bin/build-images`, and guarded by a test that walks every declared route.

Two of those names have a second spelling, and it is not cosmetic. Secret Manager holds one
value per secret and the CI identity that applies the infrastructure **cannot read a secret
back** — so a service definition can inject a secret under its own name and nothing more.
It cannot read `OTEL_INGEST_TOKEN` in order to compose the `Authorization=Bearer <token>`
string that `OTEL_EXPORTER_OTLP_HEADERS` wants. Composing it is therefore the *process's*
job, and `GLITCHTIP_DSN` is the same story without the formatting:

| The app accepts | …as well as | Because |
|---|---|---|
| `OTEL_INGEST_TOKEN` (raw bearer) | `OTEL_EXPORTER_OTLP_HEADERS` | Terraform cannot build the header string |
| `GLITCHTIP_DSN` | `SENTRY_DSN_BACKEND` | it is the name the secret was placed under |

**An endpoint without a credential is not "configured."** obs rejects an unauthenticated
export, so that combination buys a 401 per export rather than data — which reads as an obs
fault. `/internal/health` reports `telemetry_configured: false` for it deliberately, and
startup logs a warning saying so.

---

## Tripwires

Signals that the project has gone wrong. If one fires, stop and re-plan rather than
pushing through.

- **The SPA is not the product.** It is the eyes-on backlog surface, and in Phase 1 it is
  three thin screens over the API. If SPA work is still running after a week, something has
  gone wrong — you are building a product instead of a factory.
- **Never reach for Redis or a vector store.** Postgres holds the data *and* the job queue
  (`SELECT ... FOR UPDATE SKIP LOCKED`). A day of news items is about 4.5k tokens, which is
  passed in-prompt; there is nothing to embed. Reaching for either is a sign of solving a
  scale problem this system does not have.
- **Never defer grounding validation.** It shapes the script contract — the script format
  exists *so that* claims can carry source spans. Adding it later is not a feature, it is a
  rewrite of everything downstream of the script. On the conversational path it is advisory
  rather than a gate (invariant 3), and *that* is deferrable in exactly one direction:
  making it advisory was a decision, making it silent would not be. A conversational reply
  that is spoken without being counted is the same defect wearing a different hat.

---

## Phase 1 — Infra MVP

Paste arbitrary text in, get an episode out, listen on a dog walk. One hardcoded user.

```
paste-in → Source Item → News Item (deduped, grounding-validated)
        → Episode → script → Cartesia Sonic → GCS → private authenticated RSS feed
```

**In:** paste-in ingestion, dedup/integrate, manual episodes ("all unread", duration-capped),
script + grounding validation, TTS, GCS, private authenticated RSS, a 3-screen SPA
(paste-in, backlog, episode), a single hardcoded account.

**Out — do not build these yet:** Gmail, X, OAuth, the secret store, smart episodes,
ranking, iOS, voice interactivity, signup, brand.

**Status: the Phase 1 path is built** — paste-in, dedup/integrate, assemble, script +
grounding validation, TTS, object storage, the private feed, and the three SPA screens. The
stages run as Cloud Run jobs draining Postgres queues (`workers/`), and every one of them
is retried independently.

**Deployed as of 2026-08-25, and still unproven — those are different claims.** Both
environments now serve the real image: `/internal/health` answers `motet-api` with
`inference_mode: real`, `authenticated: true`, and telemetry exporting, and the served
OpenAPI document lists the Motet routes. Until 2026-08-24 that was not so — every Cloud Run
service returned Google's `hello` sample, because the infrastructure was stood up in
`bootstrap` mode and no Motet image had ever been built.

**What has not happened is a real vendor call** — not one OpenRouter completion, not one
second of Cartesia audio — so everything downstream of the fakes is still unproven, and
being deployed does not change that. The image pin lags this repo's `main` by however long
the last bump was ago: a route merged here is not a route serving there, and
`/internal/health` plus the served OpenAPI document are how you tell. Pushing the image and
the runtime environment the services get are tracked in the private infrastructure repo.

**Phase 1's real deliverable is the factory, not the feature.** The question it answers is
*"does the factory work?"* — not *"is the briefing good?"*. That is what the scaffolding in
this repo is: the one CI command, the OpenAPI contract, the fake adapters, the golden set.

RSS rather than an in-app player is deliberate: it buys background audio, offline,
lockscreen, CarPlay, and speed control with zero iOS code.

---

## Phase 2 — the credential-independent backend

**Status: built, and dormant where a credential is missing.** Gmail ingestion, the
credential vault, smart episodes, highlights, show notes and subtitles, and read state from
the audio side. Two paths are written, typed, and covered against fakes but have never
executed against a vendor, because the vendor does not exist yet:

| Dormant path | Waiting on | Turning it on |
|---|---|---|
| Gmail ingestion | a Google OAuth client (a **one-time human-owned** step, invariant 9) | `GOOGLE_OAUTH_CLIENT_ID` + `GOOGLE_OAUTH_CLIENT_SECRET`, and `MOTET_INFERENCE_MODE=real` |
| KMS-backed credentials | nothing — the keyring is **provisioned**, and the deployed API resolves the kms backend | `MOTET_VAULT_BACKEND=kms` + `MOTET_VAULT_KMS_KEY`, both set by the service definition |

Both are **configuration changes, not refactors** — that is the property the seams exist to
buy, and the thing to preserve.

Consent itself is started from the SPA's **Sources** screen, which is the only thing in the
system that calls `/v1/sources/connect`. Granting a mailbox is invariant 9's human-owned
half — a person has to look at Google's consent page and say yes — so the screen exists to
put that click somewhere a human can reach, and everything after it is automatic.

**`MOTET_INFERENCE_MODE` governs Gmail too.** Gmail is a vendor, and "may this process talk
to a vendor" is one question with one answer. A second variable would reintroduce the
exact silent-disagreement failure the mode module already documents, in a worse form: a
process could poll a real mailbox and dedup it with a fake model.

**The vault is two Protocols, not one.** `DekWrapper` wraps; `KeyManager` also unwraps.
Invariant 8 says only workers may decrypt, and Cloud KMS distinguishes `useToEncrypt` from
`useToDecrypt` — so the API holds the wrapper and cannot ask for plaintext, because the
method does not exist on what it holds. **The IAM grant is the actual control**; the split
is what stops a well-meaning refactor from quietly needing it widened.

**Highlights anchor to the source span, and nothing else.** A claim id is not stable — the
script stage deletes and rewrites every claim on retry — and an audio offset moves on every
re-render and means nothing on the visual surface. `source_items.text` is the one immutable
thing in the pipeline and is already what every claim cites, so a highlight anchored there
survives re-scripting, re-rendering, and dedup merges, and means the same thing whether it
was saved by voice or by tapping the transcript. `episode_id` and `anchor_ms` are recorded
as **provenance, not the anchor**.

**A highlight's quote is read out of the source item, never taken from the caller.** In the
voice case the caller is a model; one that quoted loosely would otherwise write its own
paraphrase into the user's highlights, where it would look verbatim.

**Smart and manual episodes go through one selector.** Manual *is* the rule with every
default left alone (unread, no window, oldest first). Two selection paths would eventually
disagree about what "unread" means, and invariant 5 is precisely the rule that one fact
must not have two definitions. Rankings are deterministic and model-free — age, or how many
independent sources covered a story. Ranking with a model is Phase 3 and would put an LLM
call into a stage that currently cannot fail.

**A rule is stored as a snapshot on the episode**, not referenced from a rule table. An
episode is a historical artifact, and "why does this contain these stories" has to stay
answerable after the rule is edited.

**Read state from the audio side is `episodes.listened_through_ms`.** It is monotonic in the
repository layer — a client that seeks backwards is reviewing, not un-listening — and its
only job is deciding which news items are read, so listening past a story on a walk and
ticking it off on the backlog screen stay one fact. Deliberately **not** named
`spoken_through_ms`: that belongs to the voice session contract, which is a different
session's work, and the voice service should call this same repository function rather than
growing a second column.

**Claim timings are apportioned, not measured.** Narration is synthesized per *segment*, so
segment boundaries are exact and claims within a segment are proportioned by length. Going
per-claim would give exact timings at the cost of three to four times the request count and
a hard prosody break at every sentence, for an error well inside what a caption cue needs.
If word-level timing is ever needed, the upgrade is Cartesia's own timestamp output rather
than more calls.

**Out, and still out:** X bookmarks (verify the API tier first — Tadas's spend decision),
the voice/interaction path, and the iOS app.

---

## Architecture

| Component | Runtime | Directory |
|---|---|---|
| API | FastAPI, Cloud Run | `api/` |
| Ingestion workers | Cloud Run jobs | `workers/` |
| Inference adapters | library | `inference/` |
| Ingestion sources | library | `sources/` |
| Credential vault | library | `vault/` |
| Telemetry wiring | library | `obs/` |
| Schema + migrations | library | `db/` |
| Object storage | library | `storage/` |
| Web SPA | Vite + React, static files on Cloud Run | `web/` |
| Voice service | Pipecat, Cloud Run — **Phase 2** | `voice/` |
| iOS app | Swift — **Phase 2** | `ios/` |
| Golden set | CI harness | `goldens/` |

**Storage.** Postgres on Cloud SQL for data *and* the job queue (`SKIP LOCKED`). Audio in
GCS behind signed URLs. No Redis. No vector store. (See tripwires.)

**Inference.** Claude for dedup/integrate, script generation, and grounding validation,
reached **through OpenRouter** and defaulting to Claude Sonnet 5; Cartesia Sonic for TTS.
OpenAI Realtime (voice) and Exa (research) arrive in Phase 2. Every one of them sits behind
an interface with a fake — invariant 7.

**Two voices on purpose:** Sonic narrates, the realtime model converses. That decouples
voice identity from the realtime vendor.

**Two audio paths, deliberately separate.** Narration is batch and offline-capable
(script → validation → TTS → GCS → client plays locally). Interaction is realtime and
online-only (barge-in → Pipecat → realtime provider → tools → resume narration). Realtime is
10–15% of session minutes, not 100%. This split is what makes offline possible, grounding
enforceable, and the economics work.

---

## CI — one command

There is exactly one entry point:

```bash
bin/ci
```

It runs migrations, tests, and typecheck for both the Python and TypeScript halves, plus
the contract and golden-set gates. **If you add a check, add it to `bin/ci`** — a check
that only exists in the workflow is a check that cannot be run locally, and it will rot.

`bin/ci` needs a Postgres to run migrations against; see `CONTRIBUTING.md`.

**Every pytest run creates and drops its own database**, and that is load-bearing rather
than tidiness. The `db` fixture truncates every table before each test, which is only a
private act if the run owns the database — and `DATABASE_URL` names *one*, so two runs on
a machine (two agent sessions, two terminals, a local run beside a CI job) used to truncate
each other's tables mid-test. Postgres reported it as a deadlock and killed one run; the
survivor then failed somewhere unrelated, on a row that had been written and was gone. That
is motet#15, and the reason it looked like a leaked connection is that within one process
the suite is serial and there is no second writer. `conftest.py` rewrites `DATABASE_URL` in
`pytest_configure` — before collection, because test modules read it at import — so
subprocesses and anything reading the environment get the same isolated database. Making
the truncate gentler (retry it, `DELETE` instead) would have left both runs deleting each
other's rows, quietly.

Two other scripts sit outside it, each for the same one reason — it needs a toolchain
`bin/ci` deliberately does not require:

```bash
bin/build-images     # needs a Docker daemon
ios/bin/build-app    # needs Xcode
```

`bin/build-images` builds and smoke-tests the three container images, and it is its own
script because it needs a **Docker daemon** — `bin/ci` needs only Postgres, and a laptop
without Docker must still be able to run every check in it. It is still a script rather
than YAML, for the same reason `bin/ci` is. CI runs it as a second job.

`ios/bin/build-app` is the same argument with a different toolchain: `xcodebuild` exists on
no machine in this project except the GitHub-hosted macOS runner the `ios` job uses, so
calling it from `bin/ci` would turn every Linux run red. It skips on a Mac without Xcode
and **fails when `CI` is set** — the same shape as `ios/bin/ci-swift`, and for the same
reason: a green run that compiled nothing is worse than a red one.

### The container images

Cloud Run runs three: `motet-api`, `motet-worker`, `motet-web`. The first two are the same
tree — one root `Dockerfile` with two targets, because a second Dockerfile would be a
second copy of one dependency graph. The SPA is `web/Dockerfile`.

```bash
bin/build-images              # all three, then smoke-test each
bin/build-images api web      # a subset
```

**Both build contexts are the repo root**: `uv.lock` describes the whole workspace, so a
context rooted at `api/` could not resolve it.

**This repo builds images and never pushes them.** It is public and holds no cloud
credential of any kind — no GCP identity, no registry login, nothing to leak. Publishing
and deploying belong to the private infrastructure repo. A PR that adds a push step here
is a PR that adds a cloud credential to a public repo; the answer is the other repo.

**The API origin is not in the SPA bundle.** Vite inlines `import.meta.env` at build time,
so a compiled-in hostname would mean one image per environment. `web/` ships a `config.js`
that the container entrypoint rewrites from `MOTET_API_BASE_URL` at start-up, and the
client reads it at call time. One image, configured where it runs.

### Runner policy

CI runs on the shared self-hosted runner pool (`runs-on: self-hosted`), the same pool
`tadasant/zimmer` uses, to stay off the GitHub-hosted Actions minute quota.

This repo is **public**, and a self-hosted runner on a public repo is exactly the
combination GitHub warns about: without a guard, any stranger's fork PR would execute
arbitrary code on shared infrastructure. Three mechanisms close that gap, and **all three
have to stay in place** — removing any one of them reopens it:

1. **A fork guard on every job that checks out code.**
   `if: ${{ github.event_name == 'push' || github.event.pull_request.head.repo.full_name == github.repository }}`
   A PR from a fork skips every job, so fork code never runs on the runner. Branches pushed
   to this repo itself run everything.
2. **A no-checkout `all-checks-pass` gate**, so a fork PR — where every job skipped — still
   reports green instead of hanging forever on a required check.
3. **`pr-auto-close.yml`**, which closes outside PRs on `ubuntu-latest` with no checkout at
   all, so the untrusted path never touches the self-hosted pool even to be rejected.

Belt-and-braces on top: the repo's Actions fork-PR approval policy is set to
`all_external_contributors`, so a fork workflow needs a maintainer's click before it could
run even if a guard were dropped.

**If you add a job to `ci.yml`, it needs the fork guard and an entry in
`all-checks-pass`.** A job without the guard is the whole hole.

**One job is deliberately not on that pool: `ios`, which runs `xcodebuild` on GitHub-hosted
`macos-latest`.** There is no Mac in the pool, and there is no Mac anywhere in this
project — which is why the iOS app went months without a compiler ever being pointed at
it. It is free because this repo is public, and it needs no Apple Developer Program
credential because a **simulator** build needs no identity, no certificate, no provisioning
profile and no App Store Connect key. That is the property to preserve: adding signing, a
TestFlight upload, or a `CODE_SIGN_ENTITLEMENTS` pointing at
`ios/App/Motet/Motet.entitlements` would put a credential and a human back into a job that
currently needs neither — and the entitlement it asks for
(`com.apple.developer.carplay-audio`) is granted by Apple's manual review, so wiring it in
before the grant arrives makes the build fail *to sign* rather than merely lack CarPlay.

It carries the same fork guard as everything else. A hosted runner is ephemeral, so a fork
PR reaching it would not be the shared-machine problem the guard exists for — but there is
no reason for a fork to run it either.

**A macOS runner bills at a higher multiplier, so `ios-changes` decides whether it starts.**
It is a no-checkout job that asks the API which files moved and answers one question: did
anything under `ios/**` change? A job rather than an `on: paths:` filter, because `paths`
is workflow-wide and `all-checks-pass` has to keep aggregating exactly one workflow — and a
skipped job is already a first-class outcome for that gate, so this reuses the existing
design rather than working around it.

Deploy workflows are a different matter — they live in the private repo.

---

## Contracts and seams

### OpenAPI is the seam between the API and the SPA

`openapi.yaml` is **generated from the FastAPI app** and committed. The TypeScript client in
`web/src/api/schema.gen.ts` is generated from that YAML. CI regenerates both and fails on
any diff, so the three can never drift.

Never hand-edit `openapi.yaml` or `schema.gen.ts`. Change the FastAPI route or model, then:

```bash
bin/generate-openapi   # app  -> openapi.yaml
bin/generate-client    # yaml -> web/src/api/schema.gen.ts
```

### Inference stages are the seam to the vendors

Each stage in `inference/` is a `Protocol` with (a) a deterministic fake and (b) a real
adapter. `inference.registry` picks between them from `MOTET_INFERENCE_MODE`, which is
`fake` everywhere except staging and production. Invariant 7 is why: a test that calls a
real model is slow, nondeterministic, and expensive, and it stops being a test.

### OpenRouter is the seam to the LLM, and the model is config

`inference/src/motet_inference/llm/` holds one provider-agnostic interface (`LlmClient`),
one real adapter (OpenRouter), and one deterministic fake. **Stages never name a vendor** —
they call `build_client()` and `build_request(stage, ...)`, and the model comes back already
chosen. `MOTET_INFERENCE_MODE=fake` therefore guarantees no test can spend money, exactly as
it does for the stage registry.

**The default is `anthropic/claude-sonnet-5`, and switching is a variable, not a commit.**
`MOTET_LLM_MODEL` moves every stage; `MOTET_LLM_MODEL_{DEDUP,SCRIPT,GROUNDING,VOICE}` moves
one. Effort works the same way, defaulting per stage: dedup `low` (the volume line), script
`high`, grounding `max`, voice `off`.

**A "stage" is a caller with its own cost profile, not a step in the pipeline**, which is
what lets the voice service's conversational turn be one of them (motet#6). It used to
resolve its own slug from a `MOTET_VOICE_LLM_MODEL` of the voice module's own, and the cost
of that was not the duplication — it was that the *one* text call in the system a person
waits on in real time was also the one whose slug nothing checked against the catalogue
until a vendor rejected it mid-turn. Voice defaults to `off` rather than to an effort
because a second of thinking there is a second of silence; that is a default, so
`MOTET_LLM_EFFORT_VOICE` still turns it on. `MOTET_VOICE_LLM_MODEL` is gone rather than
aliased — nothing set it, and an alias resolved outside `load_config` would have kept
exactly the bypass the change is for.

Four things about this are settled, and each exists because of a specific failure:

- **An unknown slug or a missing key is a startup crash**, not a 500 an hour later.
  `validate_startup()` runs in the API's lifespan and in the worker entry point. Slugs are
  checked against a committed catalogue; `bin/check-openrouter-models` verifies that
  catalogue against OpenRouter's live list. That script is deliberately **not** in `bin/ci`,
  because CI is offline (invariant 7) — run it by hand when adding a model.
- **Reasoning can be dropped silently — on the models where effort is a budget.**
  Anthropic's own API rejects an incompatible thinking config with a 400; OpenRouter drops
  the field and answers anyway. A response with no evidence of reasoning is logged and, by
  default, raised on. Never "fix" a `ReasoningNotAppliedError` by switching the check off —
  it is reporting that a stage ran without thinking.

  **The exception is adaptive thinking, and it is a fact about the model rather than a
  preference (motet#31).** From Claude 4.6 onward — which is every Anthropic slug in the
  catalogue — `reasoning.effort` sets Anthropic's `output_config.effort` and never a
  thinking budget, and Claude decides per response whether the task is worth thinking
  about. So no reasoning in a response is the model obeying `effort='low'` and identifies
  nothing, while the guard's false positives each cost a completion that was billed and
  then discarded. `ModelSpec.adaptive_thinking` records which side of that split a slug is
  on and `build_request` reads it, so the guard stays loud on a budget-based model
  (`openai/gpt-5.1` is the one such row) and does not run on an adaptive one. It fired 21
  times on the first real staging run against no fault it could have distinguished, and
  stopped every pasted item entering the pipeline. **This is a scoping, not an off
  switch:** `Reasoning(require_evidence=False)` is still not the way to make one go away,
  `reasoning_applied` still rides on every response, and an unlisted model is `"unknown"`
  rather than either answer — not raised on, but logged as the open question it is.

  **"Reasoning is on by default" is a second, narrower fact, and conflating the two is the
  mistake to avoid** — the first draft of this fix made it. `reasoning.default_enabled` is
  true for Sonnet 5 and Opus 5, **false for Opus 4.8 and absent for Sonnet 4.6**, all four
  of which think adaptively. Where it is true the argument gets stronger rather than
  merely holding: a dropped field would leave thinking on at `high` rather than off, so an
  unthought answer cannot be a dropped config even in principle — and that is the pair the
  guard actually fired on. Where it is false, an unthought answer is *ambiguous* between
  the two causes, which is reason enough not to raise but is not the same claim.
  `ModelSpec.reasoning_on_by_default` keeps them apart and
  `bin/check-openrouter-models` drift-checks it; `adaptive_thinking` is the one catalogue
  fact nothing can verify, because the live list says which efforts a slug takes and never
  what an effort *does* to it.

  Two consequences worth not rediscovering. **Raising dedup's effort would not have fixed
  it** — thinking is adaptive at every level, so a higher effort makes an unthought answer
  less likely rather than impossible, which trades a deterministic failure for a flaky one
  and pays the retry ladder for it. And **omitting the `reasoning` field is not how you
  turn reasoning off**: on a model where it is on by default, sending nothing buys adaptive
  thinking at `high`, the most expensive setting there is. `MOTET_LLM_EFFORT_<STAGE>=off`
  therefore travels as an explicit `{"enabled": false}`.

  **One competing explanation is not excluded and should not be written down as closed.**
  Sonnet 5 returns no raw chain of thought, so `usage.reasoning_tokens` is the only signal
  the check has — and OpenRouter routes a slug across several upstreams without pinning
  one. "Dedup's worker process stuck to an upstream that does not surface reasoning-token
  accounting, while script and grounding stuck to ones that do" fits every observation
  just as well, and would mean a thought answer whose accounting was lost. It does not
  change the fix, because the check cannot tell the two apart either way. The adapter
  therefore logs the **served upstream** alongside the model whenever a response arrives
  unthought, so a real run can settle it.
- **Prompt caching is the largest LLM cost lever**, because dedup passes the whole news-item
  window in-prompt once per source item. Put the breakpoint on the last *stable* part and
  check `usage.cache_read_tokens`. Never assume a hit.
- **No sampling parameters, ever.** Sonnet 5 rejects `temperature`/`top_p`/`top_k` and
  `budget_tokens`. The request type has no field for any of them; keep it that way.

Two smaller rules that fall out of the same thinking:

- **`MOTET_INFERENCE_MODE` is parsed in exactly one place** — `motet_inference.mode`. Both
  the stage registry and the LLM seam ask it. Two readings can disagree, and the
  disagreement is silent: `MOTET_INFERENCE_MODE=Real` would mean real stages wired to a
  fake model, which boots clean and emits fabricated text.
- **The API validates LLM *config* at startup but does not resolve the key.** Workers call
  `validate_startup()`; the API calls `load_config()`. Phase 1 runs all inference in
  workers, so mounting the one vendor secret into the internet-facing service buys nothing
  and widens the blast radius. When the API calls a model, that changes.

Credentials are one enum plus one resolver in `llm/credentials.py`, and that file is the
whole seam for a future "bring your Claude Max account" quota kind. **Keep it one file**,
and keep wire shapes out of it: an API key travels as `Authorization: Bearer` to OpenRouter
and as `x-api-key` to Anthropic direct, so headers belong to the adapter. A header in the
credential module forces a *provider* distinction onto the credential-*kind* axis, which is
what makes the second provider hard.

### Object storage is the seam to where audio lives

`storage/` holds one `ObjectStore` interface, a GCS backend with V4 signed URLs, and a
local filesystem backend that dev and CI run against — the same fake-by-default shape as
the inference seam, and `MOTET_STORAGE_BACKEND` defaults to `local` for the same reason
`MOTET_INFERENCE_MODE` defaults to `fake`.

**`signed_url()` returning `None` is part of the contract, not a failure.** It means "this
backend cannot hand out a direct link, serve the bytes yourself", and the API's audio route
branches on that rather than on a backend name. That is what keeps an RSS enclosure URL
identical across both backends — a podcast client cannot tell them apart — and what keeps a
third backend from ever touching the route.

**Enclosure URLs point at us, never at the bucket.** A signed URL's expiry inside a feed
document a client cached for six hours is a download that fails later for no visible
reason.

### A stage records what it spent and what it threw away

`inference/src/motet_inference/accounting.py`. Two issues (motet#24, motet#25) with one
shape: the work happened and the evidence was discarded. Usage was decoded off every
OpenRouter response and read by nobody; grounding drops were counted and never
characterised.

**A metric answers "how is the fleet doing", a log line answers "what did *that* one
cost", and the split is cardinality.** `motet.llm.tokens{stage,model,kind}` carries no
episode id, because a time series per episode is a time series per episode forever.
`collect_usage()` is the other half: a `ContextVar` ledger the worker handlers open around
a stage, so the one caller that *has* an id can put a total beside it. A `ContextVar`
rather than a parameter because a cost accumulator in the argument list would be a cost
accumulator in the `Protocol`, which every fake would then implement for a number it does
not have.

**Recording lives in the stage adapters, not in the OpenRouter client**, because *stage* is
what an operator splits cost by and `LlmRequest` deliberately does not carry one. The
consequence to remember: a run on the deterministic **stage** fakes calls no model and
therefore reports no cost, correctly — so a test that asserts cost has to run the real
adapters over `FakeLlmClient`, which is what `workers/tests/test_accounting.py` does.

**Every usage field is logged even at zero.** A field that vanishes when it is zero is a
field a log query cannot aggregate, and `cache_read=0` is precisely the observation the
prompt-caching warning above is about.

On the grounding half, **the two drop layers are separate instruments on purpose**.
`motet.script.claims_dropped{reason}` counts what the script parser could not use — a
claim counted there never reached the gate — and `motet.grounding.claims{outcome}` plus
`motet.grounding.claims_dropped{reason}` count what the gate refused. They mean opposite
things: the first is a script-prompt problem, the second is invariant 3 working. Reasons
are bucketed by `classify_grounding_reason` before they touch a label, because a model's
reason is a sentence and a sentence as a label mints a series per claim; the sentence
itself, the claim text and the episode id go in a warning line, which is the only moment
that detail exists — the pre-grounding script is never stored and a dropped claim leaves
no row anywhere.

**A clean episode says so.** "No drops today" and "grounding never ran" must not be the
same observation, which is the never-infer-"no errors"-from-"no data" trap one section up.

### The grounding gate is chunked, and its ceiling is a function of the work

`ClaudeGroundingValidator` used to judge every claim in an episode in **one** call under a
fixed 8,000-token ceiling. At nineteen news items — a normal Tuesday — the model spent all
8,000 tokens reasoning and returned no verdict at all, so `_parse` got an empty completion
and raised; the input never changed, so every retry did the same thing and the episode
never left `scripting`. That is motet#42, and it was invisible until then because every
earlier end-to-end run used two claims, where 8k is never approached.

**The diagnosis is not "the ceiling was too low", and that distinction is the fix.** The
*required output* of that call grows with the backlog and a constant does not, so every
constant is a backlog size beyond which the stage cannot complete. Raising it moves the
size; it does not remove it. So the bound moved onto the work instead: claims are chunked
(`GROUNDING_CLAIMS_PER_CALL`, plus a character bound, because eight paragraph-sized
evidence spans are not the same ask as eight short ones) and each call's ceiling is
`grounding_max_tokens(n)` — a flat term for reading the instructions plus a **per-claim**
term covering both the verdict and the thinking that produces it. Per-claim rather than
flat because that is what the staging numbers say: 8,000 reasoning tokens over twelve
claims, and *cut off*, so ≥660 a claim is a floor and the real figure is unknown. The
number of calls grows with the episode; the size of each one does not.

Three things fall out of that, and none of them is decoration:

- **Verdicts are independent per claim, which is what makes chunking free.** The original
  batching argument — do not multiply the most expensive stage in the pipeline by the
  length of an episode — survives intact. Chunk size is what trades calls against risk.
- **A chunk that still exhausts is halved, and a claim that exhausts on its own fails
  closed.** Both terms of the ceiling are estimates against one truncated observation, so
  the halving is the part that has to be right: it is what makes a wrong estimate cost a
  retry rather than an episode. The floor matters more than the ceiling — an unjudged claim
  is a *failure*, exactly as a missing verdict already was, so it is dropped rather than
  spoken. `handle_script` then ships what survived — an episode a story short instead of no
  episode at all, which is what motet#42 actually cost. It records itself as
  `motet.grounding.claims_dropped{reason="budget_exhausted"}`, the one drop reason that
  means nobody judged the claim rather than that the gate judged it.
- **Halving costs calls, so it is instrumented rather than merely logged.** A chunk that
  never fits costs up to `2n-1` calls at the most expensive effort in the system, and the
  claim-drop counter only fires at the *floor* — so without a second instrument "the chunk
  size no longer suits the model" would be invisible until claims started disappearing,
  which is the never-infer-"no errors"-from-"no data" trap one section up wearing a new
  hat. `motet.llm.budget_exhausted{stage,model}` counts every exhausted call, and the
  tokens it burned still land in `motet.llm.tokens`: billed and useless is still billed.
- **`LlmBudgetExhaustedError` is its own error because it is the one transport failure
  worth *not* retrying.** The same request spends the same budget every time, so the
  caller that can send less work should, and for a stage that *cannot* subdivide — dedup,
  script — the worker loop fails the job permanently rather than buying five identical
  billed failures. A truncated answer under a JSON schema raises it too: half a document
  parses no better than none, and the old path surfaced that as malformed JSON — pointing
  at the model's spelling rather than at the ceiling that cut it off. It is raised **only**
  on `finish_reason='length'`: an empty answer for any other reason is not deterministic,
  is worth its retry, and calling it a budget failure would tell the validator to send less
  work until it had dropped every claim in the chunk.

**The effort stays at `max`, and that is a decision rather than an oversight.** Grounding is
where invariant 3 lives; the failure was the *shape* of the request rather than the depth
of the thinking; and moving both at once would leave nobody able to say which one fixed it.
What has changed is that effort is now a free-standing cost lever with no correctness cliff
behind it — `MOTET_LLM_EFFORT_GROUNDING` moves it in configuration, and a real run at
realistic scale is the evidence that should decide it, not this file.

**What did not change is what the gate can see.** A claim is judged against its own quoted
span and nothing around it, so support one sentence outside the span reads as fabrication —
a claim citing 185 voter IDs was refused on staging while `185` sat in the same source item,
a paragraph away. Chunking moves which call a claim travels in and nothing about the
evidence that travels with it. That is motet#45, and it belongs behind the golden set.

### Ingestion state is a join onto the job queue, not a column

`GET /v1/ingestion` (`repo.list_ingestion`) is what stops content from silently
disappearing. It reports source items that are not in the backlog yet — pending, failed,
and for ten minutes after they succeed — each joined to its `integrate` job.

**The reason a failure is happening lives on the job row, and that is why this is a join.**
`source_items.last_error` is only written when the retries run out (`_record_failure` in
the runner), so an item that is *still being retried* carries no error of its own. A view
built from `source_items` alone therefore cannot tell "working on it" apart from "sitting
there" — which is the one distinction someone waiting actually cares about. Postgres being
the queue as well as the datastore is what makes that a join rather than a second system
to ask, and migration 0005's partial expression index on `payload ->> 'source_item_id'` is
what keeps it off a sequential scan of every job ever run.

**A polled message has no domain object for part of its life, and reporting only on the
domain object lost it entirely.** `handle_extract` writes the `source_items` row when
extraction *succeeds*, so between the poll and the parse the extract job row is the whole
record that the message was ever seen — and `handle_poll` advances the cursor in the same
transaction that queues the fetch, so nothing ever looks at that message again. A
newsletter that arrived, was polled, and then failed extraction five times was therefore
invisible on every surface the user has, which is the paste-in defect this route was built
for — motet#33's defect, one stage earlier (motet#35). So `list_ingestion` is two arms: a
source item joined to its `integrate` job, and an `extract` job that has produced no source
item. Migration 0008's partial expression index is 0005's, one queue over.

**Reading the job row is the fix; writing a stand-in `source_items` row is not**, and the
reason is that the earliest failures happen before there is anything to write one from. A
revoked grant fails in `_access_token`, before a single byte of the message has been
fetched — so "write the row first, from the raw bytes" cannot see the class of failure that
motivates this at all, and moving the write back to *poll* time would put a textless row
into the table that anchors every claim and every highlight, and would recast the
`(source_id, external_id)` index from "this message is ingested" to "this message was
seen". The job row already holds the attempt count, the schedule and the reason, which is
everything the surface reports.

**A message reported from a job is one line, never two, and it takes two exclusions to
mean that.** The first is on `(source_id, external_id)` and drops a job whose message
already has a source item — a `done` job in the ordinary case, a lease reclaimed after the
insert committed in the awkward one. The second keeps only the newest *open* job for a
message, because a message can genuinely have two: an expired provider cursor makes
`handle_poll` re-list a window, and a message whose earlier extraction *failed* has no
source item, so the pre-check that makes a re-poll idempotent does not fire. Reporting one
newsletter twice would be the accounting surface contradicting itself, which is motet#41's
shape one stage up. `source_kind` rides on both arms because it is
what decides the repair: a failed paste can be pasted again, and a failed mailbox message
cannot, because the cursor has moved past it. The SPA says so rather than offering a button
that does not exist.

**An unparseable message is still a deliberate skip, not a failure.** `handle_extract`
catches `ExtractionError` and records it on the source: a mailbox is mostly receipts and
calendar invites, and treating each one as an error would make the source permanently red.
What is now reported is everything that *raises* — the auth failure, the transport failure,
the vault that will not open — because those are the ones where content the user wanted was
lost.

Three smaller things are decisions rather than implementation:

- **`max_attempts` is reported, never restated.** It comes from
  `motet_workers.jobs.DEFAULT_MAX_ATTEMPTS`, so "attempt 3 of 5" counts to the number the
  queue is counting to. A second copy is wrong the moment one of them moves.
- **A succeeded item lingers for `INTEGRATED_GRACE` rather than vanishing.** It has a news
  item by then, so the row is redundant — but a paste that disappears from one list and
  reappears in another under a title dedup rewrote is not obviously the same paste.
- **`next_attempt_at` is gated on the source item being pending**, not only on the job
  being ready, so two rows disagreeing cannot produce "failed, and trying again in 30
  seconds".

In the SPA it is a panel above the backlog and a count on the tab — visible from the
*paste* screen, which is where somebody who has just pasted is. It polls only while
something is pending, and the fetch is **best-effort**: the backlog is the primary list and
must not go blank because the secondary one 404s.

### A queue nobody drains, and a UI that promised otherwise

`motet_workers.runner`, `worker_heartbeats`, `GET /v1/processing`. Two halves of motet#38,
and they answer different questions.

**A worker has to be able to just run.** The runner shipped as one process per queue,
draining once and exiting — which is exactly what a Cloud Run *job* wants, and a job has to
be *started*. The only thing that started one was a `workflow_dispatch` in the private
infrastructure repo, so the product worked for somebody holding a CI credential and for
nobody else. `runner all --poll-seconds N` is the other shape: one process, sweeping
`queues.PIPELINE` in order, so a paste integrates and an episode assembles, scripts and
renders on one pass rather than one stage per poll interval. Both shapes stay, because both
are real deployments — and SIGTERM now stops the loop rather than killing the process,
since a long-lived worker is the thing Cloud Run signals on every deploy and the obs flush
lives in the `finally`.

**The stages and the object store are built once for the process and passed into `drain`,
and that is correctness rather than tidiness.** `real_stages()` mints a fresh `LlmClient`
on every call; OpenRouter's sticky upstream routing is *per client*, and that routing is
what keeps the dedup prompt cache warm. Resolving them inside `drain` is right for a job
that drains once and exits, and would throw the cache away six times a sweep in a poll
loop. They stay optional arguments so a one-shot drain, and every test, needs to know
none of it.

**Turning it on in a deployment is configuration, not code**, and it is deliberately still
the private repo's call: an always-on worker against `MOTET_INFERENCE_MODE=real` is a
standing authorization to spend money at OpenRouter and Cartesia, which is a decision about
an environment rather than about an application.

**Which is precisely why the SPA must not assume.** It used to state, of every queued item,
that "a worker takes it off the queue within a few seconds" — as a fact, with nothing behind
it. **A queued item looks identical whether a worker is chewing through a backlog or
whether none has ever run**, so the failure was silent and read as slowness. That is the
never-infer-"no errors"-from-"no data" trap wearing the queue's clothes.

So a drain writes one `worker_heartbeats` row per queue **at the top of every pass, whether
or not it finds work** — an empty pass is what proves a worker is alive — and
`/v1/processing` reports it. The panel's copy is a function of that: a worker is running, no
worker has run recently, or *the question could not be asked*. Three, not two: a 404 from an
older API is an outage in the panel, not an idle pipeline, and must not produce the same
sentence. **Deriving it from the item's age instead would be the same mistake with more
arithmetic** — age says how long something has waited, never whether anything is coming for
it.

Two smaller things follow. The age is shown *as well*, because once a stall clears the thing
worth knowing is which item has been waiting twenty minutes. And the episode screen carries
the same split, because "Working… this page polls" is the identical promise one stage later
and several vendor calls more expensive.

### Two news items with one headline is dedup contradicting itself

`motet_workers.handlers._merge_target`. Three write-ups of one story were pasted; dedup
merged two and returned the third as a *new* news item under a byte-identical headline
(motet#40, motet#41). The backlog listed the same sentence twice and an episode would have
read the story out twice under one heading — the failure dedup exists to prevent, and the
one that is most obvious in audio.

**A "new story" whose normalized title an *unread* item in the window already carries is
merged into it instead**, in the handler rather than in the adapter, so it holds for any `Integrator` and is testable
without a model. Normalization is case and runs of whitespace and nothing else: fuzzy
matching here would be a similarity threshold of its own, in the one place meant to have
no opinion. An empty title matches nothing — two items that both failed to get one are not
evidence of anything.

**This is a backstop and not the fix, and the difference is the thing to keep.** Why the
threshold missed on genuinely independent prose about one event is a question about the
dedup prompt and window, and it is still open. What is not a judgement call is the narrow
case here: dedup *writes* the titles, so two items carrying the same one is one stage
disagreeing with itself.

**The backstop is scoped to *unread* twins, and that bound is what keeps its cost argument
true.** The window also carries recently-read items, and folding a fresh story into one the
listener has already heard means assembly never speaks it: a log line is its only trace,
and a re-paste hits the same rule rather than undoing it. The model-driven merge may still
do that and always could — it is what the window is *for*, and there it is a judgement
about two texts. A string match is not that judgement, so it does not get that reach.

### A stage's "already done" guard is bounded by the state it writes, not by the last one

`motet_workers.handlers.SCRIPTED_STATES`. Every handler short-circuits an episode it has
nothing left to do for, and `handle_script`'s guard read `state is ready` — the *last*
state in the pipeline rather than the one the handler itself writes. So an episode in
`rendering` fell straight through it and the whole stage ran again: another billed script
completion, another grounding pass at `effort='max'`, a `replace_segments` racing whatever
TTS was reading, and a second TTS job for an episode that already had one. That is
motet#50, and it is the module docstring's own idempotence contract being broken by the
one handler most expensive to re-run.

**The re-run is not a bug in the queue — it is the queue working.** `_execute` commits the
handler's work and `jobs.complete` in two transactions on purpose (squashing them would
roll the attempt counter back with the work, and a poison job would retry forever), so a
worker that dies between them leaves the row `running` with the work durably applied, and
`STALE_LEASE_SECONDS` makes it claimable again. **Reclaim is the recovery every stage
depends on; converging on the same state is the handler's half of that bargain.**

**The boundary is "at or past the state this handler writes", and the two states left out
are the decision.** `pending` is *before* the stage — a script job on a pending episode
means assembly never ran, and the `PermanentFailure` it raises is the right, loud answer;
widening the guard to `state is not scripting`, the literal shape `handle_assemble` uses,
would have swallowed that into a silent `return`. `failed` is not past the stage either,
and short-circuiting it would strand an episode that genuinely needs re-scripting with no
TTS job and nothing alerting on it — the quiet direction of the same bug, and the one a
green test suite would never show.

**A state check cannot tell a stale job from a deliberate retry, and that is the residue.**
A stale `script` row can outlive its own episode's failure and replay a `failed` episode
through the full stage, clearing the `last_error` on the way (motet#55); a *slow* script
job reclaimed while the first worker is still running it produces two full renders, and
there both workers read the episode in `scripting` (motet#53). Neither is closed by a
state check, and neither should be papered over with a wider one — they want a fence on
the job, or a lease that heartbeats.

### The episode tab reflects server state, not this page's lifetime

`web/src/App.tsx`. Nothing loaded episode state on mount, so a reload — the realistic thing
to do while a multi-minute pipeline runs — emptied the tab and left a finished episode
reachable only through the RSS feed (motet#44). **The shape of that bug is that the longer
an episode takes, the more likely it is to be lost**, and the first one is the slowest
because the backlog is fullest.

`GET /v1/episodes` is loaded once the app has a way in, and it **seeds** rather than
assigns: `current ?? list[0]`, so a tab already opened from the backlog is not dragged back
to the newest episode, and the three-second backlog poll does not do it either. The list is
kept as well as the newest item, because "make an episode" is the only other way into this
screen and it always makes a *new* one — one loaded episode would leave yesterday's just as
unreachable.

### The RSS feed is the seam to the ears, and podcast clients are stricter than the spec

`api/src/motet_api/feed.py`. RSS is Phase 1's listening surface *instead of* an in-app
player, because a browser has no background audio and no offline and a dog walk needs both.

**Validate the feed by parsing it with a real client's parser, not by asserting on XML you
wrote.** `podcastparser` is the parser inside gPodder; `feedparser` is what most other
tooling uses. Both run in `bin/ci`. This is not belt-and-braces — it caught the feed
declaring the iTunes namespace as `.../podcast-1.0/` rather than `.../podcast-1.0.dtd`,
where the document parsed perfectly and every `itunes:` element was silently ignored. No
error anywhere; the episode simply had no duration on a lockscreen.

The feed token is a **bearer secret in a URL, deliberately** — clients handle that far
better than HTTP auth — and it is stored in the clear because the owner has to be able to
read it back onto a new device. Hashing it would make every device change a rotation, and a
rotation unsubscribes every client already using the URL.

### Gmail is the seam to the mailbox, and the extractor is where it earns its keep

`sources/` holds one `MailClient` Protocol, one `OAuthClient` Protocol, a real Gmail
adapter, and deterministic fakes — the same shape as the inference seam, reading the same
`MOTET_INFERENCE_MODE`.

**The interface is deliberately smaller than Gmail's API**: list what arrived since a
cursor, and fetch one message's raw RFC 822 bytes. A narrow interface is what makes the fake
honest; a fake that had to model Gmail's history API would be a worse Gmail rather than a
better test.

**Fetching returns raw bytes, not a parsed message.** Parsing is
`motet_sources.extract`, and it runs identically on real and fake input — which is what
makes the newsletter-sludge handling testable before a credential exists. That module is
the part of this path that can actually be *wrong*, and it fails quietly: a hidden
preheader read aloud as the opening sentence, an unsubscribe footer becoming a claim, a
control character where an em dash belongs travelling into TTS.

Two things learned there that are worth not rediscovering:

- **A footer is a block, so cut at the FIRST marker in the tail, not the last.** Cutting at
  the last one keeps most of the footer. The cut is bounded to the tail because some
  templates put a compact "unsubscribe" in the masthead, and cutting there reduces a
  newsletter to its header — which looks ingested-and-empty rather than failed.
- **A sender that declares `iso-8859-1` has emitted windows-1252.** The em dash and curly
  quotes a writer typed live in 0x80–0x9F, which is a *control* block in latin-1. Decoding
  as declared puts a control character in a news item title, an RSS document, and a
  text-to-speech request. Browsers have mandated this same substitution since HTML5.

**`/oauth/callback` is the SPA's one and only path, and it is not a router.** Google hands
consent back by navigating to a URL, so the browser arrives with a fresh page load and no
memory of the app it left. `web/src/oauth.ts` reads `window.location` once at boot and
`App.tsx` renders the callback instead of the tab strip — a few lines, against a routing
dependency that would then be available for every future "shouldn't this be a route?".
Three registered redirect URIs, one per environment, are each that environment's own
origin plus that path; **the path is the part that must not drift**, because the
registrations live in the private repo and nothing in this one can tell you it broke.
Google matches the string exactly, so in dev the app has to be reached at `localhost` and
not `127.0.0.1`.

Two things there are load-bearing rather than defensive. The **authorization code is
exchanged exactly once** — StrictMode double-invokes effects, the API consumes the state
row with a `DELETE ... RETURNING`, and a second exchange would overwrite a success with
"already used"; the URL is cleared for the same reason, so that a reload cannot replay a
spent code. And **`error=access_denied` is an answer, not a failure** — someone pressed
Cancel, which is a supported response to being asked for a mailbox, and it must not read
like a crash.

### Signing in is a second key to the same lock, not a user system

`api/src/motet_api/auth/`, and the `auth_sessions` table. Tadas asked for it twice: he did
not want to type `MOTET_API_TOKEN` into the deployed SPA's Settings screen any more.

**Nothing about "one account" changed.** Signup is still out, multi-user identity is still
Phase 3, and `auth_sessions.user_id` references the single `motet-owner` row seeded in
migration 0002 — there is a test that asserts exactly that, and if it ever fails somebody
has built the user system this file still says is out of scope. What changed is only how a
*browser* proves it may talk to `/v1`.

Four things about it are load-bearing:

- **The allowlist is the security control; Google is not.** This deployment's consent
  screen is published and **unverified**, so anyone on the internet with a Google account
  can complete the flow. A naive "Sign in with Google" would therefore be strictly *worse*
  than the shared secret it replaces — an open door where there was a lock. So
  `MOTET_ALLOWED_EMAILS` is checked server-side, after the ID token verifies, and **unset
  means deny everybody**. `/internal/health` reports `login_configured` for the same reason
  it reports `authenticated`: a login that denies silently looks exactly like one nobody
  has tried.
- **It is checked on every request, not once at the door**, and a session whose address
  has left the list is *deleted* rather than refused. Checked only at sign-in, taking
  somebody off the list would revoke nothing for the rest of a thirty-day session — and
  there would be no lever to do it with, because `/v1/auth/logout` needs the very token
  being revoked and invariant 10 says nobody has a shell to run a `DELETE` from. For the
  same reason there is `/v1/auth/logout-all`, which takes the shared API token too: the
  answer to a lost phone has to be reachable from a *different* device.
- **The ID token is verified, not read.** Signature against Google's JWKS over RS256 only,
  `aud` equal to our client id, `iss` Google, `exp`/`iat` inside a minute of leeway, the
  `nonce` we stored for that authorization, and `email_verified` true. An email claim out
  of an unverified token is a string somebody typed; authorizing on one would hand the
  whole API to anyone who put an allowlisted address on their own Google account. `PyJWT`
  does the cryptography — this is the code that must not be hand-rolled, because a bug in
  it authenticates the attacker instead of crashing.
- **A session is a bearer token in the same slot, not a cookie.** The SPA and the API are
  different origins, so a cookie would need `SameSite=None; Secure`, `allow_credentials`
  on the CORS policy, and a CSRF story to go with it — three moving parts to reach a place
  the existing `Authorization: Bearer` header already reaches. Signing in puts a session
  token where the API token went, so **no call site in `client.ts` knows the difference**,
  and the CORS policy is untouched and still does not allow credentials. The trade is that
  the token sits in `localStorage` rather than in an `HttpOnly` cookie — which is exactly
  where the shared secret already sat, except this one expires and can be revoked.
- **`MOTET_API_TOKEN` still works, everywhere it worked before.** The RSS feed, the iOS
  app, any script. It stopped being something a *human types into a browser*; it did not
  stop being accepted.

Sessions are **rows, not signed tokens**, and only their SHA-256 is stored. Rows are what
make logout actually revoke — a self-contained token stays valid until it expires however
loudly a client throws it away — and they mean a deployment needs no session signing key to
provision, rotate, or leak. Nothing ever reads the token back, so nothing keeps it. (The
feed token is the deliberate opposite, and its section says why.)

**Both flows come back on the one `/oauth/callback` path, and `state` is what tells them
apart.** Signing in and connecting a mailbox are two authorizations against the *same*
Google OAuth client — reusing it was the point, since a second client is a one-time
human-owned provisioning step (invariant 9) for no gain. But they finish at different API
routes and each spends a single-use `state` doing it, so a callback sent to the wrong one
burns the authorization and the user starts again for no visible reason. `state` is the
only value guaranteed to survive a round trip through the provider, so the flow is encoded
in it: sign-in states carry a `login.` prefix, and the dot is a safe marker because
`secrets.token_urlsafe` emits only `[A-Za-z0-9_-]`. Keep `LOGIN_STATE_PREFIX` in
`motet_api.auth.registry` and in `web/src/oauth.ts` in step.

Sign-in asks for `openid email profile` and sends **neither** `access_type=offline` nor
`prompt=consent` — those exist so a *mailbox* grant issues a refresh token and survives,
and re-prompting on every sign-in would be friction with no security value. That is why
identity is its own seam (`motet_api.auth`) rather than another caller of
`motet_sources`' `OAuthClient`: one class serving two sets of parameters is how the two
quietly become one wrong set.

**An agent cannot sign in, and that is settled rather than untried.** Google refuses an
automated browser at the *identifier* step — before a password is ever requested — with
"this browser or app may not be secure", both headless and with the usual fingerprint
masking. The consequence worth writing down is the one that is easy to forget when a test
run goes green: **an agent's green run says nothing about whether a human can sign in**,
because it exercises no part of the consent screen, the redirect-URI registration, or the
ID-token verification. A human clicks the real button once per environment after any change
to `motet_api.auth` or `web/src/oauth.ts`.
[`docs/testing-staging.md`](docs/testing-staging.md) is the runbook, the evidence, and the
list of what it does not cover.

**So the staging deploy mints an agent a session instead — variant A of
[tadasant-internal#1620](https://github.com/tadasant/tadasant-internal/issues/1620),
approved 2026-08-25.** The alternative was to copy staging's `MOTET_API_TOKEN` into the
estate's shared secret store, and that was declined: it documents a routine human step as
the procedure, which is the failure mode invariant 9 names, and it parks a non-expiring
owner-equivalent credential in a second durable store. `motet_db.mint_session` is a job
entry point — never a route, never reachable from the API — that writes one `auth_sessions`
row from a **digest** handed to it as an argument, refusing unless
`MOTET_STAGING_SESSION_MINT=1`, unless the address is on `MOTET_ALLOWED_EMAILS`, and unless
the TTL is inside a day. The plaintext is generated in the deploy workflow's shell and comes
back to the requesting agent encrypted to a key that agent generated, so it exists in no
log, no Actions output, and no job-execution record.

Three things about it are the decision rather than the implementation:

- **No API change, and that is the whole reason it is cheap.** `require_caller` already
  accepted a session token in the `Authorization: Bearer` slot. The mint adds a second
  *writer* of one table, not a second way to authenticate. A `POST /v1/auth/staging/session`
  route — the redeemable-token variant — was declined for exactly this: it would put a new
  authentication path into the deployed production API, guarded by a secret being unset.
- **Production isolation is structural.** The job is created in staging and nowhere else,
  the workflow reaches staging and nothing else, and the interlock is a third lock on top.
  Two of the three are diffs a reviewer sees — in the private repo, which is where the
  mechanism belongs; this file states the property. Invariant 10 is untouched: production
  has no such job to run.
- **The allowlist is the sign-in path's own**, `motet_db.allowlist`, which is why it lives a
  package below the route that reads it: even CI cannot mint a session for an address Google
  sign-in would refuse. A second copy of that list is the thing to never write.

The widening this does buy, said plainly: **CI can write an `auth_sessions` row without
anybody signing in.** In staging that is not new reach — CI already applies every migration
and replaces every revision there — but it is a real change in what CI does.

### The vault is the seam to a credential that is not ours

`vault/` holds the envelope-encryption path: a per-record DEK, a KEK in Cloud KMS, and an
AAD bound to `user_id:source_id:provider`. **The AAD is the design, not decoration** — it is
what makes a ciphertext copied between rows fail to authenticate instead of handing one
account another's mailbox.

`MOTET_VAULT_BACKEND=local` is a fake in exactly the sense the inference fakes are fakes: it
implements the contract honestly with a local KEK, so the whole path runs in CI. It is
**refused when `MOTET_INFERENCE_MODE=real`**, because it is also the *default* — a deployed
environment quietly encrypting real tokens under a key in its own memory would satisfy
every test and none of invariant 8.

**`motet-api` and `motet-workers` depend on `motet-vault[kms]`, not on bare
`motet-vault`** — and that one bracket is what broke Gmail connect on production.
The SDK is imported lazily inside `CloudKmsKeyManager` so a laptop and CI
never pull in a cloud dependency they cannot use, which is right, and it is **not** a
reason for the *image* to be missing it: `uv sync --no-dev` installs default extras only,
nothing asked for `[kms]`, and so nothing had it. The lesson generalises past this one
package — **a lazy import is a statement about when, never about whether.**

Three things about how that failure presented are worth more than the fix:

- **It surfaced as far from its cause as it is possible to get.** The SDK went missing at
  build time; the first line of code to notice was an import, inside a request, in the
  OAuth callback, *after* Google had already issued a refresh token. Everything before it
  worked, including a clean `/internal/health`.
- **An unhandled exception is the one response that skips CORS**, so the browser could
  report nothing at all. Starlette's `ServerErrorMiddleware` sits outside every middleware
  `add_middleware` installs, `CORSMiddleware` included — so its 500 carries no
  `Access-Control-Allow-Origin`, a browser refuses to hand it to the caller, and `fetch`
  rejects with a bare `TypeError: Failed to fetch`: no status, no body, no clue. That
  string was the entire bug report. `main.UnhandledErrorMiddleware` now converts it into a
  500 the browser is allowed to read, from *inside* `CORSMiddleware`, which is what puts
  the header on it. **The stack is not what it looks like, and both of its two extra lines
  exist because of that.** `FastAPIInstrumentor` patches `build_middleware_stack` rather
  than calling `add_middleware`, so OpenTelemetry is **outermost** — outside CORS, outside
  everything the app adds. Catching an exception therefore hides it from two things that
  were relying on seeing it: OTel's own exception handler, which is why the middleware
  calls `obs.record_exception` (without it the span keeps an ERROR status and loses the
  type, message and stacktrace), and the Sentry SDK's outermost capture, which is why it
  calls `logger.exception` (the SDK's logging integration is what then carries it to
  GlitchTip). Deleting either line deletes a signal silently. `api/tests/test_deploy_wiring.py`
  walks the real stack and asserts the positions, because the first version of this
  described the order backwards and no behavioural test could tell. On the client side,
  `client.ts`'s `send()` turns a rejected `fetch` into a sentence naming the URL.
- **A key manager raises `VaultError`, and the kms backend used not to.**
  `PermissionDenied`, `NotFound`, `DefaultCredentialsError` and a missing SDK all escaped
  as themselves, straight past the callback's `except VaultError` and its 503. They are
  translated at the boundary now. `dek_wrapper` does the same for a vault that will not
  *build*, because a dependency resolves before the route body and the route's own handler
  cannot see that one.

**`/internal/health` reports `vault_backend` and `vault_ready`**, for exactly the reason it
reports `login_configured`: the vault is exercised once per mailbox, by a human, at the end
of a consent flow, so a deployment that cannot seal and one nobody has asked to seal for
look identical from outside. It resolves configuration and **does not call Cloud KMS** — the
route is unauthenticated, and a billed vendor call per request would be a free way to spend
money. The key path is never in the response; it is topology. The *backend name* is not —
the private repo's own service definition calls it "not secret", and "this deployment is on
the local backend" is precisely the misconfiguration the field exists to make visible. `bin/build-images` asserts the
flag against the real container, because whether the SDK is in the *image* is the one claim
the workspace's own venv cannot make on the image's behalf.

### Podcast clients read show notes, chapters and transcripts in more places than one

`api/src/motet_api/shownotes.py` renders all three from the transcript already stored —
each claim beside its source span, plus the timing the TTS stage apportions. Nothing new is
kept; the structure invariant 3 forced already *is* a citation-bearing transcript.

Where clients actually look, which is not always where the spec says:

- Show notes go in **both** `<description>` and `<content:encoded>`. Apple reads the first;
  most third-party clients prefer the second. A client that finds only one shows either
  plain text or raw tags.
- Chapters are emitted **twice** — inline as Podlove Simple Chapters and by reference as
  Podcasting 2.0. Different clients read different ones, and the inline form also works for
  a client that will not make a second authenticated request.
- `<podcast:transcript>` points at WebVTT with `rel="captions"`, because the cues are timed.

`ElementTree` has no CDATA support and escapes everything, which is wrong for
`content:encoded` — so that element gets an opaque token that is swapped for a real CDATA
section after serialization. The token carries a per-document nonce and contains no
character the writer would escape.

**A rendered episode only.** Before TTS every claim's timing is zero, so an advertised
transcript would be a stack of cues at 00:00 and chapters would all point at the start. An
absent document reads as "not available"; a wrong one reads as broken, and a client caches
it.

### The golden set is the seam to "is it any good?"

`goldens/` holds three corpora, one per stage that has no single right answer and fails
*quietly*: dedup and script (`fixtures/`), Gmail extraction (`gmail/`), and smart-episode
selection (`episodes/`). All of it runs in `bin/ci` against the fakes, where it asserts the
*structural* contract — every claim resolves to a real source span, dedup is stable, a
newsletter's prose survives and its machinery does not, a rule selects the same stories in
the same order twice. Scoring real model output against the corpus is a separate, later,
non-blocking job.

The selection corpus runs against **the real repository query and a real Postgres** rather
than a reimplementation of the ordering: the selection *is* an `ORDER BY` with a window
predicate and a source-count subquery, so a corpus that recomputed it in the harness would
pass while the SQL was wrong.

---

## Conventions

- **Feature branches only**; open a PR; never commit to `main`.
- **Python** is 3.13, managed with `uv` (workspace at the repo root). Lint and format with
  `ruff`; typecheck with `mypy` in strict mode.
- **TypeScript** is strict. The SPA is Vite + React; `tsc --noEmit` is the typecheck.
- **Migrations** are plain numbered SQL in `db/migrations/`, applied in order and recorded in
  `schema_migrations`. They are forward-only — write a new migration rather than editing an
  applied one. A `--` comment is the one exception, because it never reaches the database and
  so cannot make two environments disagree; the rule is protecting the SQL. A comment that has
  gone stale is fixed in place, since that is the line a reader meets first.
- **No `print`, no `console.log`** in committed code; use the logger, which routes to the obs
  stack.
