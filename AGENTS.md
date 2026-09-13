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
thing. And a better idea is a reason to **ask**, never a licence to build: where it would
change the shape of the system rather than the inside of it, invariant 12 says to stop and
put the options to the owner.

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

3. **Every reported claim carries the source span it was copied from.** Not *validated* —
   that is the reversal. Removing it was decided by Tadas, 2026-09-12, motet#75, which
   invariant 12 names as its own first consequence; the issue is the design session and
   the record of the alternatives. Grounding validation is gone from both halves of the
   system: the hard gate that stood between the script and Cartesia, and the advisory
   check that ran behind every conversational reply. What that costs is stated plainly in
   the issue and is worth restating here — **a claim can now be spoken with nothing having
   checked that its source supports it.**

   **The structure stays, and it is load-bearing for reasons that have nothing to do with
   the gate.** The script stage still asks the model for a verbatim `quote` per claim,
   `locate_quote` still finds it in the source or discards the claim, and
   `segment_claims.source_item_id` / `span_start` / `span_end` still record where it came
   from. Highlights anchor to that span, the show notes and the WebVTT transcript render
   from it, and the episode screen shows every claim beside its source. Removing spans is a
   separate and much larger decision; nothing here licenses it.

   **What still constrains what gets spoken is a prompt, and a prompt is not a guarantee.**
   `SCRIPT_SYSTEM` tells the model not to state a number, a name or a date its quote does
   not contain, and the conversational system prompt tells the model to answer only from
   the material it was handed or from a tool result. A claim whose quote cannot be located
   is dropped while the answer is parsed — `motet.script.claims_dropped{reason}` counts
   those — but nothing compares the sentence to the span.

   **If it comes back, it should come back cheaper first**: a deterministic check for
   numbers, names and quotations absent from the cited span, run as a counter rather than a
   gate. A model-backed gate is worth reconsidering when there is a way to measure whether
   it catches anything — which is the thing the removed one never had.

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
   script generation, and TTS each have a Protocol in `inference/` and a deterministic fake
   alongside the real adapter. Tests and CI use the fakes. No test in this repo may make a
   real vendor call.

8. **Source credentials are never plaintext at rest; only workers can decrypt.** Envelope
   encryption, Cloud KMS KEK, per-record DEK, AAD bound to `user_id:source_id:provider`.
   The decrypt permission is scoped to the worker service account — that IAM boundary is
   the actual control, not the encryption.

---

## Operating invariants

Settled with Tadas in Zimmer session 8241 — 9 to 11 there, 12 in motet#76. These govern
how the system is built and run, not what it does.

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

**This invariant stands as written, and invariant 12 is its structural counterpart** — the
same human-owned boundary drawn around what the system *is* rather than around what it is
allowed to spend or sign for.

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

**`/internal/health` reports `revision`, and it is a read of a value the deploy already
sets rather than new plumbing.** `service.version` in `OTEL_RESOURCE_ATTRIBUTES` is the
commit the image was built from; `motet_obs` has always resolved it, because GlitchTip
takes it as the release. `ObsStatus` carries it so that the label the spans wear and the
label the route reports are one string resolved once — the same argument the service name
already makes. **The route does not repeat it verbatim**: it is unauthenticated and this
repo is public, so `motet_api.main.REVISION_PATTERN` admits a commit SHA and the deploy's
`bootstrap` sentinel and refuses anything carrying `/`, `:`, `.` or `@`. That is the
difference between a disclosure argument that is a property of this repo and one that is a
promise about a variable in the other one — and it is `vault_ready`'s `detail` being
withheld, one field along, for the same reason. A refused value reports `null` and says so
at ERROR on startup.

**An endpoint without a credential is not "configured."** obs rejects an unauthenticated
export, so that combination buys a 401 per export rather than data — which reads as an obs
fault. `/internal/health` reports `telemetry_configured: false` for it deliberately, and
startup logs a warning saying so.

**A library's own log record gets two guards, and they are deliberately different widths.**
`motet_obs.runtime` carries `_NO_EXPORT_LOGGERS` — a feedback-loop guard on the OTLP *log*
handler, because exporting the log exporter's failure produces another failure fast enough
to saturate a container — and `_NO_EVENT_LOGGERS`, a `before_send` hook keeping a
third-party diagnostic out of GlitchTip, where a new issue in the production project pages
Slack. **One tuple served both and could not**, which is motet#73: the loop guard is a
filter on a handler, and `sentry_sdk`'s logging integration patches
`logging.Logger.callHandlers`, so no handler-level filter reaches it. `Failed to export
metrics batch due to timeout, max retries or shutdown.` therefore paged — while being
excluded from the log pipeline, which left it on **no** obs surface at all.

So the loop guard is now **exactly as wide as the loop** — which is wider than "the log
exporter", because the handler, the batch processor, the protobuf encoder and the attribute
cleaner all log from inside `emit` or `export`, on the emitting thread. A *metric* or
*trace* exporter's diagnostic does not, so it now reaches VictoriaLogs, which is where
invariant 11 wants it — observable to an agent, and not a page. `sentry_sdk` came off that
list too and stays off: nothing an OTLP export does produces a `sentry_sdk` record, so a
GlitchTip outage is now visible somewhere. `urllib3` is the one entry on *both* guards and
therefore keeps only stdout, because a transport record cannot be attributed to a signal —
an accepted cost, named in the docstring rather than left to be rediscovered.

The event guard stays at whole-namespace width, because "is this a Motet fault" is a
question about who wrote the code, not about which signal failed, and it is a `before_send`
hook rather than `ignore_logger` so that the dropped records still ride along as
breadcrumbs on a real Motet error. **Every `opentelemetry` entry in the loop guard is an
underscore-private module path**, and upstream has moved one before — so
`obs/tests/test_alert_scoping.py` asserts each literal against the installed class's own
`__module__`, and a dependency bump that renames one is a red test rather than a saturated
container. That module pins both guards against the real SDKs, reading events out of the
Sentry envelopes that arrive at the same local socket the OTLP collector answers on —
whether a record becomes an event is decided inside `sentry_sdk`, so a stub would be
testing the stub.

### 12. No new architecture without an explicit design session and human sign-off

Decided by Tadas, 2026-09-12, motet#76; motet#75 is its first consequence. **This section
is the worked example of its own rule** — the sign-off goes at the top, because an
invariant that did not record its own would be asking for something nothing in this file
demonstrates.

An agent never introduces, replaces or removes a piece of architecture on its own
judgement, however well argued. It stops, lays out the options and their costs, and waits
for the owner to choose. The sign-off is recorded in the PR and in the AGENTS.md section
that describes the change.

**This exists because the system grew structure one well-justified step at a time.** The
grounding gate went from one model call to a chunked, halving, self-narrowing, fenced,
fail-closed subsystem across four issues. The job queue went from `SKIP LOCKED` to a lease
heartbeat, two fences, a lock order and a bounded pruner across five. Every step was
correct in isolation, documented here, and merged. **The aggregate was never chosen by
anyone** — which is the failure this invariant names, and it is not one any individual
review could have caught.

**What counts as architecture.** The test is: would the system diagram or the package graph
change, or would a new mechanism need its own section in this file to be understood? If
yes, it needs sign-off. Concretely, at least:

- A new deployable, image, or process shape (a new service, job, or a change from one-shot
  to always-on).
- A new datastore, cache, queue, or a new role for an existing table (a table used as a
  queue, a table used as a log).
- A new vendor, provider, or seam, or a second implementation behind an existing seam.
- A new cross-service protocol or contract (a new API surface between our own services, a
  WebSocket contract, a session contract).
- A new mechanism in the job queue or the pipeline (a fence, a lease, a retry policy, a new
  stage, a scheduler).
- A new inference stage, or a new model call anywhere one does not exist today.
- Anything that requires a new resource, IAM grant, secret, or variable in the private
  infrastructure repo.
- Removing any of the above.

**What does not.** Work inside an existing shape needs no session: a route on the existing
API, a column on an existing table used the way that table is already used, a handler change
within the existing queue mechanics, a bug fix inside an existing adapter, a test, a metric
on an existing instrument, docs.

**When in doubt, it counts.** The cost of asking is a short conversation; the cost of not
asking is a mechanism the owner has to discover after it ships. The asymmetry is deliberate.

**A decision this file already records has had its session, and building it is not a new
one.** Invariant 3's cheaper deterministic check and Cartesia's own timestamp output are
each written down here as the intended next step, with the condition that triggers them;
so is every tripwire, as a decision against. What needs a session is a mechanism nobody
chose — not one whose choosing is on the page.

**What a design session looks like.** The agent stops before writing code and presents, **in
the conversation rather than a PR**:

1. The problem, with the evidence that it is real — an incident, a measurement — not a
   hypothetical.
2. At least two options, including "do nothing" where it is viable, each with what it buys,
   what it costs, and what it would take to reverse.
3. A recommendation.

The owner picks. The PR then records the choice and the alternatives rejected, and the
section in this file for the mechanism opens with the sign-off rather than only the
reasoning.

**Invariant 9 is this one's other half, and the split is what each protects.** 9 draws the
human-owned boundary around *one-time setup* — provisioning an account, minting a first key
— and says everything inside it must be reachable by an agent. 12 is its structural
counterpart: it draws the same kind of boundary around *what the system is*, and for the
same reason. These are the choices where a person decides the shape, and they should not be
reachable by an agent optimising a local problem. Neither invariant makes the other's call:
9 is about who may spend money or accept a terms-of-service, 12 is about who may add a
mechanism.

**So 9's operational half survives 12 intact, and the line between them is structure
against operation.** Deploying, rotating a provisioned secret, adding a DNS record, scaling
a service, running a migration — 9 says an agent does all of those with no human in the
loop, and running the system the owner already chose is not adding to it. The seventh
bullet above bites when the resource *is* the new structure, not when it is the routine
operation of structure that exists. A routine operation that stalls waiting for a human is
still the defect 9 names. Where both readings genuinely fit, "when in doubt, it counts"
decides — that is what it is for.

---

## Tripwires

Signals that the project has gone wrong. If one fires, stop and re-plan rather than
pushing through. **These stand as written; they are the specific instances of the general
rule invariant 12 now states** — decisions already taken, named in advance so that the
design session does not have to be held twice.

- **The SPA is not the product.** It is the eyes-on backlog surface, and in Phase 1 it is
  three thin screens over the API. If SPA work is still running after a week, something has
  gone wrong — you are building a product instead of a factory.
- **Never reach for Redis or a vector store.** Postgres holds the data *and* the job queue
  (`SELECT ... FOR UPDATE SKIP LOCKED`). A day of news items is about 4.5k tokens, which is
  passed in-prompt; there is nothing to embed. Reaching for either is a sign of solving a
  scale problem this system does not have.
