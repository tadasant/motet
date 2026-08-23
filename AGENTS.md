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

3. **Every reported claim carries a source span, validated before TTS.** A briefing that
   invents a funding number is dead. Grounding validation runs *before* audio is
   synthesized, never after, and a claim that fails validation does not get spoken.
   See the tripwire below: this is not deferrable.

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
> instead — `motet_api.obs.status()` reports which exporters are actually configured, and
> it is exposed on the API's health surface.

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
  rewrite of everything downstream of the script.

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

**Phase 1's real deliverable is the factory, not the feature.** The question it answers is
*"does the factory work?"* — not *"is the briefing good?"*. That is what the scaffolding in
this repo is: the one CI command, the OpenAPI contract, the fake adapters, the golden set.

RSS rather than an in-app player is deliberate: it buys background audio, offline,
lockscreen, CarPlay, and speed control with zero iOS code.

---

## Architecture

| Component | Runtime | Directory |
|---|---|---|
| API | FastAPI, Cloud Run | `api/` |
| Ingestion workers | Cloud Run jobs | `workers/` |
| Inference adapters | library | `inference/` |
| Schema + migrations | library | `db/` |
| Web SPA | Vite + React, static behind Cloudflare | `web/` |
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
the contract and golden-set gates. The GitHub Actions workflow calls `bin/ci` and nothing
else. **If you add a check, add it to `bin/ci`** — a check that only exists in the workflow
is a check that cannot be run locally, and it will rot.

`bin/ci` needs a Postgres to run migrations against; see `CONTRIBUTING.md`.

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

Credentials are one enum plus one resolver in `llm/credentials.py`, and that file is the
whole seam for a future "bring your Claude Max account" quota kind. **Keep it one file.**

### The golden set is the seam to "is it any good?"

`goldens/` holds newsletters with their expected news items and a script considered good.
It will grow to ~20 cases. It runs in `bin/ci` against the fakes, where it asserts the
*structural* contract — every claim in the script resolves to a real source span, dedup is
stable, output is deterministic. Scoring real model output against the corpus is a separate,
later, non-blocking job.

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
