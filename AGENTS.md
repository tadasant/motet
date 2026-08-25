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
self-hosted runner pool behind a fork guard — see [Runner policy](#runner-policy).

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

Only **`http/protobuf`** is installed: the obs stack ingests OTLP over HTTP, and the gRPC
exporter would drag `grpcio` into both images for nothing. A different
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
| KMS-backed credentials | a Cloud KMS keyring | `MOTET_VAULT_BACKEND=kms` + `MOTET_VAULT_KMS_KEY` |

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

There is one other script, and exactly one reason it is separate:

```bash
bin/build-images
```

It builds and smoke-tests the three container images, and it is its own script because it
needs a **Docker daemon** — `bin/ci` needs only Postgres, and a laptop without Docker must
still be able to run every check in it. It is still a script rather than YAML, for the
same reason `bin/ci` is. CI runs it as a second job.

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
`MOTET_LLM_MODEL` moves every stage; `MOTET_LLM_MODEL_{DEDUP,SCRIPT,GROUNDING}` moves one.
Effort works the same way, defaulting per stage: dedup `low` (the volume line), script
`high`, grounding `max`.

Four things about this are settled, and each exists because of a specific failure:

- **An unknown slug or a missing key is a startup crash**, not a 500 an hour later.
  `validate_startup()` runs in the API's lifespan and in the worker entry point. Slugs are
  checked against a committed catalogue; `bin/check-openrouter-models` verifies that
  catalogue against OpenRouter's live list. That script is deliberately **not** in `bin/ci`,
  because CI is offline (invariant 7) — run it by hand when adding a model.
- **Reasoning can be dropped silently.** Anthropic's own API rejects an incompatible
  thinking config with a 400; OpenRouter drops the field and answers anyway. A response
  with no evidence of reasoning is logged and, by default, raised on. Never "fix" a
  `ReasoningNotAppliedError` by switching the check off — it is reporting that a stage ran
  without thinking.
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
  applied one.
- **No `print`, no `console.log`** in committed code; use the logger, which routes to the obs
  stack.