- **Never drop the claim-to-span structure.** Grounding validation is gone (invariant 3,
  motet#75) and the shape it forced into existence is not: a claim carries the source item
  and the character range its quote was located at. Highlights anchor there, the show notes
  and the transcript render from there, and the episode screen shows a claim beside its
  source. Deleting that is a much larger change than deleting the checker was, and it is
  not implied by it.

---

## Phase 1 — Infra MVP

Paste arbitrary text in, get an episode out, listen on a dog walk. One hardcoded user.

```
paste-in → Source Item → News Item (deduped)
        → Episode → script → Cartesia Sonic → GCS → private authenticated RSS feed
```

**In:** paste-in ingestion, dedup/integrate, manual episodes ("all unread", duration-capped),
script, TTS, GCS, private authenticated RSS, a 3-screen SPA (paste-in, backlog, episode),
a single hardcoded account.

**Out — do not build these yet:** Gmail, X, OAuth, the secret store, smart episodes,
ranking, iOS, voice interactivity, signup, brand.

**Status: the Phase 1 path is built** — paste-in, dedup/integrate, assemble, script, TTS,
object storage, the private feed, and the three SPA screens. The stages run as Cloud Run
jobs draining Postgres queues (`workers/`), and every one of them
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
`/internal/health` is how you tell — it reports `revision`, the commit the serving image
was built from. **The served OpenAPI document is the weaker instrument and was the only one
for a while**, which is motet#37: a document diff bounds the build to a *range*, and only
when consecutive commits happen to differ in their route table, so a bugfix bump — which
usually changes only behaviour — is invisible to it. Pushing the image and the runtime
environment the services get are tracked in the private infrastructure repo.

**Phase 1's real deliverable is the factory, not the feature.** The question it answers is
*"does the factory work?"* — not *"is the briefing good?"*. That is what the scaffolding in
this repo is: the one CI command, the OpenAPI contract, the fake adapters, the golden set.

RSS rather than an in-app player is deliberate: it buys background audio, offline,
lockscreen, CarPlay, and speed control with zero iOS code. The SPA grew an in-page player
in motet#89 for listening at a desk; it does not change this, because a browser tab still
has no background audio and no offline.

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
put that click somewhere a human can reach. Everything after it that is free — poll, fetch,
extract — is automatic; the first model call waits for a person to press **Ingest now**
(see "Connecting a source does the free work at once" below).

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

**It is also served back, and that read is what makes the position cross-device** (motet#11).
`EpisodeResponse.listened_through_ms` carries it on every episode a client loads, so a phone
that has never played an episode can still resume where a laptop got to — invariant 4's "the
position is ours" reaching past the device that did the listening. `PUT
/v1/episodes/{id}/position` is the write a syncing player wants, and it is **the same handler
as `POST /v1/episodes/{id}/progress`**, two decorators on one function rather than two write
paths: a second path would be a second definition of one fact, which is exactly what
invariant 5 forbids. `POST .../progress` stays because it is the shipped contract in both
deployed environments and every generated client already carries it — removing a route is a
breaking change an additive feature has no business making — not because anything is known to
call it today. The explicit `summary=` on the `PUT` decorator is load-bearing: the Swift
generator names its endpoint function from the summary, and FastAPI would derive the same one
for both routes.

**The value is monotonic, so it is the *furthest* point rather than the playhead**, and the
distinction is the one thing to get right when wiring a client to it. The iOS store keeps
both — `spokenThroughMs`, which moves backwards when the listener seeks back, and
`furthestSpokenMs`, which does not — and the server's column is the second of those. That is
why the API does not spell this field `spoken_through_ms` however much a client would like it
to: the same name for two different quantities across the client boundary is worse than two
names for one. A last-write-wins playhead would also be the wrong thing to sync, because a
durable offline outbox replays *stale* writes, and last-write-wins on a stale write rewinds a
walk. Where the listener scrubbed back to stays on the device, deliberately.

**Claim timings are apportioned, not measured.** Narration is synthesized per *segment*, so
segment boundaries are exact and claims within a segment are proportioned by length. Going
per-claim would give exact timings at the cost of three to four times the request count and
a hard prosody break at every sentence, for an error well inside what a caption cue needs.
If word-level timing is ever needed, the upgrade is Cartesia's own timestamp output rather
than more calls.

**Out, and still out:** X bookmarks (verify the API tier first — Tadas's spend decision)
and the iOS app. The voice/interaction path is built and **dormant** — no voice service is
deployed — see "Play Live" below.

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

**Inference.** Claude for dedup/integrate and script generation, reached
**through OpenRouter** and defaulting to Claude Sonnet 5; Cartesia Sonic for TTS.
OpenAI Realtime (voice) and Exa (research) arrive in Phase 2. Every one of them sits behind
an interface with a fake — invariant 7.

**Two voices on purpose:** Sonic narrates, the realtime model converses. That decouples
voice identity from the realtime vendor.

**Two audio paths, deliberately separate.** Narration is batch and offline-capable
(script → TTS → GCS → client plays locally). Interaction is realtime and
online-only (barge-in → Pipecat → realtime provider → tools → resume narration). Realtime is
10–15% of session minutes, not 100%. This split is what makes offline possible and the
economics work.

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

Five other scripts sit outside it, for three different reasons. Two need a toolchain
`bin/ci` deliberately does not require; two are the opposite of `bin/ci`'s whole premise,
which is that a run is offline, free and fake; and one is the opposite of its *shape*,
because every check in `bin/ci` finishes and this one starts processes and waits:

```bash
bin/build-images              # needs a Docker daemon
ios/bin/build-app             # needs Xcode
bin/check-openrouter-models   # needs OpenRouter's live model list
bin/local-env                 # needs a service account key, and the network
bin/dev                       # needs a Docker daemon, and never returns
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

`bin/local-env` is the fourth. **Decided by Tadas, motet#79** — it adds a seam, a vendor
SDK and a service account in the private repo, which is three of invariant 12's bullets, so
the sign-off goes here rather than the reasoning alone. It is outside `bin/ci` for
`bin/check-openrouter-models`'s reason turned up one notch: **it is the one script in this
repo whose job is to reach a cloud API for real credentials**, and `bin/ci` is offline, free
and `MOTET_INFERENCE_MODE=fake` by design (invariant 7).

It writes the `.env` a **real-mode local run** needs, and the promise it keeps is that a
laptop needs exactly *one* non-public thing on it: a service account key with read access
to the local-dev secrets. Minting that key is a one-time human-owned step (invariant 9);
everything after it is the script.

**It discovers a roster rather than restating one, and that is what lets it live here.**
The project id comes from the key file's own `project_id` field, and the secrets are
whichever ones carry the label `motet-local=true` — both decided in the private
infrastructure repo (tadasant-internal#2804). So this repo names no project, no secret
roster and no staging value, and a change to any of them is a re-run rather than a PR. The
localhost overrides it appends — `MOTET_INFERENCE_MODE=real`, the `kms` vault backend,
local storage, `localhost` URLs — are the opposite: application knowledge, so they are a
literal block in `tools/local_env.py` and are covered by tests.

Three guards on it are load-bearing rather than polish, because the file it writes holds
real vendor keys: **no value is ever printed** — not on success, not in an error message —
the file is written `0600`, and an existing one is never replaced without `--force`. The
block is also *authoritative*: a labelled secret carrying one of the override names is
dropped with a line naming it, because "`MOTET_API_TOKEN` is unset locally" has to mean
unset rather than describe an intention that a roster change can silently reverse.

**`bin/ci` refuses a `.env` structurally**, which is this change's one edit to it:
`unset UV_ENV_FILE` plus `UV_NO_ENV_FILE=1`, before anything runs. `uv run` does not read
`.env` on its own — the variable is what makes it — so the instruction this script prints
turns a shell into one where *every* `uv run` inherits the file, `bin/ci` included. Two
of its variables were already pinned and the rest were not: a `bin/ci` in that shell would
have handed the test suite staging's OTel endpoint and ingest token and shipped its
telemetry to the estate's obs stack under a real credential, passing green the whole way.
Pinning the two that matter is counting; refusing the file is a property.

**What nothing in CI can tell you is whether it works**, and that is structural rather than
an omission. The labelled secrets do not exist until the private half lands, and a test
that reached Secret Manager would be the vendor call invariant 7 forbids — so the seam is a
`SecretReader` Protocol with a fake, exactly like every other vendor in this repo, and what
is pinned is the key-file read, the rendering, the refusal and the mode. The *adapter* is
the half a fake cannot cover — the filter string, the `versions/latest` alias, the id
parsed off a resource name — and a typo in any of those ships green, so it is driven over
a stub that records the requests and they are asserted, which is `api/tests/test_drain.py`
asserting the bytes rather than a fake's bookkeeping. What is left is whether Google
answers the request the way the SDK's own types say it will, and the first real run is a
human's, against a key a human minted.

### `bin/dev` runs the processes; compose runs the one thing that holds state

**Decided in motet#83**, which named the design question and answered it, and whose issue
gate instructed the implementing session to take that reading. The split is the decision:
`docker-compose.yml` owns **Postgres and nothing else**, and `bin/dev` (`tools/dev.py`)
owns the API, a polling worker, and the SPA's dev server, on the host.

The reasons are the issue's and they are concrete. `uvicorn --reload` and `vite` both watch
the host filesystem, which in a container is a bind mount and a per-platform inotify story
for a loop that already works. And the API and the worker need `UV_ENV_FILE=.env` *and* a
still-exported `GOOGLE_APPLICATION_CREDENTIALS` — the KMS vault builds its client from ADC
— so containerising them would mean bind-mounting a service account key into a container to
keep real mode working. Postgres has neither problem, and it is where both of the sharp
edges lived: the database name, and a port.

Four things about it are the design:

- **The compose file creates `motet_dev` *and* `motet_test`** (`db/local/`, which the image
  runs once on an empty volume), because a laptop needs both and they are not
  interchangeable — the second is what `bin/ci` defaults to and what `conftest.py` creates
  a per-run database from. `docker exec motet-pg createdb -U postgres motet_dev` was a
  documented manual step whose omission surfaces as a migration failing against a database
  that is not there.
- **The healthcheck asserts both databases answer a query, over TCP.** `pg_isready` alone
  would not: the image runs a *temporary* server while it executes the init scripts, so a
  check that only asks "is a server accepting connections" can pass before `motet_test`
  exists — and `-h 127.0.0.1` forces the transport that temporary server does not listen
  on. `--wait` is only a bring-up if the health question is the one the caller needs.
- **Teardown signals each child's process *group*.** Both interesting children are wrappers
  — `uv run` around `uvicorn`, `npm run dev` around `vite` — so signalling the pid the
  supervisor holds reaches the wrapper and orphans the process actually holding the port.
  Each child is started in a session of its own, so Ctrl-C arrives at the supervisor alone
  and teardown happens in one place, with a grace period and a SIGKILL behind it.
  `tools/tests/test_dev.py` proves it on a real grandchild rather than asserting a call was
  made, which is `bin/build-images`' argument one seam along.
- **It does not set `UV_ENV_FILE`, and that is a safety property rather than an omission.**
  `uv run` reads no `.env` without it, real mode spends real money against staging's caps,
  and that export is documented as the deliberate act that turns real mode on. What
  `bin/dev` adds is a line saying the file is there and unread — "my .env is ignored" and
  "I forgot the export" are otherwise the same five minutes. It also names every `.env`
  variable the shell already exports with a different value, because `uv run` never
  overrides one and a stale key from `~/.zshrc` otherwise fails as a vendor 401 (motet#85).
  `bin/local-env` says the same at write time. Both wrappers run `uv run --no-env-file`
  for that reason: a supervisor whose own environment uv had filled from the file could
  not tell an export from a loaded line. The children still read it.

**The API's port is now set in one place and passed to the other.** `web/vite.config.ts`
reads `MOTET_DEV_API_PORT` (defaulting to 8000) and `bin/dev` exports it to the Vite child,
so `--api-port 8123` moves both. It used to be a literal in the Vite config, which made any
other port return `index.html` for `/v1/...` and fail as a JSON parse error pointing nowhere
near the cause. The SPA's own port is passed with `--strictPort` for the same class of
reason: Vite silently picking 5174 breaks a registered OAuth redirect URI, and Google
matches one as an exact string.

**The invariant-12 reading, recorded as invariant 12 asks.** This adds no deployable, no
datastore, no vendor, no seam, no protocol, no queue mechanism, no inference stage and no
resource in the private repo — nothing it touches runs anywhere but a laptop, and the
Postgres it starts is the Postgres `CONTRIBUTING.md` already told you to start by hand. The
judgement taken is that **motet#83 is its design session**: the owner filed it, named the
alternative ("add a `docker-compose.yml` that runs everything"), argued against it with
reasons, and recommended this split; the issue gate then told the implementing session to
take that reading. This section is the record.

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
`MOTET_LLM_MODEL` moves every stage;
`MOTET_LLM_MODEL_{DEDUP,DEDUP_CONFIRM,SCRIPT,VOICE}` moves one. Effort works the same way,
defaulting per stage: dedup `low` (the volume line), dedup_confirm `medium`, script
`high`, voice `off`.

**A "stage" is a caller with its own cost profile, not a step in the pipeline**, which is
what lets the voice service's conversational turn be one of them (motet#6) — and what lets
dedup's second look be another, one call in the pipeline further on than dedup itself and
made a fraction as often (see "dedup contradicting itself" below). It used to
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
  accounting, while the script stage stuck to one that does" fits every observation just
  as well, and would mean a thought answer whose accounting was lost. It does not
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

`inference/src/motet_inference/accounting.py`. motet#25's shape: the work happened and the
evidence was discarded. Usage was decoded off every OpenRouter response and read by nobody.
(motet#24 was the same shape one stage along, on the grounding gate's drops; that gate is
gone — motet#75 — and `motet.script.claims_dropped{reason}` is what is left of the pair.)

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
adapters over `FakeLlmClient`, which is what `inference/tests/test_accounting.py` does.

**The voice conversational turn records itself, in `voice/`, and that is the same rule
rather than an exception to it** (motet#58). The load-bearing half of "recording lives in
the stage adapters" is *the object that owns the call and names the stage is the object
that records it* — and for `LlmStage.VOICE` that object is
`LlmConversationModel.reply()`, which is not a pipeline stage and does not live in
`inference/`. Moving the leg across the package boundary to make it look like the pipeline
stages would drag voice's own `TurnRequest` and system prompt into `motet-inference` and
point the dependency arrow backwards. Until it recorded, a real voice session's completions
were billed and appeared in no metric and no log line, so a Grafana panel split by `stage`
showed three series where the enum has four — a voice fleet spending money and a voice
fleet nobody has used looked identical.

**A voice session's "what did that one cost" line is keyed by session id, and the block is
per turn.** `collect_usage()` is a `ContextVar`, so it holds across the awaits of one turn
in one task; a session is a socket's lifetime across many tasks and a block around it would
not reliably see anything. `VoiceSession` therefore opens one per turn, logs that turn's
total beside the session id, sums the entries onto `VoiceSession.spend`, and reports
`llm_completions` and `llm_tokens` in the summary logged on close. Same mechanism as an
episode's, one scope smaller.

**Every usage field is logged even at zero.** A field that vanishes when it is zero is a
field a log query cannot aggregate, and `cache_read=0` is precisely the observation the
prompt-caching warning above is about.

On the drop half, **`motet.script.claims_dropped{reason}` is what a claim's loss is
counted as**, and it is a script-prompt problem every time: the parser could not locate the
quote, or the claim named a source the story does not have. It is the only such instrument
left — the grounding gate's two counters went with the gate (motet#75). A dropped claim
leaves no row anywhere and the model writes a different script on every run, so the log
line beside the counter is the only moment the detail exists.

### Ingestion state is a join onto the job queue, not a column

`GET /v1/ingestion` (`repo.list_ingestion`) is what stops content from silently
disappearing. It reports source items that are not in the backlog yet — pending, failed,
and for ten minutes after they succeed — each joined to its `integrate` job. A *held* item
(pending with no job, motet#91) is not on its way anywhere and is reported by
`/v1/source-items/held` instead; see "Connecting a source does the free work at once".

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
message. `handle_poll`'s pre-check refuses to queue a message that already has an extract
job in any state, so two jobs for one message are no longer routine — but a re-read that
reaches back past job retention (below) can still meet a message whose earlier job was
pruned, and the exclusion is what keeps that one line too. Reporting one
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

In the SPA it is a panel above the backlog and a count on the sidebar's Backlog item —
visible from the *paste* screen, which is where somebody who has just pasted is. It polls only while
something is pending, and the fetch is **best-effort**: the backlog is the primary list and
must not go blank because the secondary one 404s.

### Connecting a source does the free work at once; inference waits for "Ingest now"

`repo._HELD_WHERE`, `/v1/source-items/{held,integrate,dismiss}`, `GET
/v1/source-items/{id}`, migration 0012. **Specified by Tadas in motet#91**, which states the
design, the evidence and the checklists; its issue gate read it as proceeding with every
open question left at the prototype's answer, and this section is the record of that
choice. Connecting a mailbox used to be a
standing authorization to spend: every message a poll found was extracted *and* queued for
dedup, and the first real connect queued forty dedup calls before the Sources screen had
finished re-rendering. `handle_extract` now stops after writing the source item, and a
person picks what to brief on the Backlog's held panel.

**Held is a join, not a column.** A `pending` source item with no `integrate` job *is* the
held state. `_HELD_WHERE` spells it once, for the listing, the claim and the dismiss, and it
is the exact negation of the condition `list_ingestion` reports a pending item on — so every
item is on exactly one surface, and a held one is never "on the way in". Reported as
pending, it was what made the Processing panel call a deliberately waiting item stalled.
The anomaly that arm used to be kept for — a paste whose job row is somehow missing — now
lands on the held list, with a checkbox that repairs it.

**Paste is not held**, because pasting is asking; it still queues its job in the same
transaction as its row.

**The claim takes a per-user transaction-level advisory lock**, `pg_advisory_xact_lock(2,
hashtext(user_id))`, and that lock is what stops two tabs from writing two integrate jobs:
without it the second request answers its `NOT EXISTS` from a snapshot in which the item
still looked held. The two-argument form lives in a different lock space from the worker's
one-argument `try_lock` on the same user, so "Ingest now" never waits behind a running
integrate job. Each job is written exactly as a paste's is — same queue, payload and
`serialize_key` — so invariant 6 is untouched. Ids that do not qualify are skipped, never
refused: a second tab is not an error.

**Dismiss is a state, not a delete.** The `(source_id, external_id)` row is how a re-poll
knows it has seen a message, so a deleted row would be fetched and held again on the next
bounded resync. It takes the same lock as the claim, so a dismiss and an ingest racing for
one item cannot both win, and only a held item can be dismissed — one that has started has
a job a state flip would strand. Nothing un-dismisses; the SPA asks first.

**`received_at` is the message's `Date:`**, clamped to the database's now, because a 60-day
first sync stored every message inside one minute and the list read "5:04 PM today" on
every row. Rows from before migration 0012 were backfilled from `created_at`: the header was
read and discarded at extraction, and the raw bytes are not kept.

**Dedup's decision is persisted on `news_item_sources`**, the row that already recorded
*which* thing dedup did (`position`). `relation`, `reason`, `candidate_id` and `model` are
the first pass's answer, carried on `IntegrationResult.decision` — optional on the seam, so
a test double that explains nothing is still an integrator, and its absence is NULLs rather
than an invention. `basis` is the step the outcome rests on: `first_pass`, `second_look`, or
`title_backstop`, the last being a merge the model never asked for, which a stored
`unrelated` beside a merge would otherwise misreport. `decided_title` and `decided_summary`
are the news item's copy as this decision left it, because the news item's own columns are
rewritten by every later merge. The mis-merge that motivated this was visible as `merged`
and could not be explained; now it can.

**The lifecycle view's stage 2 is a list of steps**, with dedup as the only one, so that
enrichment steps can join it without a new shape. **Per-item spend is deliberately not
persisted**: `handle_integrate` already logs the total beside the source item id and the
metric carries it per stage, and motet#92's `llm_usage` design is where a per-item ledger
would live — if it survives its design session, this becomes a join, not a column.
`cost_recorded: false` says so on every step rather than leaving a blank.

**The invariant-12 reading, recorded as invariant 12 asks.** Taking the automatic
extract→integrate step out and putting a person in its place changes the pipeline's shape,
and "when in doubt, it counts" — so the sign-off this rests on is the owner's issue, not the
size of the diff, exactly as motet#78's and motet#83's sections read theirs. What it adds is
one enqueue removed, four routes on the existing API, nine nullable-or-defaulted columns and
a state on existing tables used the way those tables are already used, and a lock in the
advisory-lock family the queue already uses: no new deployable, datastore, vendor, seam,
stage, model call or resource. `IntegrationResult`
gains an optional field rather than a second implementation. The two things the issue
*names* that are above the line — raw-bytes retention in object storage and agentic
enrichment — are deferred to their own sessions.

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

### Enqueuing is an event, so the API starts the worker rather than waiting for a clock

`motet_api.drain`. Every enqueue in this API traces to a person doing something — pasting
text, asking for an episode, connecting a mailbox, pressing "Ingest now" — so the API knows the exact moment
there is something to drain, and it used to do nothing with that. The `motet-worker` job
was started by a standing Cloud Scheduler sweep that fired whether or not any work
existed: roughly 21,900 executions a month per environment, essentially all of which found
nothing.

**It lives in the API's request path, never in the shared `enqueue_*` helpers**, and that
is the placement rather than a detail of it. `handle_poll` re-arms a poll from inside the
worker, so a trigger inside `enqueue_source_poll` would fire from worker code — an
execution starting another — and "an execution exists because a user did something"
would stop being true. The routes arm a per-request nudge; the helpers know nothing about
it, and `motet-workers` does not import `motet_api` — it could not without a dependency
cycle. A test drains the poll queue through a worker-side re-arm with every trigger in the
process spied on, and asserts nothing fired.

**Be precise about which win this is, because the obvious one is not real.** Cloud Run's
*job scheduling latency* — the gap between an execution being created and the task
actually starting — was measured at 90–165 seconds in staging, and every trigger pays it
whether a schedule or a paste caused it. So this saves at most the poll interval out of a
three-to-five-minute total; it does not make draining feel instant and no copy anywhere
should say it does. The two arguments that do land are that idle cost goes to zero, and
that a Cloud Run execution then exists **because a user did something** rather than
because a clock fired.

**The Cloud Scheduler drain stays.** It is the backstop that makes every failure below
cost latency instead of work, and retiring it is a separate decision for a human once the
event path has been watched in staging. The cost argument only fully lands once it goes.

Five things are settled, and each is a different failure this shape avoids:

- **No request body, ever.** The scheduler version of this call sent
  `{"overrides":{"containerOverrides":[{"args":["all"]}]}}`, mirroring `gcloud run jobs
  execute --args=all`. Cloud Run rejected it and reported the rejection **only to GCP
  Cloud Logging**, which invariant 11 means nothing in this estate can read — so the job
  was created cleanly, every Terraform plan showed no drift, and every tick produced no
  execution, no container and no log line anywhere. It took four applies to bisect by the
  only available evidence. The worker job declares `args = ["all"]` in its own definition,
  so an unmodified execution already drains every queue. `api/tests/test_drain.py` drives
  the real adapter over `httpx.MockTransport` and asserts the bytes, because this is a
  claim about a socket rather than about a fake's bookkeeping. It is a permission fact as
  well as a validation one: `roles/run.invoker` carries `run.jobs.run` and not
  `run.jobs.runWithOverrides`, so a body is refused outright.
- **The trigger is inert unless an environment opts in, and it names nothing it was not
  handed.** `MOTET_DRAIN_TRIGGER` is off by default, like the scheduler drain that is off
  unless an environment names a cadence. The grant is per-environment in the private repo
  (`api_can_trigger_worker`: staging on, production off until a human flips it), and the
  switch is meant to be set *from that same flag*. The project is `GOOGLE_CLOUD_PROJECT`,
  already injected; the job name defaults to `motet-worker`; the region has **no** default,
  because it is a fact about the private estate and this repo is public. **So
  `/internal/health` reports `drain_trigger`**, for exactly `vault_ready`'s reason: an
  inert trigger and a working one look identical from outside. The job's location is *not*
  reported — a project id and a region on a public, unauthenticated route.
- **It never fails the request.** The job row is committed inside the API's own
  transaction before anything is asked to drain, so a permission error, a quota error or a
  timeout costs latency and nothing else. `fire` swallows everything and records it; a
  paste must not 500 because the drain trigger could not fire. **A 403 is expected, not
  alarming** — it is what any environment without the grant answers — so it is a WARNING
  and `outcome="denied"`, never an ERROR: only ERROR reaches GlitchTip, and a page per
  paste for a decision somebody made on purpose is how an error channel stops being read.
- **The route arms it and the transaction fires it**, in `deps.connection`, after
  `conn.commit()`. A nudge for work no other process can see yet is a nudge for nothing,
  and a request that fails on its way to the response fires nothing at all. Deliberately
  **not** a background task, which is where a best-effort call would otherwise belong:
  Cloud Run throttles a container's CPU between requests unless the service asks
  otherwise, so a task scheduled after the response may not run until the next request
  arrives — and a nudge that fires unpredictably is worse than none, because the scheduled
  sweep is what it would then be silently relying on. **"Before the response" is bought
  by `scope="function"` on every `Depends(connection)`, and it was not true before this
  change.** FastAPI's default scope tears a `yield` dependency down *after* the response
  is sent, so the commit had always run post-response — a failed commit was a 201 the
  client already held — and a nudge placed beside it would have landed in exactly the
  throttled window this bullet rules out. The scope is part of FastAPI's dependency cache
  key, so all three sites must agree or a request gets two connections;
  `test_the_nudge_fires_before_the_response_starts` pins the order on a raw ASGI `send`,
  which is the one place it is observable.
- **A burst of enqueues starts a burst of executions, and that is accepted rather than
  overlooked.** Concurrency is already handled and already load-bearing — `SKIP LOCKED`,
  the per-user `serialize_key`, the lease keeper, the work fence — and the always-on
  fleet plus the scheduler already produce concurrent executions, so this adds no new
  class of it. Coalescing would trade that for a cost saving worth fractions of a cent on
  a product with one user, and it would rest on the scheduling-latency figure above: an
  in-process debounce is only correct while the *pending* execution is guaranteed to start
  after the enqueue it suppressed, and a measurement is not a guarantee. In-process state
  is also only partly effective, since Cloud Run runs several API instances; real
  coalescing would be a Postgres row and a lock on the one path whose whole contract is
  that it never fails. `motet.api.drain_triggers{reason,outcome}` is what would say the
  burst rate had outgrown the decision, and a row keyed per environment is where it would
  go if it had. **Invariant 6 holds on the claiming side, which is where it always had to:**
  a triggered execution and a scheduled one running at once is two drains, and `integrate`
  and `poll` are claimed under a `serialize_key` (the user, and the source) held as a
  Postgres advisory lock — so the second finds the key busy and defers without spending an
  attempt. `workers/tests/test_pipeline.py::TestSerialization` pins it on two real
  connections.

**A lazy import is a statement about when, never about whether** — the `motet-vault[kms]`
lesson, one seam along. `google-auth[requests]` is declared on `motet-api` rather than
inherited through `motet-storage`, and `AdcAccessToken` imports it at *construction*, so a
missing SDK is an ERROR at startup and `drain_trigger: false` rather than silence inside a
call that by contract never raises. `bin/build-images` puts that question to a real
container, next to the `vault_ready` assertion that exists for the same reason.

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

**This is a backstop and not the fix, and the difference is the thing to keep.** What is
not a judgement call is the narrow case here: dedup *writes* the titles, so two items
carrying the same one is one stage disagreeing with itself. Why the threshold missed on
genuinely independent prose about one event is a different question, and the section below
is its answer.

#### The band where dedup says it is unsure gets a second look

`motet_inference.prompts.INTEGRATE_SCHEMA`, `ClaudeIntegrator._is_same_event`. The other
half of motet#41, and the observation it turns on is that the model *recognised* the story
— it wrote a byte-identical headline for it — and said "new" anyway. That is a decision
rule failing, not a similarity judgement failing, so the fix is in what dedup is asked
rather than in how hard it is asked.

**The first pass answers a three-way relation about one named candidate, not a yes/no about
the whole backlog.** It returns `closest_news_item_id`, a `relation` of `same_event` /
`related` / `unrelated`, and a one-sentence `reason` — the comparison, before the headline
it used to write first. `same_event` merges and `unrelated` does not. `related` is the
uncertain band, and it is the one motet#41 sat in.

**A `related` answer buys one focused pairwise re-ask, at `LlmStage.DEDUP_CONFIRM`'s depth.**
The first pass scans the whole window *and* writes a title and a summary at the shallowest
effort in the system, because it is the volume line; the second look is handed one pair and
one question. Being rare by construction is what lets it afford the thinking — and
`dedup_confirm` is a stage in exactly the sense above, a caller with its own cost profile.

**This does not move a threshold, and that is the design.** Moving one trades false merges
for false splits and both fail silently. What changes is that the uncertain band is looked
at again, and a merge still requires an affirmative answer: a second look that says no
leaves the story exactly where the first pass put it. Every failure of the second look — an
unreadable answer, an exhausted budget, a transport error — is also a "no", because a merge
is the side nothing outside the pipeline can undo. None of them raises: the first pass has
already succeeded, and a retried job would re-bill the volume call to learn the same thing.

**`motet.dedup.decisions{relation,outcome}` is what makes the design falsifiable.** The pair
is the instrument rather than either half: `related`+`merged` is the second look flipping an
answer, all-`new` says it is agreeing every time and is pure cost, and the `related` rate is
the extra spend the accuracy is bought with. Without it, "the band never fires" and "the band
fires on everything" look identical from outside — which is the never-infer-"no
errors"-from-"no data" trap, on the stage whose failures are the least visible.

**The band's size is unbounded until a real run measures it, and that is the honest state
of it.** "Rare by construction" is what the prompt asks for — it tells the model to prefer
`same_event` or `unrelated` wherever the texts allow a decision — and not something the code
enforces: there is no cap, no circuit breaker and no cheap pre-filter. A model that hedged on
most items would roughly double dedup's call count at a higher effort, which is why the
metric above is a prerequisite of the design rather than decoration, and why
`MOTET_LLM_EFFORT_DEDUP_CONFIRM` and `MOTET_LLM_MODEL_DEDUP_CONFIRM` exist. Second-look calls
carry no window, so each one is a fraction of a first-pass call's input.

**The second look may merge into an already-*read* window item, and that is decided rather
than overlooked.** The rule one section up is that a *model-driven* merge gets that reach —
it is what the window is for — and a string match does not, because it is not a judgement
about two texts. This is a judgement about two texts, made on one pair at more depth than the
pass that produced the uncertain answer, so it sits on the permitted side of exactly that
line. It is also the only side the seam admits: `NewsItem` carries no `read_at`, and giving
the inference layer a read-state opinion would put episode policy in the wrong layer.

**What no test in this repo can tell you is whether a real model answers `related` rather
than `unrelated` on the AP piece.** Invariant 7 keeps vendors out of CI, so what is pinned
offline is the decision procedure — the real adapter over a scripted `FakeLlmClient`, in
`inference/tests/test_adapters.py::TestTheSecondLook` and end to end through a real Postgres
in `workers/tests/test_pipeline.py::TestThreeWriteUpsOfOneStory`. The prompt half is
argued rather than measured, and a real staging run is the evidence that should revise it.

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
completion, a `replace_segments` racing whatever TTS was reading, and a second TTS job for
an episode that already had one. That is motet#50, and it is the module docstring's own
idempotence contract being broken by the one handler most expensive to re-run.

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

**A state check cannot tell a stale job from a deliberate retry, and that was the
residue.** A stale `script` row can outlive its own episode's failure and replay a
`failed` episode through the full stage, clearing the `last_error` on the way (motet#55);
a *slow* script job reclaimed while the first worker is still running it produces two full
renders, and there both workers read the episode in `scripting` (motet#53). Neither is
closed by a state check, and neither should be papered over with a wider one — they want a
fence on the job, or a lease that heartbeats. Both are below: first the lease, then
the fence.

### A lease is a claim about liveness, not a guess at how long the work takes

`motet_workers.jobs.touch`, `motet_workers.loop._hold_lease`. `STALE_LEASE_SECONDS` was
written to be "longer than the slowest stage can legitimately take" — but the slowest
stage's size is the *user's backlog*, and no constant is longer than something unbounded.
A script job ran 2580s against a full one, a second worker took the row while the first was
still working it, and the whole stage ran twice: a 22k-token script completion and a
complete Cartesia synthesis, billed twice for one episode, with the second render silently
overwriting the first at the same object key. That is motet#53. It only appeared once the
always-on worker fleet landed — before that an expired lease was picked up by nobody
rather than within seconds — so it is an interaction between two
changes that were each right.

**So the lease now measures silence rather than elapsed time.** A worker touches
`locked_at` every `LEASE_TOUCH_SECONDS` from a thread of its own while its handler runs, and
the reclaim arm still asks the same question it always did — it just gets a different
answer for a worker that is alive. `STALE_LEASE_SECONDS` is unchanged and should not be
raised to accommodate a slow stage: that is the shape that failed.

Four things about it are the decision:

- **A thread in the worker process, and that is the liveness argument.** The heartbeat can
  only outlive the job by outliving the process, and it cannot — so a SIGKILL, an OOM, a
  revision replacement and a task timeout all still leave a row that goes stale on schedule.
  A separate reaper, or a `locked_until` the handler extends by guessing, would each have
  reintroduced the guess this removes.
- **It has its own connection, opened per touch.** The caller's is inside the handler's
  transaction for the whole of a long job, where an `UPDATE` is invisible until the moment
  it is no longer needed. Per touch rather than held, because a connection idle for forty
  minutes is one a proxy may drop.
- **The extension is bounded, and that is which failure direction this leans toward.** A
  process that is alive but *wedged* — a handler blocked forever on a socket — would
  otherwise be heartbeated forever, and its row would sit in `running` with hand-written
  SQL against production as the only recovery, which invariant 10 forbids outright. Past
  `MAX_LEASE_EXTENSION_SECONDS` the keeper stops, says so at ERROR, and the ordinary stale
  window takes over. **A wedged worker therefore costs one duplicated run — money, and
  visible — rather than a stranded episode, which costs the episode and is unrecoverable by
  any sanctioned means.** On a queue with a `serialize_key` — `integrate` and `poll` — that
  is a *visibility* claim rather than a recovery one, and the difference is worth keeping:
  the wedged worker still holds its advisory lock, so the reclaiming worker finds the key
  busy and defers, indefinitely and without counting an attempt. That predates the lease
  keeper and is unchanged by it; what the cap adds is a line naming the job.
- **`attempts` is the fence, and it needs no column** — with a precondition that has to be
  said out loud, because `claim` incrementing it is not on its own enough. `defer`
  *decrements*, so `claim → 1, defer → 0, claim → 1` is two claims of one row carrying the
  same value. What makes the fence sound is that a deferred job never starts a keeper —
  `drain` defers and `continue`s before `_run_one` — so no live worker holds a value a
  later claim can reproduce. **Moving the serialization check inside a job's own execution
  would break that silently**, which is why `touch`'s docstring says so too. Given it, a
  worker whose lease did lapse finds out instead of stamping `locked_at` onto a row another
  worker is now running and extending the duplicate it exists to prevent. It is
  deliberately not applied to `complete` or `fail`: those record work that has already
  happened, and refusing to record it would strand the row rather than protect it.
- **A missed touch is classified, not assumed.** `LeaseTouch` has three members because the
  two ways a touch can miss mean opposite things: the row is still `running` under another
  claim (a duplicate run, and the one outcome worth an ERROR), or it is not `running` at
  all — this job finished while the touch was in flight, which is a race a fleet meets
  routinely. Reporting the second as the first would put a false "the stage is running
  twice" into GlitchTip about a job that ran once. `motet.jobs.lease{queue,outcome}` counts
  all of them including `held`, because a series that exists only when something is wrong
  cannot tell "no long jobs" from "the keeper never ran".

**What this does not do is version the audio object.** Both renders wrote the same
`audio_key` and the second replaced the first, which is the quieter half of motet#53 — but
it is a *symptom* of the double run and stops happening when the double run does. Changing
how a private enclosure is keyed or signed is a different decision, near the signed-URL
path, and belongs to a human rather than to the session that fixed the lease.

### A job says whether its own work already landed

`jobs.work_committed_attempt`, written by `jobs.mark_work_committed` **from inside the
handler's own transaction** and read by `_execute` off the row the claim returns. Two
different things are called a fence within one screen of each other and they are not the
same: the **lease fence** above is `attempts`, and it answers "does this worker still hold
this job"; the **work fence** here answers "has this job's work already landed". That
placement is the entire mechanism: the column is durable exactly when the work is, so a
freshly claimed job carrying it is a replay of work that already committed, and one
without it is not — no inference from domain state, and no second opinion that can
disagree with the first.

**The window it closes is the price of the three transaction boundaries, not a bug in
them.** `_execute` commits the handler's work and the job's outcome separately because the
failure arm has no choice — `jobs.fail` is written on a connection whose work transaction
has just aborted — so a worker that dies between them leaves the row `running` with the
work durably applied. The lease reclaim then hands it to somebody, which is the recovery a
killed worker depends on and must stay; what must not happen is that somebody running the
stage again. Now they complete the row and call no handler, and the outcome is
`already_applied` on `motet.jobs.processed` rather than a silent return, because "how often
does a worker die with its work committed" is a question nothing could answer before.

**It is on the job because on the episode the two cases are identical.** A replay and a
re-script somebody asked for both arrive as a `script` job against an episode in a state
the stage may run from — which is why #50's guard deliberately let `failed` through, and
why widening it would have traded this defect for its quiet twin, an episode stranded in
`failed` with no TTS job and nothing alerting on it. On the job row they are not identical
at all: a deliberate re-script is a *different row*, with the column NULL, and it runs.

**Not a substitute for the handlers' own idempotence, and not a concurrency control.** Two
live claims of one row — a worker wedged past `MAX_LEASE_EXTENSION_SECONDS` while another
reclaims it — cannot see each other's uncommitted work, so this says nothing about them.
The lease above is what bounds concurrency; the state guards are what make a converging
re-run harmless; this is a fence against *replay*, which is the one of the three that no
amount of reading the domain object can catch.

**It sets a lock order, and that is now a rule rather than an accident: a domain row
first, then the job row.** The work fence is the only write that takes a job's own row
lock from inside a handler's transaction, and it is the *last* statement in it — a fence
written at the top would hold that lock for the whole of a forty-minute stage and block
the lease keeper, which is motet#53 with a new cause. So `_execute`'s failure arm, the
only other transaction that touches both rows, records the domain object *before* it calls
`jobs.fail`, and asks `jobs.will_retry` for the ceiling rather than reading it off `fail`'s
return value. Written the other way round, the two concurrent claims the lease bounds but
does not eliminate can deadlock on one job, and the transaction Postgres picks is not the
one you would choose.

### A terminal job row is kept for a window, and the two windows are not the same

`motet_workers.jobs.prune`, `loop.prune_jobs`, migration `0010`. Nothing had ever deleted a
job row: `complete()` flips the state to `done` and the row stays, so `jobs` grew for the
life of the deployment — one row per pipeline stage per pasted item and per episode,
forever (motet#56). That is the half of motet#49 its PR did not take; the index there
removed the *latency* consequence of an unpruned table and nothing about its growth. What
was left is storage, autovacuum work, and the footprint of every *other* index on the
table — `jobs_source_item_idx` holds every `integrate` job ever run and is walked by the
ingestion panel the SPA polls.

**The sweep rides the drain pass rather than a scheduler.** A cron entry would live in the
private infrastructure repo, splitting a one-file change across two repositories and one of
them not public. The worker is already the process with a connection, a loop and nothing to
wait for. It runs once per invocation in the one-shot Cloud Run job shape — which is what
production runs and what an enqueue starts (motet#71), so a sweep gated on a clock the
process does not have would never fire there at all — and every
`runner.PRUNE_INTERVAL_SECONDS` in the poll loop, on the first pass rather than after one
interval, because a worker restarted oftener than the interval would otherwise never sweep.

**Two windows, because the two terminal states hold different amounts of information, and
the short one is the one that fails quietly.**

| | `done` — 7 days | `failed` — 90 days |
|---|---|---|
| What the row still holds | when a stage ran, and how many attempts it took | that, plus `last_error` |
| Who else holds it | `source_items.state`, `episodes.state` | for `integrate`/`assemble`/`script`/`tts`, `si.last_error`; for `poll`, `sources.last_error`. **For `extract`, nobody** |
| The floor it must clear | `repo.INTEGRATED_GRACE`, ten minutes | the debugging window for a failure a person has not looked at yet |

**`failed` is materially longer because of one specific row.** `failure_recorders` has no
entry for `extract` — there is no domain object to mark, extraction is what writes the
`source_items` row, and `handle_poll` has already advanced the cursor past the message — so
a failed `extract` job *is* the record that a newsletter arrived and was lost
(motet#35). `list_ingestion`'s extract arm, which is what puts it on the user's screen, is
driven by those job rows and has no time bound of its own: delete one and the message does
not age off the panel, it disappears from it, with nothing anywhere saying it existed. A
quarter is far longer than anyone leaves a backlog unattended and costs nothing in rows,
because a `failed` row means five attempts were exhausted and is not the volume line.

**Be exact about what the `done` floor protects, because it is less than it sounds.** That
arm of `list_ingestion` is driven by `source_items` and joins the job *left*, so a window
under the grace would not remove the just-landed line — it would empty it, zeroing the
attempt count on the row somebody is at that moment watching. Seven days is well past that
and is chosen for forensics instead: the job row is the only record of *when* a stage ran,
and the realistic question is asked days later.

**The delete is bounded, and the bound is two numbers.** `PRUNE_BATCH_SIZE` caps the row
locks one statement takes — an unbounded `DELETE` on a queue table holds every lock it
takes until it commits, against the claim query the pruning exists to help — and
`PRUNE_MAX_BATCHES` caps the sweep, so a backlog drains over several passes instead of one
long one. Reaching the cap is not an error and is still a WARNING, because a cap hit every
hour forever is the table growing faster than this removes it — though `capped` is a
lower-bound signal rather than a count of what is left, since a batch cut short by `SKIP
LOCKED` under a concurrent sweep reads the same as a drained window.

**Autocommit is what makes the batch bound real, and `prune` refuses without it.** Inside
one transaction the batches would hold every lock to the end, which is an unbounded delete
with extra steps — and it would delete exactly the same rows, so every test about *which*
rows still passes. The single property the design rests on could therefore be dropped
without a test going red, which is why it is a `ValueError` rather than a docstring.

**`updated_at` is the age**, written by `complete` and `fail` and by nothing afterwards. A
job that went up the backoff ladder, or a script stage that ran for forty minutes, was
enqueued long before it settled — keyed on `created_at` such a row would be deleted as it
produced it.

**`motet.jobs.pruned{state}` is added to even at zero, and `motet.jobs.prune_sweeps{outcome}`
is the second instrument rather than decoration.** A sweep's whole content is deletion, so
it leaves no other trace, and "nothing was old" and "no worker has swept" would otherwise be
the same empty panel — the never-infer-"no errors"-from-"no data" trap one section up. A
*failed* sweep is a third thing again and records no rows at all, which is why it gets an
outcome of its own: the residual fault this catches is narrow but never heals — a worker
role without `DELETE` on `jobs`, a lock timeout — and each one recurs every sweep forever
while the table grows. Connectivity is deliberately not in that set, because `drain` opens
the same connection on the same pass and does not swallow. The failure is swallowed and
logged at ERROR: pruning is bookkeeping running beside work somebody is waiting on, and the
cost of skipping an hour is an hour of rows.

**In the one-shot shape the sweep runs *before* the drain**, which is the opposite of the
obvious ordering. A sweep placed last is skipped whenever a drain raises or a task timeout
ends the execution — so the invocations against the fullest backlogs, which create the most
rows, would be exactly the ones that prune none.

### One user's burst is stepped over, not claimed and deferred one row at a time

`jobs.CLAIM_SQL`'s busy-key filter, `jobs.lock_key` stored on the row by migration 0011.
**Signed off by Tadas in motet#78**, which states the approach and rejects the alternative;
see the invariant-12 note at the end of this section for why that sign-off was read as
covering it.

Invariant 6 was already enforced and is unchanged: an `integrate` job carries
`serialize_key = user_id`, a worker takes a Postgres advisory lock on it **after** the
claim, and a worker that finds the key busy hands the row back with `defer`. What that did
not bound was cost. The queue is ordered by `run_at`, so a user who pastes two thousand
items puts two thousand rows at the head of it, and **every other worker claimed and
deferred each of them in turn — two writes per row — before it reached anybody else's
work.** Each cycle is milliseconds, so it was a tax rather than starvation; the tax scales
with the burst.

So the claim now reads `pg_locks` and does not offer a row whose key is held. Four things
about how that is written are the decision:

- **A read, never a lock.** Taking the advisory lock inside the claim was the other option
  and is rejected: a function with side effects in a `WHERE` can fire for candidate rows
  `SKIP LOCKED` then discards, and the lease fence's soundness rests on "a deferred job
  never starts a keeper", which the claim-then-lock order is what guarantees. **That order
  is untouched and `try_lock` is still the correctness fence** — this is an optimisation.
  A key taken in the window between the filter and the lock still ends in `defer`, exactly
  as it did before, and a test pins that path.
- **`NOT EXISTS`, not `NOT IN`, and the failure it avoids is total.** One NULL anywhere in
  a `NOT IN` list makes the predicate NULL for *every* row, so a single unexpected
  `pg_locks` row would offer nothing on any queue — and a queue nothing is returned from
  looks exactly like a queue with nothing in it. It is the same property that makes a NULL
  `lock_key` read as *not held*: a row this migration's backfill did not reach is still
  offered, and still goes through `try_lock`. The two directions are not symmetric — one
  costs the cycle this removes, the other strands work permanently and quietly.
- **`lock_key IS NULL OR …` is there for cost, not correctness**, and it short-circuits:
  on the four queues that carry no serialization key the subplan is *never executed* and
  `pg_locks` is never read at all. `pg_lock_status()` takes every lock-manager partition
  lock to answer, which is not a thing to do once per claim on a queue that can never need
  it. `EXPLAIN (ANALYZE)` says "never executed" there and `loops=1` on `integrate`, and the
  test asserts both — the hashed subplan is what makes it once per claim rather than once
  per candidate row.
- **`pg_locks` is cluster-wide and an advisory lock is not**, so the subquery filters on the
  current database. Without it, two pytest runs on one server — each with a database of its
  own since motet#15 — would stop claiming work because the other was busy, as a flake that
  looks exactly like an empty queue.

`jobs_ready_idx` and `jobs_stale_idx` both still carry their own arm's `Index Cond`, which
is asserted rather than assumed: putting the claim back on a sequential scan is motet#49,
and the filter is a `Filter` on the bitmap heap scan rather than anything the index has to
answer.

**One behaviour changed beside the cost, and it is named in `MAX_LEASE_EXTENSION_SECONDS`:**
a wedged worker's stale row is now stepped over instead of being claimed and deferred round
and round until the process dies. The outcome is the one that constant always described —
the job does not run while another session holds its key — and the ERROR line naming the
wedged job still comes from the keeper.

#### The scaling signal counts users with work, not rows

`jobs.queue_readiness`, `motet.jobs.ready{queue}` and `motet.jobs.ready_keys{queue}`, and
`readiness` on `/v1/processing`. Depth is the wrong number for a serialized queue: two
thousand ready `integrate` rows for one user can employ **one** worker, because invariant 6
says so, and a scaler reading depth would start a pool that spends its life deferring.

`ready` is the rows that are ready *and due* — a row backing off up the retry ladder or
deferred five seconds is not work a new worker can start on. `ready_keys` is how many of
those could run at the same time: distinct serialization keys, **plus one for each row that
has no key**. The issue specifies `count(DISTINCT serialize_key)` for the serialized queues
and a plain row count for the others; this expression equals whichever applies on every
queue that exists, because a queue's rows today either all carry a key or none of them
does. What it adds is that a queue carrying both reports a number a scaler can act on
rather than a zero that reads as "no work". It is two aggregates rather than a
`count(DISTINCT)`, which cannot hash-aggregate and sorts every due row: 83 ms against 20 ms
over 20,000 rows, median of eleven, on a query the SPA reaches every three seconds while
anything is pending.

**`blocked_keys` is the third number, and it is there because the filter above took a
signal away.** A worker that met a held key used to claim the row, log `job N deferred`, and
hand it back; the churn was the defect, and that line was the only evidence anywhere that a
key was blocking work. Stepping the row over silently would leave a *leaked* lock — a
wedged worker past `MAX_LEASE_EXTENSION_SECONDS`, a session that never released — looking
exactly like an idle deployment: workers claiming nothing, `ready_keys` saying "start more
workers", and not a line anywhere. So it is counted instead, which is the same move
`motet.jobs.lease{outcome="held"}` makes. **Nonzero is the healthy case** — a key is held
whenever somebody is working it — and what deserves attention is it staying pinned while
`ready` does not fall.

**`ready` counts nothing that is `running`, so it is zero while a job is still going**, and
a scaler reading it alone would scale a pool to zero on top of one. That is why motet#78
specifies a floor of one, and the floor is the deployment's half of this signal rather than
a gap in it: this number says how much work is waiting, and `worker_heartbeats` says whether
anyone is on it.

**Every queue is reported on every pass, not only the one being drained**, and that is the
difference between a signal a scaler can close a loop with and one it cannot: a pool scaled
to zero drains nothing, would emit nothing, and could therefore never be scaled back up.
One grouped query answers for all six either way. The route carries the same numbers for
the case even that does not cover — *no worker at all* — which is why it is on
`/v1/processing`, beside the heartbeat that answers "is anything draining" (motet#38).

**`readiness` is its own list rather than fields on the heartbeat rows**, because the two
answer different questions over different sets: a heartbeat exists only for a queue a worker
has run, and readiness has to exist for every queue. Merging them would have meant widening
a shipped non-null field to nullable for no gain.

**The gauges are sampled rather than continuous, and a consumer has to know it.** They are
written once per `drain` call — where the heartbeat is written once per *claim*, because it
is a single-row upsert and this is an aggregate over every due row, and a scaler decides on
a scale of tens of seconds rather than per job. The SDK's last-value aggregation also hands
its value to the exporter and clears it, so a collection interval with no drain pass in it
exports no point at all. Read them with `last_over_time`; the gap is the honest answer,
because it is what "no worker ran" looks like.

**The deployment shape is not decided here.** One long-lived pool per queue and a connection
pooler in front of Cloud SQL are sections 3 and 4 of motet#78, they are the `motet-production`
surface, and the issue defers them to a design session under invariant 12. `runner <queue>
--poll-seconds N` already exists and is untouched; nothing here picks an instance count, and
nothing here is a new resource in the private repo.

**One constraint that session has to be handed, because this change raises its stakes:**
`try_lock` is `pg_try_advisory_lock`, which is **session-level**, and a PgBouncer in
transaction-pooling mode breaks that outright — a lock taken in one transaction stays held
on a connection handed to an unrelated client, and `unlock` may run on a different backend.
That would break invariant 6 itself, not merely this filter; what the filter adds is that
the claim now *trusts* `pg_locks` to describe reality. So section 4's answer is either a
pooler in session mode, or moving to `pg_try_advisory_xact_lock` scoped to the handler's
transaction — which is a different mechanism and its own design session.

**The invariant-12 reading, recorded as invariant 12 asks.** The filter is a change to an
existing mechanism in the job queue rather than a new one — no new deployable, datastore,
vendor, seam, protocol, stage or model call, and no resource in the private repo — but
invariant 12 names "a new mechanism in the job queue" and says "when in doubt, it counts".
The judgement taken is that **motet#78 is the design session for it**: the owner filed it,
laid out both options, rejected taking the lock inside the claim *with the reason*, and
recommended the `pg_locks` pre-filter. That is the shape invariant 12 asks for — the
problem, the options, the costs, a recommendation, an owner's choice — and the escape hatch
"a decision this file already records" is about not holding a session twice, not about
which file the record lives in. This section is now that record. Sections 3 and 4, where
the same issue says the shape "is the owner's call and needs a session before anything is
built", are left alone.

### The SPA is a shell with a URL per section, and still not a router

`web/src/shell/`. A fixed sidebar, a top bar holding the page title and the account menu,
and one path per section — `/backlog`, `/episodes`, `/sources`, `/paste`, `/admin` — kept in
React state by `usePath()`, about forty lines of `pushState` and `popstate` (motet#88). It
replaced a tab strip held in component state, which lost its place on every reload: the
shape of motet#44 one level up.

- **Not a router, and the trigger for revisiting that is named.** One string, no matching,
  no nesting, no link component. If a nested path is ever wanted — an episode id, a
  source item id — that is the moment to ask whether forty lines are still enough, not
  before.
- **`/` and any unknown path are the Backlog**, which is where "what is waiting for me" is
  answered — the Processing panel and the badge are both there. The shell then *replaces*
  the address with the section's own path (a trailing slash too), so the sidebar and the
  address bar agree and Back does not return to a path that was never a place. That is the issue gate's reading
  of the owner's open question and one constant, `HOME`, in `shell/sections.tsx`.
- **The door and `/oauth/callback` render without the shell**, and the shell does not
  rewrite the address while either is up. On the callback that leaves the address to
  `forgetCallbackUrl`; on the door it keeps a deep link, so pasting a token at `/episodes`
  opens Episodes. The door and the shell are different trees, so the API token field
  saves on submit rather than per keystroke — one that saved as it was typed would swap the
  door for the app on the first character.
- **The sidebar offers Admin only to a caller `/v1/auth/session` says is an admin**, which
  is the operator view's own rule below, carried into the shell. `/admin` is still a
  section for everybody else: typed in, it says why and asks for nobody's data.
- **A section declares its own layout** (`layout: 'reading' | 'wide'`). Wide is a screen
  made of tables, which scroll inside the content area rather than the page — a property
  of the section, not a class one screen happened to carry.
- **A screen does not title itself.** The top bar's `<h1>` is the title; each screen's
  `<section>` is `aria-label`led instead of carrying an `<h2>` that says the same word.
- **Under 800px the sidebar is a sticky bar with a Menu button** (`aria-expanded`,
  `aria-controls`). The nav is one element either way, so there is one list of sections
  and one active state.

This is structure so the screens are reachable, not the start of a design system: the
three greys and one accent exist so the active item and the badge have *a* colour, and
brand is still Phase 3. If the next SPA issue is about the shell rather than about a
screen's job, that is the tripwire above firing.

### The episode screen reflects server state, not this page's lifetime

`web/src/App.tsx`. Nothing loaded episode state on mount, so a reload — the realistic thing
to do while a multi-minute pipeline runs — emptied the tab and left a finished episode
reachable only through the RSS feed (motet#44). **The shape of that bug is that the longer
an episode takes, the more likely it is to be lost**, and the first one is the slowest
because the backlog is fullest.

**The section is a shelf of every episode, and the detail is a click in** (motet#89).
`web/src/screens/Episodes.tsx` lists them newest first, grouped into *Up next* (unlistened,
in progress, still being made, failed) and *Listened* (folded past five), and opens the
existing detail under a back link. Before it, the section *was* one episode's detail, seeded
with the newest, and the only way to another was an inline "Other episodes:" line.

- **Listened is derived, not stored** — `listenState` in `screens/listening.ts`, from
  `listened_through_ms` against `duration_ms` with five seconds of slack for the sign-off.
  That is option (b) of the issue's question 2, and it accepts by design that a row's
  verdict can disagree with its stories' read state (invariant 5): tick every story off on
  the Backlog and the row still says Unlistened; un-read one and it still says Listened,
  because the position cannot go down. A read flag on the segment response is options (a)
  and (c), and they are the owner's call rather than a refactor.
- **Mark listened writes both facts, in order, and is one-way**: `POST …/listened` (every
  story read), then `PUT …/position` at the duration. There is no "mark unlistened"
  because there is nothing to write — un-listening could only mean un-reading.
- **App holds which episode is open, as an id, and the list is the only copy.** The section
  unmounts whenever another is showing, so "which one was I looking at" has to live above
  it. No path segment: the shell's note above says a nested path is the moment to revisit
  forty lines of `pushState`, and App state answered the question without that.
- **The list rides the backlog's refresh**, merged rather than assigned — an episode
  created while the request was in flight is kept, the position is the larger of the two
  copies, and which episode is open is never touched. So the shelf and the detail fetch
  nothing themselves, and there is one poller. The refresh polls while any episode is in
  the pipeline, not only an open one.
- **The landing is the shelf, except the first time the section is shown while an episode
  is still being made**, which opens that episode's detail (question 4): its Working… copy
  and "not moving" banner live there, and a reload mid-render is the realistic way to
  arrive. It is judged once, when the section is first shown rather than when the list
  first arrives — a render that finished while somebody was on the Backlog is a shelf, not
  a detail — and a later refresh never moves the screen.

### The episode detail has a player, which reverses a Phase 1 decision

`web/src/screens/EpisodeScreen.tsx`, motet#89. **The owner's go for shipping it came with
the #87–#95 batch, relayed by that batch's release orchestrator (Zimmer session 17607)**;
the issue gate left "fix the player or relabel the pill *Open*" to the implementing PR,
and #101 records the call. Phase 1 shipped RSS *instead* of a player. That reason still
holds for the walk, so the feed URL is still offered on the detail; the player is for a
desk, with the transcript beside it.

- **An `<audio>` pointed at the audio route, never a `fetch` into a blob.** A deployed API
  answers `GET /v1/episodes/{id}/audio` with a 307 to a signed URL on the object store's
  origin; a media element follows that without CORS and gets range requests from the
  store, while a `fetch` would need CORS on the bucket, which nothing grants. The prototype
  fetched a blob because the local backend serves no `Range` — so locally, seeking is
  limited to what has buffered, and that is the dev path only. The route takes the feed
  token in the query because a media element cannot send a header.
- **It resumes from `listened_through_ms` and writes `PUT …/position`**, the position
  resource a syncing player wants, so listening here moves the shelf and marks stories
  read as their segments pass. It is the first client to write the position from real
  playback — iOS keeps its own and RSS clients cannot report. It reports every ten seconds
  of playback and flushes on pause, on the end and on leaving the screen; a refused report
  is not retried per tick, because the next one carries the same frontier.
- **Only continuous listening from the frontier already heard moves the position.** The
  server marks every story the position has *passed*, so a reported position is a claim
  about everything before it. A tick counts only while playing, only as a step of five
  seconds or less (a seek's echo is a jump), and only when it starts at the frontier — so
  scrubbing, a ▶ jump to a later story, and playing on from past a skip all leave the
  skipped stories unread, and `ended` counts only when the frontier had reached the last
  step. That is the iOS player's `maxListeningStepMs` rule fitted to a route that knows
  positions rather than coverage. The cost is the safe direction: after a skip, Resume
  lands back at the skip, and Mark listened is how to say the rest was heard.
- **The shelf's Play pill starts playback; a row click only opens.** A rejected `play()`
  is the browser's autoplay policy, and the controls are right there. Safari usually
  refuses — `play()` runs after the feed token and the metadata arrive, outside the click —
  so there the pill behaves as *Open*.

### Play Live is built, and dormant until a voice service is deployed

`voice/` (`live.py`, `position.py`, `realtime/`), `motet_api.voice`, `web/src/screens/Live.tsx`,
motet#93. **The owner chose the OpenAI Realtime arm over a batch STT leg on the composed arm,
with "do nothing" named, in the 2026-09-12 prototyping session** — the issue records the
alternatives. That choice is what this builds; **the design session invariant 12 asks for
is still owed**, and the questions it has to settle are listed at the end of this section
with the default each currently runs on. None of the defaults is a decision.

**Nothing is deployed, and that is the state this ships in.** Staging and production run
the API, the worker and the web app and no voice service; deploying one is its own
sign-off. So `MOTET_VOICE_BASE_URL` and `MOTET_VOICE_START_SESSION_TOKEN` are unset there,
`GET /v1/voice` answers `configured: false` with the reason, and the episode screen shows
a disabled **Play Live** with that sentence beside it — no request to a host that does not
exist, and `POST /v1/episodes/{id}/voice-session` is a 503 rather than a 500 for anyone
who calls it anyway. Turning it on is configuration, listed in the PR.

- **The API mints the session, never the browser (invariant 2).** The prototype's browser
  assembled `SessionContext` and called the voice service directly, which worked only
  because the start token was unset. Now the API builds the context from the database —
  every segment, every claim with its apportioned `start_ms`/`duration_ms` (now on
  `ClaimModel` too), the listener's position — calls `StartSession` server-to-server with
  the start token, and returns a socket URL and the `authenticate` frame to send on it.
  The browser never holds the start token and never names a vendor (invariant 1).
- **Interruption is decided locally, on every arm, from every packet.** The session's
  detector sees each listener packet *first*, including while the live channel is
  forwarding, and a decision emits `interrupted_at` before the vendor is handed the
  position and the pre-roll. The session's detector decides the interruption of
  narration (invariant 4: the frozen clock is ours); the vendor's server VAD governs only
  the reply turn. A vendor `speech_started` is `live_speech_starts`, never a barge-in;
  a client `barge_in` frame is one, and is counted.
- **`EnergyVad` tells silent, quiet and measurable frames apart**, and that is the fix for
  the owner's `barge_ins: 0` sessions. A browser mic under echo cancellation delivers
  -60 to -70 dBFS between words, never zeros; treated as silence, the floor seeded from
  the listener's own voice and nothing could fire. A quiet frame now seeds the floor at the
  absolute floor and walks it down. `listener audio: … route=… snr_db=…` every ten seconds
  of mic audio, and `barge-in: …` per decision, are what make the next such session
  diagnosable from the obs stack.
- **Listener audio reaches the vendor only from a barge-in to the end of the reply** — the
  spend gate, since realtime audio is billed per token in and out. `narration_resumed`
  (and a paused player's `narration_paused`, which is *not* a barge-in) are the contract
  frames that close it: "never mind, resume" cancels the reply and stops forwarding rather
  than billing the briefing. A floor nobody speaks into closes itself after 30 seconds, and
  a *typed* question owes a reply without opening the mic at all. **A tool call's answer is
  a second response**: the first one's `response.done` arrives after the tool has already
  run, so it must not end the turn — that was the one bug the PR review found that shipped
  billed audio nobody could hear. `motet.voice.realtime.tokens{arm,kind}` and
  `motet.voice.realtime.replies{arm,outcome}` are the cost as metrics (invariant 11);
  `input_audio` growing while replies do not is the gate leaking.
- **A reply the listener talked over is truncated, not recorded as spoken** — in the
  vendor's history (`conversation.item.truncate`) and in ours — by how much of it could
  have played.
- **A live channel that opened and died is reopened**, at the next barge-in or question
  and never on a timer, at most twice a session, carrying the conversation so far — and
  never for `arm_dormant` or `insufficient_quota`, which a reconnect cannot fix. A typed
  question with no channel still goes to the composed arm.
- **The fake-mode realtime arm has a fake live channel** (`realtime/fake_live.py`), so the
  whole loop can be felt in a browser for free: `bin/dev --voice` with
  `MOTET_VOICE_ARM=openai_realtime`. Real mode without `OPENAI_API_KEY` is dormant and
  says why; `MOTET_VOICE_ARM` still defaults to `composed`.
- **The socket is the one cross-origin surface, and it checks `Origin`**
  (`MOTET_VOICE_ALLOWED_ORIGINS`). `StartSession` has no CORS policy on purpose: only the
  API calls it.

Deliberately not built, each for a stated reason: a **startup probe that the realtime key
is billable** is a vendor connection per instance start and belongs with the decision to
deploy the service at all; **streaming the composed arm's reply** (2–5 s of silence today)
waits on whether the composed arm is the default.

**Open for the design session, with what runs today:** the default arm given realtime cost
(`composed`); open mic relying on browser echo cancellation vs headphones (open mic,
headphones recommended on screen); reply length (one or two sentences, by prompt); resume
at the interruption offset vs a rewind (the offset); the batch-STT comparison (not built);
whether talking over a reply is natural or rude (allowed).

### The RSS feed is the seam to the ears, and podcast clients are stricter than the spec

`api/src/motet_api/feed.py`. RSS is Phase 1's listening surface *instead of* an in-app
player, because a browser has no background audio and no offline and a dog walk needs both.
The SPA has had an in-page player since motet#89, for a desk; the walk is still this.

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
cursor, and fetch one message's raw RFC 822 bytes — plus, since motet#96, list the labels
and move one message between them (the section below). A narrow interface is what makes the
fake honest; a fake that had to model Gmail's search syntax would be a worse Gmail rather
than a better test.

**Every listing is a `messages.list` search carrying the source's filter, first sync or
not** — `(<filter>) after:<epoch seconds>`, paged with `pageToken`. Incremental polls used to
ask the history API, which is cheaper and has no `q`, so from the second poll on everything
added to the mailbox was ingested whatever the filter said (motet#95). A watermarked search
costs the same one request for a quiet mailbox, and it is the only shape that can honour an
arbitrary Gmail query; resolving a label to a `labelId` for history would have served one
kind of filter and special-cased the rest.

Four things about it are the design, and each is a failure the first version had:

- **A pass is followed to its end, and the cursor never moves past a page nobody read.**
  The first sync used to read one page of 50 and then set the cursor to the mailbox's live
  history id, so a 170-message backlog became 40 items and nothing said so (motet#94). The
  cursor now holds the pass's `nextPageToken` for as long as there is one; only an exhausted
  pass moves the watermark, and only to where that pass *began*, less an hour. "Fewer than
  50 came back" proves nothing — Gmail pages can come back short with more behind them — so
  the token is the only end-of-search signal read.
- **A run is bounded; the search is not, so the run re-arms.** `handle_poll` stops paging at
  `POLL_PAGE_SIZE` queued or `MAX_PAGES_PER_POLL` read, records where it got to, and — only
  while the search has pages left — enqueues the next poll with no delay, which the same
  drain then claims. That is the existing worker-side re-arm the drain-trigger section
  describes, keyed on "more pages" rather than on an expired history id, and the chain ends
  on the run that finds no further page. **This is why a first connect's total extraction
  spend went up**: it used to stop silently at one page, and now the whole window drains.
- **The overlap is deliberate, and the pre-check is what makes it free.** The watermark
  trails the pass start by `WATERMARK_OVERLAP_SECONDS` because the pass start is this
  process's clock and Gmail's index lags arrival. What the overlap re-lists is dropped before
  a fetch by `phase2.unqueued_message_ids`, which counts an `extract` job in *any* state as
  "already handed on" — without that half, a receipt extraction skipped has no source item
  and would be fetched again on every poll inside the overlap.
- **The window is a fact on the source, not a constant in the adapter.** A first sync is
  bounded to `MOTET_GMAIL_FIRST_SYNC_DAYS` (default 7), and the run that starts one records
  the value in `sync_state.first_sync_days`. Each run records `sync_state.last_sync` (`at`,
  `seen`, `queued`, `caught_up`, `error`); a poll that gives up after its retries writes its
  reason there and on `sources.last_error` through the `poll` failure recorder, because its
  own transaction is the one that rolled back. `SourceResponse` reports `query`,
  `first_sync_days` and `last_sync`; the cursor stays the adapter's and is never reported.

**A cursor the adapter did not write is a bounded first sync, not an error.** A source
connected before the change carries a history id; re-reading its window is the repair, and
it recovers what that source's first sync dropped for as far back as the window reaches.
Two things this still does not do, both inherited from the history version rather than
introduced: a message that *starts* matching the filter after it arrived — a label added a
week later — is older than the watermark and is not listed, and Gmail offers no
oldest-first search, so across the pages of one long pass the newer page is ingested first
(within a page the order is still oldest first).

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

**`/oauth/callback` is read once at boot, apart from the shell's paths.** Google hands
consent back by navigating to a URL, so the browser arrives with a fresh page load and no
memory of the app it left. `web/src/oauth.ts` reads `window.location` once at boot and
`App.tsx` renders the callback instead of the shell. Finishing hands over to `/sources`
(a mailbox) or `/` (a sign-in) with `replace`, so the callback's own history entry
becomes the section and Back cannot return to a spent code. The section paths are the
shell's and can change freely; this one cannot.
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

#### Label sync is the connector's one write, and it is opt-in per mailbox

`motet_workers.labels`, `motet_sources.labels`, `PUT /v1/sources/{id}/label-sync`, `POST
/v1/sources/{id}/reauthorize`, migration 0014. The owner's mailbox workflow is
`Newsletters → Completed`, and ingesting an item into Motet *is* reading it — so a Gmail
source can carry two optional label names, one to remove and one to add, applied to the
item's message when the owner deliberately ingests it (motet#96). The names are personal,
so either may be empty.

**The scope is the design, and the gate on it is structural.** Connecting still asks for
`gmail.readonly` and nothing else. `gmail.modify` — which could also archive or trash mail —
is asked for by exactly one route, `reauthorize`, which refuses with 409 until the source
has labels set, and which binds the consent to that one source. So a mailbox that never
turns label sync on is never shown a consent screen that mentions changing mail and never
holds a grant that could; `api/tests/test_label_sync_api.py` pins the connect URL's scope as
*exactly* readonly rather than merely including it. The worker then asks the stored grant,
without decrypting anything, whether it carries the scope, and a read-only grant is recorded
as `needs_reauthorization` with no Gmail call at all. Setting labels is a setting, not a
consent: it widens nothing. **No authorization sends `include_granted_scopes`**, which is
what keeps the opt-in per *source* rather than per Google account: with it, Google folds
every scope the account ever granted this OAuth client into each new token, so connecting a
second mailbox on an account that had re-consented another would hand the new source
`gmail.modify` unasked. The callback also records only the granted scopes that authorization
asked for, and the worker decides from the recorded scopes, so a provider that widened a
token anyway still cannot make a source act writable. **Turning label sync off stops every
write and does not narrow the grant** — disconnecting forgets the token, and only revoking
Motet in the Google account withdraws Google's side of it.

**A re-consent is checked against the mailbox the source already is.** Re-authorizing
replaces an existing source's grant, and Google's account chooser returns whichever
account was picked. So `sync_state` records `mailbox_address` and `mailbox_verified_for` —
the refresh grant's `updated_at`, which every consent rewrites — and before a poll or a
write uses a grant it has not been checked for, `ingest.check_mailbox` asks Gmail which
mailbox that grant reaches. **Bound to the credential, not to a flag a consent sets**, so a
stale `sync_state` write can only cause a re-check, never skip one; and asked **with a token
minted from the grant stored now** (`ingest.mint_token`), because an access token from the
previous grant stays valid for up to an hour and would answer for the old account. The same
function will not *store* a token whose grant was replaced while the refresh was in flight —
it re-reads the grant's stamp after the network call and retries instead — so no caller,
extraction included, can cache a token for a grant that is gone. A rotated refresh token
keeps the scopes its grant was recorded with, and carries the check's stamp with it. A different address **disconnects** the source — its credentials are the other
account's, so they are deleted, the source is paused, and `last_error` names both addresses —
rather than reading one inbox under another's cursor; a profile naming no address blocks
rather than passes. The `reauthorize` URL carries a `login_hint` for the recorded address
as well; that is convenience, and the check is the control. The residual gap, stated: a
source re-consented before any poll recorded its address has nothing to be compared with,
and records whatever it sees.

**Only a deliberate ingest writes, and that is a flag on the job rather than an inference.**
`handlers.enqueue_integration` — "Ingest now", the held-item claim above — is the one
writer of `deliberate: true` on an integrate job; `labels.schedule` registers a write only
when the flag is set, the source is Gmail, and it has labels. Absent — as on a paste's job,
or one extraction queued before the ingest gate — means no write. That makes a poll, an
extract, a stale job, a paste, and any future "always ingest from this sender" unable to
reach a mailbox by construction rather than by which enqueue paths happen to exist. A poll does *read* the label list under the readonly scope, so the pickers
have names to offer — only when the cached catalog is missing or a day old, so a mailbox
that never uses label sync pays one read a day — plus one profile read per consent.

**After the commit, never inside it.** The write-back runs from `Context.after_commit`,
which `_execute` hands back once the dedup transaction *and* `jobs.complete` have committed,
and which `drain` runs only after releasing the job's serialization lock. So a Gmail outage
cannot roll back an integrate, a slow Gmail call holds neither the dedup transaction's row
locks nor that user's next integrate job, and an integrate that rolled back never moved a
label for a story that does not exist. After `complete` rather than between the two
commits, because that window is the one the work fence exists to cover and a network call
inside it would widen it. The Sources screen counts a failure only if it was recorded
since the mailbox was last authorized (`source_items.label_attempted_at` against the refresh
credential's `updated_at`), so "re-authorize" does not outlive the re-authorization. **It never fails the job**: every outcome is written to
`source_items.label_synced_at` / `label_error` and counted on
`motet.gmail.label_writeback{outcome}` — `applied`, `needs_reauthorization`,
`label_not_found`, `message_not_found`, `failed` — which is what makes "it silently stopped
working" visible. A Gmail outage logs at WARNING; only a bug in the step is an ERROR.

**Names are resolved to ids through a cache the poll keeps.** `sync_state.label_catalog`,
written by the poll and merged — one key, never the whole document, because the write-back
runs under the user's key and can overlap a poll that owns the cursor. A name the cache
does not have is re-read once; a modify Gmail answers with 400 or 404 is re-resolved and
retried once, which is how a label deleted and recreated under the same name recovers.
**Only four system labels may be written** — `INBOX`, `UNREAD`, `STARRED`, `IMPORTANT` —
refused at input by the settings route, again by the resolver (by id, whatever the cached
catalog claims), and a third time by `GmailMailClient.modify_labels` before any request, so
no row or bug upstream can move a newsletter into `TRASH` or `SPAM`. Removing `INBOX` is
archiving, which is reversible from All Mail, and is the `Inbox → Archive` workflow.

**Un-ingesting does not put the label back, and that is decided rather than deferred by
accident.** There is no un-ingest to hang it on, and a dismissed held item was never
written to. A restore would also need to know what the message carried *before* — a blind
re-add of `Newsletters` could add a label the message never had — and nothing records that
today. If it is ever built, the modify response's `labelIds` is where the before-state comes
from.

**What no test here can tell you is whether Google grants it.** `gmail.modify` is a
restricted scope, like `gmail.readonly`, and the fake OAuth provider grants whatever a test
tells it to. Whether the OAuth client's consent screen has to list the scope before Google
will offer it is a one-time human-owned step (invariant 9), and the first real re-consent
is the owner's click.

**The invariant-12 reading, recorded as invariant 12 asks.** Two methods on an existing
Protocol, two columns on an existing table used the way `last_error` already is, a key in
each of `config` and `sync_state` used as they already are, a step inside an existing
handler, and two routes on the existing API: no deployable, datastore, queue, vendor or
resource in the private repo. The one piece that touches the job runner is
`Context.after_commit`, and the judgement taken is that **motet#96 is its design session**.
It is written as a list any handler could append to, which is wider than the one step that
uses it; `handle_integrate` is its only caller, and a second caller is the moment to ask
again rather than a use this reading already covers:
the owner's issue specifies "a post-commit step at the end of `handle_integrate` — after the
dedup transaction commits", names the alternative (a `label` job) and rejects it as "a new
mechanism in the job queue … not worth one API call". A post-commit step needs somewhere
after the commit to run, and that list is the narrowest such place: not persisted, not
retried, not a queue, discarded when the handler fails. A worker that dies between the
commit and the step leaves the message where it was, with neither column set — the cost of
not being a job, stated rather than discovered.

### The Sources screen is a catalog, and a source row says what it did

`web/src/screens/Sources.tsx`, `web/src/screens/sources/` (motet#90). A card per
integration — Gmail, Paste, and two honestly disabled "Coming soon" — and one panel under
the grid for the account(s) behind the one you pick. **The catalog is static**, because
`GET /v1/sources` lists *accounts* and something not yet connected has no row to render.

The last sync's result, the filter and the first-sync window are motet#94's fields and are
read, not re-derived: "Sync now" watches `last_sync.at` rather than `last_polled_at`,
because an extraction that skips a message moves `last_polled_at` too, and watching it
would call that a sync. It re-fetches on an interval while it waits — a watch re-armed
only by a change stopped at the first unchanged answer.

Two things the API grew for it are decisions rather than fields:

- **`sources.disconnected_at` (migration 0016) is what separates a disconnected mailbox
  from an abandoned consent**, which are otherwise the same row. The disconnect route sets
  it only when it actually deleted a credential, so "disconnecting" a row that never held
  one cannot turn it into a mailbox nobody may dismiss.
- **`DELETE /v1/sources/{id}` dismisses an abandoned consent and refuses everything
  else.** The delete cascades to source items and on to the claims and highlights citing
  them, so every guard in `phase2.remove_unused_source` is a data-loss guard: another
  user's row is a 404, and a 409 answers the paste source, a stored credential, any sign
  one was held (`disconnected_at`, `last_polled_at`, `active`), and any source item. The
  row is locked `FOR UPDATE` first, because a concurrent callback's credential insert
  holds a key-share lock the delete must wait for — checked without the lock, the
  credential would be cascaded away with the row. **The consent's `oauth_states` rows are
  then taken `NOWAIT`**, because a callback mid-exchange holds that row from its consuming
  `DELETE` and next wants the source row: waiting would be a deadlock, and the aborted
  side may be the one holding a spent authorization code. A held row is a 409.

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

### The operator view is the one read across users, and only a listed person gets it

`GET /v1/admin/overview`, `deps.require_admin`, `web/src/screens/Admin.tsx` (motet#87).
Invariant 10 is the reason it exists: with no database shell, the queues, per-user counts
and failing jobs have to be a screen or nowhere. **It is the first route that returns
data across users** — every address, every count, every job's `last_error` — which makes
the check in front of it the part to get right.

- **`MOTET_ADMIN_EMAILS` narrows the sign-in allowlist; it is not a second door.** Same
  format, parsed by the same function in `motet_db.allowlist`. An admin is a *session*
  whose address is on both lists — `require_caller` has already revoked a session whose
  address left `MOTET_ALLOWED_EMAILS` before the admin list is consulted. One flag, not a
  role system; if it ever grew into one, that would be invariant 12's business.
- **Unset or empty means nobody, and the shared API token is never an admin.** The token
  belongs to no person, so "unset means nobody" could not be literally true if it passed;
  an open deployment (`MOTET_API_TOKEN` unset) is refused for the same reason. A refusal
  is a 403, never a 401 — a 401 tells the SPA its session is dead.
- **`/v1/auth/session` reports `admin` from the guard's own predicate** (`deps.is_admin`),
  so the SPA links to `/admin` for exactly the callers the API would answer. That is
  presentation; the 403 is the control.
- **Every `/v1/admin` route takes `Admin`, and a test walks `app.routes` to prove it.** Not
  an `APIRouter` with a router-level dependency: this FastAPI mounts an included router as
  one opaque `app.routes` entry, which every route walk in the repo — the reserved-path
  guard included — would silently stop seeing. The walk fails if one appears.
- **The job list is a keyset page on `jobs.id`**, 200 by default and 500 at most, with
  `jobs_next_before` as the cursor. Not an offset, because workers insert at the head of a
  list the screen polls; not a time window, because one Gmail backfill puts a thousand rows
  into the last hour. The aggregates are always for everyone; job → user is a join on the
  payload per queue kind, fine while `jobs.prune` bounds the table.

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
