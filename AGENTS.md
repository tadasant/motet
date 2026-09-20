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

Deploy workflows live in the private repo — with one exception, the TestFlight upload, which
lives here for its free macOS runner, runs on every merge that touches `ios/`, and is
fenced to `main` (see [Runner policy](#runner-policy)). CI in *this* repo runs on the shared
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
   same fact as having listened past it in an episode. The one exception is an episode
   made with `keep_in_backlog`, whose listening marks nothing read (Tadas, 2026-09-19; see
   "A picked episode is the same selector with one more knob").

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
(`motet-api`, `motet-worker`, `motet-voice`) because that label is what an operator filters on.

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
being deployed does not change that. The image pin lags this repo's `main` until a bump PR
in the private repo merges — `notify-deploy-pin.yml` asks for one on every push to `main`,
but merging here still deploys nothing: a route merged here is not a route serving there, and
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

**A picked episode is the same selector with one more knob** (Tadas, 2026-09-19: "select
a few items … and generate an episode based on JUST those selections", from the iOS
backlog). `POST /v1/episodes` takes an optional `news_item_ids`, and the route stores it as a
smart episode whose rule is `SmartRule.picked` — only those ids, read or not, no window,
oldest first — so assembly, the duration cap and the script stage are exactly an ordinary
episode's. The ids are checked at creation: one that is not the caller's is a 422 that says
how many, never which, and nothing is created.

**`keep_in_backlog` is invariant 5's one deliberate exception, and it is per episode.** The
owner asked to generate "without necessarily dismissing the entries", and nothing
dismisses at generation — listening does. So `episodes.keep_in_backlog` (migration 0023)
means *listening to this episode* marks nothing read: `record_listen_progress` still moves
the position and returns zero marked, `POST …/listened` marks nothing, and the iOS player,
which writes read state per story itself, skips both writes when the episode says so. The
same story heard in any other episode is still read. **Off by default**, on the request and
in the app's sheet, because off is what every episode has always done; the whole-backlog
request is byte-for-byte unchanged. On the response it is optional-with-default rather than
required, so a TestFlight build decoding an older API — or its own offline cache — still
decodes. The invariant-12 reading: a knob on an existing rule, a column on an existing table
used the way it already is, and two fields on an existing route — no new mechanism.

**A rule is stored as a snapshot on the episode**, not referenced from a rule table. An
episode is a historical artifact, and "why does this contain these stories" has to stay
answerable after the rule is edited.

**Read state from the audio side is `episodes.listened_through_ms`.** It is monotonic in the
repository layer — a client that seeks backwards is reviewing, not un-listening — and its
job is deciding which news items are read (on a `keep_in_backlog` episode, only where to
resume), so listening past a story on a walk and
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
distinction is the one thing to get right when wiring a client to it. The iOS app keeps
its playhead, which moves backwards when the listener seeks back, on the device, and reports
the end of the listening that is unbroken from the server's value (`ListenedCoverage.frontier`)
— never the playhead, and never a point past a story it skipped. That is
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
and the iOS app. The voice/interaction path is built, and **dormant** in any
environment that has not wired a voice service to the API — see "Play Live" below.

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
| Landing page (getmotet.com) | static HTML/CSS, Cloudflare Pages | `site/` |
| Voice service | Pipecat, Cloud Run — **Phase 2** | `voice/` |
| Agentic enrichment | FastAPI + Pi + Chromium, Cloud Run | `enrich/` |
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

`bin/build-images` builds and smoke-tests the four container images, and it is its own
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

Cloud Run runs five: `motet-api`, `motet-worker`, `motet-voice`, `motet-web`,
`motet-enrich`. The first four of those come from one root `Dockerfile` with four targets
(`api`, `worker`, `voice`, `enrich`), because a second Dockerfile would be a second copy of
one dependency graph. The SPA is `web/Dockerfile`.

```bash
bin/build-images              # all five, then smoke-test each
bin/build-images api web      # a subset
```

**`motet-voice` is built from the same lockfile but is not the same tree.** Its stage runs
`uv sync --package motet-voice --no-editable`, which leaves the venv holding only the voice
service's dependency closure and copies nothing else, so `motet_db` and `psycopg` are not
in the image at all — invariant 2 as a property of the artifact, which the smoke test
asserts. The smoke also mints a session on one container and opens its WebSocket on a
second that shares the secret, because Cloud Run gives a socket no affinity and a secret
that differs between instances fails only there.

**`motet-enrich` is `motet-voice`'s argument one service along, and it is the one target
not built on `base`.** Its venv is `uv sync --package motet-enrich --no-editable`, so
`motet_db`, `psycopg` and `motet_vault` are absent from the image — design option D2 as a
property of the artifact, which the smoke asserts. It is built on
`mcr.microsoft.com/playwright:v<version>-noble` rather than the slim Python image because it
needs a Chromium and the hundred-odd shared libraries one needs, and the tag is pinned to
the Playwright version `enrich/harness/package-lock.json` resolves: a mismatch is
`Executable doesn't exist at /ms-playwright/…` on the first real fetch and nowhere earlier,
so `enrich/tests/test_toolchain_pin.py` reads both files. Its smoke drives the browser MCP
server for real inside the container — a page rendered, an off-site navigation refused, the
storage state written — because those are claims about a running Chromium and a unit test
can only assert the predicate behind them.

**Both build contexts are the repo root**: `uv.lock` describes the whole workspace, so a
context rooted at `api/` could not resolve it.

**This repo builds images and never pushes them.** It is public and holds no cloud
credential of any kind — no GCP identity, no registry login, nothing to leak. (Its two
credentials of any kind are the App Store Connect key behind `testflight.yml`, which reaches
Apple and nothing in the infrastructure, and the dispatch token behind
`notify-deploy-pin.yml`, which needs Contents: write on the private repo to send its
notification and is fenced to `main` accordingly; see [Runner policy](#runner-policy).)
Publishing
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
profile and no App Store Connect key — and neither does the unsigned device archive it also
runs (`ios/bin/testflight check`). That is the property to preserve: adding signing, a
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

**The `ios` job also *runs* the app now, and that is the only place in this repo anything
does.** `ios/bin/ui-test` boots a simulator, plays a locally generated tone through the
shipping player, and asserts the app's own measurement of whether audio came out (see "The
iOS app measures its own audio" below). It rides the existing macOS job rather than a
second one: the runner is already up and Xcode is already warm, so what it adds is a
simulator boot plus one build-and-run — roughly 5 to 10 minutes — where a job of its own
would have doubled the iOS cost at the higher multiplier for the same coverage. It is
gated by `ios-changes` like everything else in that job, and its video, screenshot and
result bundle are uploaded on `always()`, because a red run is the one nobody here has a
Mac to reproduce.

**`ios-ui-tests.yml` is the same script behind a `workflow_dispatch`, and it is the entry
point an agent can start.** It exists for the case CI cannot cover: pointing a build at a
server this repo does not know about. It holds **no credential** — a simulator build needs
no identity, which is what separates it from the two workflows below — and it names no
host: the server arrives as the *name* of a repository variable, and the value is masked
out of the log. Its guard is not `ci.yml`'s: there is no `pull_request` trigger, so that
expression would be false forever; what it carries instead is the repository check, and
`pull_request` / `pull_request_target` must never be added without the fork guard coming
with them.

Deploy workflows are a different matter — they live in the private repo, **with one
exception: `testflight.yml`.** Asked for by Tadas on 2026-09-13 ("get it into
TestFlight"), in Zimmer session 17604, and built in session 17805. It is here rather than in
the private repo for the reason the `ios` job is on a hosted runner: macOS minutes are free
on a public repo and billed at a multiplier on a private one. It is one of two workflows in
this repo that hold a credential, so it is fenced four ways and **all four have to stay**:

1. **A push to `main` and `workflow_dispatch` are its only triggers.** No pull request —
   from a fork or a branch — starts it, a fork can neither push to this `main` nor dispatch
   here, and `pull_request` / `pull_request_target` must never be added.
2. **The job refuses any ref but `main`** and any repository but this one.
3. **The key is an environment secret, in `testflight`, whose deployment-branch policy
   admits `main` only.** That is the fence that survives a branch editing (2) away: GitHub
   withholds an environment's secrets from a job on a ref the policy does not admit.
   Never move them to repository secrets.
4. **It runs on a GitHub-hosted, ephemeral runner**, never the self-hosted pool, where a key
   written to disk would outlive the job on a shared machine. Its one action is pinned by
   commit SHA, because a moved tag would run inside the job holding the key.

**The invariant-12 reading, recorded as invariant 12 asks.** The owner asked for the outcome
(a TestFlight build), not for this placement. The alternatives, and why they lost:
the private repo on a paid macOS runner (the same workflow at a multiplier, for a key that
reaches no infrastructure); Xcode Cloud (its setup needs Xcode on a Mac, and no Mac exists
anywhere in this project); and uploading by hand from Xcode (the same missing Mac, plus a
human in a routine operation, which invariant 9 calls a defect). Merging the PR that added
it is the owner's choice of this option, so a reversal is a move to the private repo, not a
rewrite: the script is the workflow's whole body.

What it holds is one App Store Connect API key (Admin role, so Apple's cloud-managed
distribution certificate signs at export and there is no .p12 or profile to store) and the
team id. No GCP identity, no registry login, nothing about the infrastructure. The server
the build defaults to is the environment variable `MOTET_IOS_API_BASE_URL`, so this repo
still names no host. Creating the Apple identity, the app record and the key is invariant
9's human half; running the workflow afterwards is not, and nobody has to: **every push
to `main` that touches the app under `ios/` (or the workflow) uploads a build**, where a
merge on the web side asks the private repo for a pin bump. Tadas asked for it on
2026-09-19 in Zimmer session 19132 — three iOS PRs had merged and none reached his phone,
because the only trigger was a hand-run dispatch — and that request is the sign-off for the
trigger. `openapi.yaml` is deliberately not in the path filter: the app
compiles the committed Swift client under `ios/`, and `bin/ci` fails a commit where the two
disagree. A burst of merges uploads the run in flight plus the newest commit: GitHub keeps
one pending run per concurrency group and replaces it, and a started upload is never
cancelled. A dispatch (`gh workflow run testflight.yml --ref main`) still works, for a
re-upload (never Re-run an old run: it rebuilds that run's commit under a lower build
number) or `-f signing=archive`. `ios/README.md`, "Distribution", is the procedure.

**The consequence to hold on to is ordering.** A build now reaches TestFlight minutes after
its PR merges, while the API it defaults to (`MOTET_IOS_API_BASE_URL`) serves whatever the
private repo pins, and nothing makes a TestFlight build wait for that. So an iOS change that
needs a new API field has to decode an older API — optional-with-default, as
`keep_in_backlog` is — or it fails on the phone until that API is deployed. "Ship the API first" is no longer a step anybody performs; it is a
property the Swift client has to have.

**The other is `notify-deploy-pin.yml`, and it deploys nothing.** Staging and production run
whatever commit the private repo pins, so a merge here used to go live only when somebody
bumped the pin by hand — motet#116 merged and never shipped. Tadas asked for every merge to
`main` to open a pin-bump PR there (2026-09-13); this is the sender, and the receiving
workflow in the private repo is the half that opens the PR. On every push to `main` it sends
a `repository_dispatch` of `motet-main-updated` carrying the SHA. The receiver also polls on
a schedule, so the dispatch buys promptness and nothing else — which is why it is a **no-op
while `GH_MOTET_SYNC_TOKEN_TADASANT_INTERNAL` is unset and a warning, never a failure, when
refused.**

**The token is not a notification-shaped credential, and that is why it is fenced like the
Apple key.** GitHub's dispatch endpoint needs Contents: write on the target repository, so
the token can push to the private infrastructure repo. It should be fine-grained, scoped to
that one repository and to Contents alone. The same four kinds of fence as TestFlight apply,
and all four have to stay: `push` to `main` is the only trigger; the job refuses any other ref or
repository; the token is an **environment** secret in `deploy-pin`, whose deployment-branch
policy admits `main` only, which is the fence that survives a branch adding a workflow that
reads it; and it runs on a hosted runner with no checkout, passing the token to curl on
stdin. The cost of the environment is a deployment record per push to `main`. It is its own
workflow rather than a job gated on `all-checks-pass`, because waiting for main's CI would
gate nothing the schedule does not bypass, and a job in `ci.yml` would put the token in a
file pull requests run. Minting the token, and giving the environment its branch policy and
secret, is invariant 9's human half.

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
`high`, voice `off`. On a laptop and in staging a `settings` row from the admin screen sits
above both, per job; production never reads one (see "Models, spend, and the settings that
only staging honours").

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
not have. The third record is the `llm_usage` ledger (motet#92), fed by `usage_sink` beside
`collect_usage` — a row per completion, because per-user spend is the one number neither of
the other two can hold.

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

In the SPA it is a panel above the backlog. The sidebar's Backlog count is **what needs a
person** — held items plus failed ones (Sources' "Waiting for you" and "Failed" tiles,
summed), and not an item a worker is still carrying (motet#98) — so it is visible from every section, the paste screen included. It polls only while
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
and one path per section — `/backlog`, `/episodes`, `/sources`, `/paste`, `/credentials`, `/admin` — kept in
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

This is structure so the screens are reachable, not the start of a design system. It
wears the brand now (next section), but the brand is tokens, two faces and a handful of
drawn pieces, not a component library. If the next SPA issue is about the shell rather than
about a screen's job, that is the tripwire above firing.

### The SPA wears the Polyphony brand, and adds two webfonts and nothing else

`web/src/styles.css`, `web/src/brand/`, `web/src/fonts/`, motet#110. **Tadas chose the
brand on 2026-09-12**: "Polyphony", variant A. `brand/GUIDELINES.md` is the decision and
`brand/polyphony/index.html` is the reference; where they disagree on a value, the page
wins. This was a restyle rather than a redesign. Every class a screen used before is the
class it uses now, and no layout, section or interaction moved, except where the list below
says otherwise. One section has moved *since*, and it is the door's landing hero — see
below, and "The app's door is a sign-in, not a second landing page".

- **Self-hosted fonts, not Google Fonts.** Fraunces and Instrument Sans are served as latin
  subsets of the variable woff2 files, OFL, with their licences alongside them. Vite
  fingerprints them with the bundle. The issue left the choice open. Self-hosting means a
  private, authenticated app makes no third-party request, and the fonts are cached with the
  assets they belong to. `font-display: swap`, over the guidelines' fallback stacks.
- **The voice hues are never a status.** A connection state, a stage and a badge are ink at
  opacity. Only vermilion doubles as the error red. Vermilion, ochre, teal and plum appear
  together — in the eyebrow, the motif, the primary button's hairline, the active sidebar
  item's underline, and the played part of a scrubber or progress bar. The previous
  palette's green "done" and yellow "awaiting" are gone for that reason, not by accident.
- **Two readability departures from the reference, both derived rather than new colours.**
  Error *text* is `--error-text`, which is vermilion mixed a fifth of the way toward ink.
  True vermilion is 3.7:1 on parchment, under AA for words, so the hue stays on dots,
  hairlines and badges. Labels that carry information, such as table headers and fact
  names, use ink-soft rather than the reference's ink-mute (2.6:1), which is kept for
  decorative captions.
- **The old variable names are aliases.** `--fg`, `--muted`, `--line` and `--bad` point at
  the brand tokens, so a screen written against them did not need touching.
- **The landing was the reference hero, and it has since moved out of the SPA.** It had
  the headline, the positioning, the motif and **Start listening**, which started the
  Google sign-in the door always offered — and once `site/` shipped that same hero to
  `getmotet.com`, `app.getmotet.com` was serving the pitch a second time in front of the
  one thing somebody who typed the app's address came for. So the door is a sign-in and
  nothing else, and the section below is the record. The motif's path data is still lifted
  verbatim into `web/src/brand/scoreData.ts`, not redrawn; the transport picture under the
  score (`aria-hidden`, as on the reference page) lives only in `site/` now.
- **New episodes are titled "Episode — <date>"**, no longer "Briefing — …". The title is
  stored and appears in the RSS feed, so older episodes keep the old word.
- **Dark mode is not designed**, so nothing here derives one.

**The web player's transport replaced the browser's own controls.** The ink play circle,
the scrubber with the four-hue gradient, the tabular times and the speed pill all drive the
same `<audio>` element, and the listening-frontier rules below are unchanged. The scrubber
is a native range input laid invisibly over the drawn track, so a keyboard and a screen
reader reach one real slider. **The speed pill is a control the page did not draw
before**; the browser's native controls offered speed only on some platforms. Two costs to
know about. Volume and mute are gone from the page, so the system's volume is the control.
And at speeds above 1×, Play Live's server-side clock extrapolates at 1× between the
once-a-second position reports, so an interruption offset can trail by up to about half a
second at 2×. A load failure is the transport's own error line, because nothing else would
show one.

**The mic pill is Play Live's existing control, not hold-to-ask.** The guidelines' pill
reads "hold to ask". The web player has no push-to-talk: Play Live starts on a press, a
press while narrating interrupts (`barge_in`), and the voice service ends the turn. So
`Live` renders into the transport's pill slot through a portal, and the pill says
*Play Live* or *just ask*. Hold-and-release semantics would be new capability, and it is
not built. `site/`'s hero picture says "hold to ask" because it copies the reference, and
the reference is a picture too — the SPA no longer draws that picture anywhere.

### The app's door is a sign-in, not a second landing page

`web/src/screens/SignIn.tsx`, `web/src/styles.css`. Motet has two public surfaces —
`getmotet.com`, the landing built from `site/`, and `app.getmotet.com`, the SPA — and for
as long as both existed they showed the same page. `SignIn.tsx` rendered the reference
hero out of `brand/polyphony/index.html`, which is the page `site/` is built from: the
eyebrow, the headline, the subhead, the motif with its drawn transport, a **Start
listening** CTA and the three-fact strip, with the actual sign-in panel below all of it.
**The duplication was by construction rather than by accident** — one reference page, two
renderings of it — and the screen's own comment said so.

**The section above is the sign-off this reverses, and the reversal is stated rather than
assumed** (the preamble's bar: not "I have a better idea", but "the reason this was
decided no longer holds"). Tadas chose the hero in motet#110, when the SPA's door was the
only surface Motet had and a landing there was the only place the brand could live. `site/`
is that place now, so the reason is spent. Tadas, 2026-09-20: *"right now it's very
duplicative"* — `app.getmotet.com` should take you straight to signing in, or straight to
the dashboard if the browser already holds a token.

**So the door is the sign-in and its footnotes**: the wordmark in the bar, the brand
eyebrow, `Sign in`, one primary **Sign in with Google**, and three hints — who is accepted,
the registered redirect URI, and the API token. What is kept is deliberate and each piece
earns it: the 503 from `api.startLogin` still reads verbatim because it names the missing
variable, `redirectUri()` is still printed because a Google mismatch is invisible from
inside the app, and the `<TokenField>` disclosure stays because the shared token is still
the answer when there is no Google account to hand. Brand identity stays too — dropping the
*duplicated marketing* is not the same as making the page anonymous.

**One button, because the error had nowhere to go from the second one.** "Start listening"
and "Sign in with Google" called the same function, and only the hero rendered `status` —
so a refusal pressed on the lower button printed a screenful above it, on a screen whose
whole job is to report refusals clearly. There is one button now and the error is directly
under it.

**A browser that holds a token never sees any of this**, which is the other half of what
the owner asked for: `inShell` is true as soon as there is a token (or the deployment is
open) and no callback is in the address, and it renders the shell.

**`getmotet.com` is a literal in the SPA, and that is the repo-split reading.** The door
links out for anyone who arrived wanting the pitch. The Repo split rule forbids a secret, a
project id, a bucket, a service-account address, an internal hostname or a topology detail;
this is none of them — it is the product, named in this file's first line. It needs no
variable because it is a fact about Motet rather than about an environment, and a variable
would be a private-repo change per environment for one identical value. **The consequence,
stated: a staging door links to the production landing.** That is intended — there is one
landing page — and it is the only cross-environment link in the SPA.

**Two things the deletion exposed, both now fixed.** `.door-main` had a full-width variant
that existed only for the hero, so the door and every OAuth callback share one reading
column. That left `.door-panel` with exactly one consumer, `ConnectorCallback` — and it was
a two-column grid sized for the full-width strip below the hero, which inside the reading
column laid that linear message out in two overlapping columns. It had been rendering that
way since the callback shipped, on a screen nobody screenshots. It is a block now.

**What went with the hero**, because nothing else used it: `.hero*`, `.headline`,
`.now-playing`, `.motif .transport`, `.play-glyph`, `.btn-text`, `.pill-static`,
`.door-panel .kicker`/`.token`, and `Motif`'s `children` prop, whose one caller was the
transport picture. `Motif` itself stays — `Backlog`'s empty state is its remaining home, and
its full-size variant is kept for a surface that may want it rather than because one does.

**Three lines of `brand/GUIDELINES.md` are now departed from**, and they are named here
rather than edited there, because that file is the owner's record of the decision: the
motif "belongs on the landing/sign-in screen" (in-app it is the empty backlog only), "the
web SPA and its sign-in and landing" (there is no SPA landing), and the primary button's
"22px play glyph in a parchment circle" (no `.btn-primary` in the SPA draws one).

**The invariant-12 reading, recorded as invariant 12 asks.** No deployable, datastore,
queue mechanism, vendor, seam, cross-service protocol, inference stage, model call or
private-repo resource — one screen's markup, the CSS it owned, and a prop with no caller.
This is "work inside an existing shape", and the SPA tripwire is not firing either: the
change *removes* SPA surface and is about a screen's job rather than about the shell.

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
  fetched a blob because the local backend served no `Range`; it serves one range now,
  because iOS Safari will not play media from a server that ignores `Range`, so the dev
  path could not be tried on a phone. The route takes the feed token in the query because
  a media element cannot send a header.
- **The route asks the store before it signs, and a missing object is a 410.** A signed URL
  is minted without touching the object, so audio a bucket's retention rule had deleted was
  still redirected to, and the element reported the store's 404 as "could not load" — which
  is what the owner saw on every staging episode, on a phone, and read as a mobile bug. An
  `<audio>` error carries no status, so on one the player asks the route itself
  (`api.audioProblem`, `redirect: 'manual'`) and shows the API's sentence, or says the
  browser could not play a file that is there. An existence check that cannot be answered
  falls through to the redirect: it may only make a failure clearer, never cause one. It is
  a metadata read per request to the route — once per load in Chrome, and possibly again on
  a seek in WebKit, which may return to the route rather than the signed URL.
- **It resumes from `listened_through_ms` and writes `PUT …/position`**, the position
  resource a syncing player wants, so listening here moves the shelf and marks stories
  read as their segments pass. It is the first client to write the position from real
  playback — the iOS app does the same by the same rule (`ios/README.md`, "Playback position
  is cross-device") and RSS clients cannot report. It reports every ten seconds
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

**It shipped with nothing deployed, and deploying it was a sign-off of its own**: Tadas
approved a `motet-voice` service in staging and production on 2026-09-13, relayed by the
release orchestrator (Zimmer session 17607). The image is the root `Dockerfile`'s `voice`
target; the service, its secrets and the API's two variables are the private repo's. Until
an environment sets `MOTET_VOICE_BASE_URL` and `MOTET_VOICE_START_SESSION_TOKEN`,
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
- **What a browser allows only inside a tap happens before the first `await`.** iOS
  Safari starts an `AudioContext` made outside a gesture suspended — no reply audio, and no
  mic frames, because a suspended context never runs the processor — and refuses the
  narration's `play()` when `ready` arrives over the socket. So the tap makes and resumes
  the context and plays-and-pauses the element in one tick, which is what lifts WebKit's
  per-element restriction; a `play()` still refused is sent as `narration_paused`, and the
  player's own play button resumes it.
- **The socket is the one cross-origin surface, and it checks `Origin`**
  (`MOTET_VOICE_ALLOWED_ORIGINS`). `StartSession` has no CORS policy on purpose: only the
  API calls it.

**Whether a session can answer at all is its own field on the first `ready`, because no
code could carry it.** An arm with no live channel to open sent its dormancy as prose in
`detail`, so the only place either client surfaced it was a line in the transcript log —
which is the state production has been in since the service was deployed (`arm=composed`
with no speech-to-text vendor provisioned). On a phone that reads as "the VAD works but I
get no audio", because barge-in detection is genuinely unaffected and nothing else is
possible.

Putting `arm_dormant` in `reason` and letting the clients branch on it does **not** work,
and is the trap to avoid: `failure_reason` already emits that same code for a `LiveArm`
whose channel would not open, and there a typed question *is* answered by `text_arm`. One
code, two opposite consequences for the listener. So `SessionStateEvent.can_answer` says the
thing directly — `live is not None`, or a `text_arm` whose `capabilities().conversational`
is true, which is the flag a dormant composed arm sets false **while remaining its own
`text_arm`**, so `text_arm is not None` is not the test. It also covers the case neither
code could: a `LiveArm` with no `text_arm`, which has always been told it could type a
question it cannot. Optional on the wire, and a client that reads it treats absent as true,
so an older service offers a control that might not work rather than hiding one that does.
**The silence itself is the private repo's to fix** — a vendor has to be provisioned — and
saying so is this repo's.

Deliberately not built, each for a stated reason: a **startup probe that the realtime key
is billable** is a vendor connection per instance start and belongs with the decision to
deploy the service at all; **streaming the composed arm's reply** (2–5 s of silence today)
waits on whether the composed arm is the default.

**The iOS app has the same client**, ported rule for rule into MotetKit (`LiveSession`) with
the socket and audio in `MotetPlayback` — a second client of the existing session contract,
not a new one: the same mint route, the same frames, no vendor named (`ios/README.md`,
"Play Live is built on the phone, as on the web"). Its open questions are the web's, plus the
ones only a phone asks — how AirPods route while the mic is open, and whether the echo
canceller hears the narration.

**Open for the design session, with what runs today:** the default arm given realtime cost
(`composed`); open mic relying on browser echo cancellation vs headphones (open mic,
headphones recommended on screen); reply length (one or two sentences, by prompt); resume
at the interruption offset vs a rewind (the offset); the batch-STT comparison (not built);
whether talking over a reply is natural or rude (allowed).

#### A platform tool is a call on Motet's MCP server, and the slug is what points it there

`voice/src/motet_voice/tools/mcp.py`, `motet_voice.config.MOTET_MCP_SLUG`,
`motet_api.voice.SESSION_MCP_SERVERS`, motet#120. The voice service used to reach the API
through **HTTP path templates kept by hand in its own tree**, and three of its four tools
named something that does not exist: `get_item_detail` wanted `GET /v1/news-items/{id}` and
`start_research` wanted `/v1/research`, neither of which is a route, and `save_highlight`
posted a `quote` to `POST /v1/highlights`, which takes a *span* and reads the quote out of
the source itself. Only `mark_read` worked, which is why `SESSION_TOOLS` granted a deployed
session that one tool — the file said so, and nothing anywhere went red. **A template in one
repo half against a route in the other is a contract nothing holds**, and that is the thing
this change removes rather than the transport.

So the four tools are two, and each is a `tools/call` on `/mcp` (motet#111): `mark_read` is
the server's `set_news_item_read`, `save_highlight` is its own name. A name the server does
not have is *its* refusal, and the parity test there (`api/tests/test_mcp_parity.py`) is
what keeps the surface current — which is the whole reason to bind to it rather than to the
routes underneath.

**The two that went are gone rather than dormant, and they are not the same case.**
`start_research` needed Exa, which is not provisioned, **and** a route nobody has designed;
inventing one is invariant 12's business, not a transport change. `get_item_detail` needed a
single-news-item read, which is a route, a repository query, a regenerated OpenAPI document
and client, and a registry entry — for detail the session has already been handed, since
`SessionContext.transcript` carries every story, every claim and now the span behind it.
Dropping it was the smaller honest change; adding the route remains open to anyone who finds
a session genuinely short of something.

**The binding is the gate, and an unbound session's tools are dormant rather than absent.**
A client still supplies no URL and no credential (invariant 1's shape, one layer in): it
names a slug, and `StartSession` decides. A session that binds nothing still *sees* the
tools, described as dormant with the reason, so a persona told it cannot save a highlight
says so instead of promising and failing.

**Which slug the *service* knows and which one a *deployment* resolves are two questions,
and the difference is a 422 against a dormant tool.** A slug outside `KNOWN_MCP_SLUGS` is
refused at StartSession, naming both sides — it can never mean anything here. `motet` where
`MOTET_VOICE_API_BASE_URL` is unset is the other case and is **accepted**, with a warning and
dormant tools: the API sends that binding on every Play Live session, so refusing it would
take the whole conversation down to protect two tools, which is exactly the trade
`load_settings` already declines to make for a missing vendor key. `/internal/health`'s
`mcp_slugs` is the second list — what resolves — for `vault_ready`'s reason.

**A highlight's span is resolved from the session's own transcript, never from the model.**
`POST /v1/highlights` reads the quote out of the source text at the span it is given — which
is the rule that stops a model's paraphrase becoming a verbatim-looking highlight — so
something has to turn "save that" into a span. `TimedClaim` now carries `source_item_id`,
`span_start` and `span_end`, filled by `motet_api.voice.session_config` from the claim's own
`span`; the model names the line it heard, and `locate_claim` matches it (exact, then
containment, on case- and punctuation-folded text) and sends **that claim's** span. A model
that paraphrases loosely therefore fails to save rather than saving its own words, and it
cannot invent an offset because it is never asked for one. The fields are optional: a caller
that is not Motet has no spans, and a claim without one is simply not a candidate.

**Three rules inside that matching are each a wrong highlight avoided, not a refinement.**
Punctuation folds to a *space* and the collapse happens after, because deleting it joins the
words either side — `forty-million` became `fortymillion`, an em dash left a double space —
and this pipeline's prose is full of both, so the first version failed to save exactly the
lines a listener asks for. Within a pass the **tightest fit** wins rather than whichever
claim came first: a three-word claim is contained in almost any paraphrase and would take
every quote. And containment needs `MIN_CONTAINMENT_WORDS`, because *under*-quoting is the
other half of the same failure — a model that says `quote="that"` would otherwise land
inside some claim and write a highlight nobody asked for, which then reads as verbatim
source text. An exact match is always allowed however short: that is the model repeating
what was said rather than guessing. Length rather than a similarity score, deliberately —
a threshold is a judgement about two texts, in the one place meant to have no opinion.

**The credential is the owner token, and swapping it is a variable.** *The (a)/(b) choice in
motet#120 is the owner's and this is what runs until he makes it.* `MOTET_VOICE_MCP_TOKEN`
is presented when set; unset falls back to `MOTET_VOICE_API_TOKEN`, which the service already
holds — so **(a) needed nothing provisioned**, and (b) (a session minted for a service
identity, or an MCP OAuth grant, from `tadasant-internal`) is a value in one variable rather
than a rewrite. Under (a) the *credential* is the whole API, and what bounds this connection
is the `?tool_groups=` selection plus the fixed platform-tool table: `backlog,highlights`,
two write groups for the two tools that write, and **no read-only group at all**, because
everything a session reads arrives in its `SessionContext` (invariant 2). `registry.py` says
the honest limit of that — *"a caller that must not write needs a credential that cannot"* —
and the residue is stated rather than hidden: the selection also grants `list_news_items`,
`list_highlights` and `delete_highlight`, which no platform tool names and the voice service
therefore never calls. That second bound is `PLATFORM_TOOLS`, not the credential.

**Metered spend is out of reach by construction**, which is a property of the selection
rather than of the tools: every model call in the system is reached through `ingestion`,
`episodes` or `admin`, and none of those groups is asked for.
`api/tests/test_mcp_voice_binding.py` asserts it by calling `paste_text`, `create_episode`,
`create_smart_episode`, `connect_source`, `rotate_feed`, `get_admin_overview` and
`set_llm_config` on the real connection and getting "Unknown tool" for each.

**The SDK's client, not a hand-rolled one**, and the reason is `McpServerBinding`'s own: it
exists because Zimmer will point it at servers nobody here wrote, so the half of the protocol
this service speaks has to be the real one rather than the subset Motet's server happens to
accept. `motet-voice` therefore takes `mcp>=2.2,<3` — the same pin as `motet-api`, serving
the other half — and declares `httpx2` beside it rather than inheriting it, which is
`google-auth[requests]`' rule one package along: a dependency you import by name is one you
depend on. Both reach an HTTP endpoint and never a database, so invariant 2 is untouched and
`test_no_database_access.py` still holds.

**A connection lasts one call, and that is correctness rather than cost.** Holding one open
across calls buys one POST against a stateless server and costs three things the first draft
of this discovered the hard way. The SDK's transport is an `anyio` task group, and **anyio
refuses to let a task close a cancel scope another task entered** — the transport is
process-wide, so the websocket task that opened the connection is almost never the one that
closes it, and every close raised `Attempted to exit cancel scope in a different task` and
was swallowed, which is a leak that reads exactly like a clean close. The lifespan's own
teardown on SIGTERM had the same fault. And one session's failed call tore down a connection
other sessions had calls in flight on. A connection owned by the task that uses it has none
of those, needs neither a lock nor a generation counter, and is one `async with`; the shared
httpx client keeps the socket and the TLS session, so what a call actually pays is an
`initialize` round trip. `voice/tests/test_mcp_binding.py` drives three calls from three
tasks at once and closes from a fourth.

**What the listener is told when it fails is fixed prose, and the detail goes to the log.**
A tool's error text is read out loud, handed to the model, and sent to the browser: an SDK
exception there is usually meaningless (`unhandled errors in a TaskGroup (1 sub-exception)`)
and an HTTP error's string carries the deployment's API hostname and this connection's
tool-group selection with it. The log line unwraps the `ExceptionGroup`, which is the half
that is actually diagnostic.

**`motet.voice.tool_calls{tool,outcome}`** is what makes any of this falsifiable. A binding
that resolves to nothing, a credential the API refuses, and a deployment nobody has spoken to
are otherwise the same silence — the never-infer-"no errors"-from-"no data" trap on the one
path that leaves this process. `dormant` and `not_granted` are counted alongside `ok` and
`failed`, because a persona saying "I can't do that" is a product that does not work rather
than an error anywhere. `/internal/health` reports `mcp_slugs`, `mcp_tool_groups` and
`mcp_credential` — `api_token`, `scoped`, or `none`, never a value — for `vault_ready`'s
reason. `none` is the one worth looking for: it works only against an API whose own token is
unset, so in a deployed environment it is a misconfiguration that answers every call with a
401 the listener hears as "I can't do that".

**The invariant-12 reading, recorded as invariant 12 asks.** motet#120 is the design session
for the binding itself — the owner's issue states the two steps and names `?tool_groups=` —
and its one genuinely open question, the credential, is deferred to him above rather than
decided here. What this adds beyond that is one client dependency on an existing service,
three optional fields on an existing contract model, a counter, and two tools removed: no
deployable, datastore, queue mechanism, vendor, inference stage, model call, or resource in
the private repo. Invariant 2 is unaffected in either direction — the voice service still
reaches data only through Motet's API, now over MCP instead of bespoke HTTP.

**What no test here can tell you** is whether a deployed voice service reaches a deployed
API: both are configuration in the private repo, `MOTET_VOICE_API_BASE_URL` has never been
set in either environment, and Play Live is dormant until one is. The first real tool call is
a listener's.

### The iOS app measures its own audio, because nothing outside it can

`ios/Sources/MotetKit/Diagnostics/`, `ios/Sources/MotetPlayback/AudioLevelMeter.swift`,
`ios/App/MotetUITests/`, `ios/bin/ui-test`. motet#138 fixed a player that produced no sound
and said nothing; this is the other half — making that symptom *machine-detectable*, so it
cannot recur silently.

**The constraint that shapes all of it: no cloud device service captures iOS audio.** AWS
Device Farm's session artifacts are video, logs and screenshots with no audio track
documented anywhere; Appetize supported iOS audio output in the past, deprecated it with no
plans to restore it, and has never supported microphone input on any platform. So "is sound
coming out" is answerable only from inside the process, and a probe nobody reads is the same
silence one layer in — which is why there is a reader in every configuration, not only a
screen in Debug.

`PlaybackProbe` is three layers: what the player says it is doing, whether its clock
actually **moved** over a window, and an RMS/peak measurement off an `MTAudioProcessingTap`
on the item's audio mix. The second is the one that catches the failure that happened —
`.waitingToPlayAtSpecifiedRate` sets no error, emits no further event and never ticks — and
the third is the only one that is not the player's own opinion of itself.

**A missing layer abstains rather than voting no.** The meter answers nil until a buffer has
ever arrived and for any sample format it cannot read, and `isAudible` treats nil as
"nothing measured". Reporting it as silence would make a build where the tap did not install
report a fault on every perfectly good episode — the never-infer-"no errors"-from-"no data"
trap with its sign flipped, and the same trap `telemetry_configured` versus
`telemetry_exporting` exists for.

**The rules live in `MotetKit` and the AVFoundation file only reads hardware**, which is
`AudioSessionPlan`'s split one seam along: the window that decides "is the clock moving",
the abstention, the verdict and the locale-proof `key=value` formatting are all tested on
Linux in `bin/ci`. Whether the tap fires is a claim about a running `AVPlayer`, and
`ios/bin/ui-test` is what makes it — the one thing in this repo that boots a simulator and
runs the app.

**What that UI test can reach is bounded by something already settled: an agent cannot sign
in.** Google refuses an automated browser at the identifier step, so no automated run gets
past the app's front door. The flow therefore drives a Debug-only fixture that plays a
generated tone through the real engine, the real audio session and the real controller, and
asserts that the signal *changes* between playing and paused — both directions, because
either alone would pass against a hard-wired answer. The network half of playback is
deliberately not in it.

**A third build configuration, `Staging`, says which deployment a build is for.** Pointing a
build somewhere else was already a command-line setting; knowing which one you were on was
not, and the two look like one problem. `MOTET_BUILD_ENVIRONMENT` is a **label and never a
hostname**, which is exactly what lets it have a value in this public repo when
`MOTET_DEFAULT_API_BASE_URL` cannot — and an unlabelled build reads as production, because
a badge over real data is the worse of the two ways to be wrong. Reaching the real staging
API is one variable in the private repo; nothing here names a host.

**The invariant-12 reading, recorded as invariant 12 asks.** This adds no deployable, no
datastore, no queue mechanism, no vendor, no seam to one, no inference stage, no model call
and no resource in the private infrastructure repo — the staging server is a *value* for a
variable the build already reads, not a new one. Invariant 1 is untouched and is the reason
a staging variant is configuration at all: the client speaks only Motet's own API, so
pointing it at another deployment is a base URL. What it does add inside this repo is a
build configuration, a test target, a CI step and a dispatch workflow — CI and test
scaffolding, which invariant 12 names on the "does not count" side, and none of which
changes the package graph or the system diagram. The one judgement worth stating: the
probe reads `AVPlayer`'s audio mix through `MTAudioProcessingTap`, which is a new *use* of
an SDK the app already links rather than a new dependency.

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

**The artwork is the one feed URL without the token** (motet#110). `<itunes:image>` and RSS
2.0's `<image>` point at `GET /v1/feed/artwork.png`, which serves a package-data copy of
`brand/mark/motet-mark-3000.png` with no authentication: the image is the public brand mark,
identical for every user, and the clients that fetch artwork hand it to image caches and
proxies that keep a URL far longer than a feed — a bearer secret there is a leak for
nothing, and a rotation would blank the cover. The URL carries a content hash (`?v=`), so a
re-rendered mark is a new URL rather than a stale cache; a test holds the copy byte-equal to
its source, and `bin/build-images` asks the real container for it.

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
  bounded to the source's own `config.first_sync_days`, else `MOTET_GMAIL_FIRST_SYNC_DAYS`,
  else 30 days, and the run that starts one records the value it used in
  `sync_state.first_sync_days`. Each run records `sync_state.last_sync` (`at`,
  `seen`, `queued`, `caught_up`, `error`); a poll that gives up after its retries writes its
  reason there and on `sources.last_error` through the `poll` failure recorder, because its
  own transaction is the one that rolled back. `SourceResponse` reports `query`,
  `first_sync_days` and `last_sync`; the cursor stays the adapter's and is never reported.

#### The window is the owner's, per mailbox, and reaching further back is its own route

`gmail.first_sync_days`, `sources.config['first_sync_days']`, `POST
/v1/sources/{id}/resync`, `ingest.pending_resync`. **The default was 7 days and nothing on
any screen said so** (motet#139). The first production connect searched `label:Newsletters`
over a week, found 55 messages, paged through them correctly — 50 then 5, then "caught up" —
and the owner, who expected the 200-odd in that label, read the cap as a pagination bug. It
was not: `sources/tests/test_gmail.py` pins the paging, and the production logs show the
chain running to its end. **A bound nobody is told about is indistinguishable from a bug**,
and that is the defect rather than the number.

Three things came out of it, and the third is the one that is easy to get wrong:

- **The default is 30 days**, which is the shortest window in which a newsletter backlog
  looks like a backlog. Widening costs no inference: extraction is free and deterministic
  and every message stops at the held gate (motet#91) until a person picks it, so a wide
  window buys fetches rather than model calls.
- **The window is chosen per mailbox, at connect, and reported back.** `config` rather than
  a column, because it is the same kind of fact as `query`: the owner's standing instruction
  about one mailbox. Both clients send it on every connect, so the window a person was shown
  is the window they get. `MAX_FIRST_SYNC_DAYS` is ten years — offered as "Everything", and a
  real bound rather than a decoration, because it is what stops a typo turning one connect
  into an archive crawl. `SourceResponse.configured_first_sync_days` is what the *next* first
  sync would use, beside `first_sync_days`, which is what the last one actually reached;
  **null means nobody chose, and the API deliberately does not resolve the fallback**, which
  is a variable only the worker is given.
- **Widening a window on its own does nothing, so the repair is a route.** The adapter reads
  the window only on the page that *begins* a search, and a connected mailbox is long past
  that: its watermark has moved, and no later poll ever looks behind it. `resync` therefore
  writes the window *and* asks the next poll to start a fresh search.

**The request is parked in `config`, and that placement is the whole mechanism.** The
obvious implementation — have the route delete `sync_state.cursor` — loses the request
whenever a sync is in flight, and loses it *silently*: `handle_poll` rewrites the whole of
`sync_state` at the end of every run from the snapshot it started with, so the deleted
cursor is simply written back. `config` is the owner's intent and no poll rewrites it, so a
request parked there survives a concurrent run and is honoured by the next link of the chain
— which the route's own `enqueue_source_poll` guarantees exists. A poll honours it when
`config.resync_requested_at` is newer than `sync_state.resync_done_at`, and stamps *the value
it was asked for* rather than its own clock, so a second press during a run is not swallowed.

**A resync is cheap and safe to press twice.** It re-lists mail this mailbox has already
pulled in, and the poll's pre-check drops a message that has a row or an extract job before
it is fetched — so what it costs is listing. `source_items` is unique on `(source_id,
external_id)`, so nothing is duplicated and nothing already ingested is charged for twice.

**The invariant-12 reading, recorded as invariant 12 asks.** A key in `sources.config` used
the way `query` and the label-sync settings already are, a key in `sync_state`, an optional
argument on an existing `MailClient` method, one route on the existing API and its MCP
counterpart, and a field on an existing response. No deployable, datastore, vendor, seam,
queue mechanism, inference stage, model call, or resource in the private repo.

#### The phone can see what has been pulled in

`ios/App/Motet/Sources/HeldItemsView.swift`. The other half of motet#139, and the answer to
"why can't I see the 55 that did sync". They synced: all 55 became source items and every one
logged `held for ingest`. **The app had the count and nothing behind it** — a number on the
Sources screen — because the Backlog tab lists *news items* and a held item is deliberately
not one yet (motet#91). So from an iPhone, 55 newsletters pulled in correctly were
indistinguishable from 55 that were never fetched, and the only surface that could tell them
apart was the SPA's "Pulled in, waiting for you" panel on a laptop.

It is that panel, rule for rule: oldest message first, select, one button that spends and one
that discards, and a dismiss that asks first because nothing un-dismisses. Reachable from the
mailbox's own count *and* from a banner above the Backlog — above, for the SPA's reason, that
"where did the mail I just synced go" is asked immediately and an answer under a long list of
older stories is an answer nobody scrolls to. The Backlog tab builds its own `SourcesModel`,
because that model is scoped to the Sources tab and "what has arrived and is waiting" is a
Backlog question.

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

**The phone uses the same registered path, and the page hands the consent back.** The iOS
Sources tab connects a mailbox, re-consents for label sync and authorizes an MCP server by
passing the web app's `/oauth/callback` as the redirect URI and opening the consent in the
system sign-in sheet, told to finish on `motet://consent`. The callback page, loaded inside
the sheet on an iPhone, remembers no state in its empty sessionStorage — which is how it
knows the consent is the app's (`consentBegunElsewhere`) — so it exchanges nothing and forwards
Google's query to that link (`AppConsentHandoff`); the app finishes the consent at the same
API route with its own session. A tab that remembers a *different* state keeps the old
refusal, and a person who really did begin it in another tab can press "Finish here
instead". No redirect URI is registered for the app and no route was added for it.
`ios/README.md`, "Sources and connectors on the phone".

**It first waited for an https callback on that host and path, and that is what failed on a
phone** (Tadas, 2026-09-19: "the iOS app can't do it at all"): iOS honours one only on 17.4+,
only after verifying the `webcredentials` association, and only if it catches Google's
cross-site redirect — and where it did not, the page loaded in a sheet with no Motet session
and the consent died on a 401 the app never saw. The custom scheme that sign-in declined is
safe here because **a consent's code is worthless without an allowlisted Motet session**
to finish it with and a PKCE verifier (and, for Google, a client secret) that never leaves
the API; sign-in's code *becomes* a session, which is why it needed more. The invariant-12
reading: a second use of the app's existing scheme and one branch on an existing page — no
route, table, vendor or registration.

Two things there are load-bearing rather than defensive. The **authorization code is
exchanged exactly once** — StrictMode double-invokes effects, the API consumes the state
row with a `DELETE ... RETURNING`, and a second exchange would overwrite a success with
"already used"; the URL is cleared for the same reason, so that a reload cannot replay a
spent code. And **`error=access_denied` is an answer, not a failure** — someone pressed
Cancel, which is a supported response to being asked for a mailbox, and it must not read
like a crash.

#### A sync in flight reports its step and its count, from the server

`SourceResponse.sync_progress`, `motet_api.sync_progress`, `sync_state.sync_run`,
`phase2.source_sync_jobs`. Tadas pressed "Sync now" on a production mailbox (2026-09-19) and
watched "Syncing…" for as long as he cared to, because the only thing the screens could
watch was `last_sync.at` moving — one link of a chain that can be dozens long — and they
gave up after two minutes whatever was happening. The production cause that day was that
nothing drains production's queue at all, which the screen could not tell apart from a slow
mailbox.

**The worker adds the chain up; the API joins it to the job queue.** Each poll link adds
what it listed, queued and paged onto `sync_state.sync_run`, in the transaction that writes
the cursor; a run is continued while its search has pages left and started afresh otherwise,
and `record_poll_failure` — or a poll finding the source paused — ends it as `failed`. A link
does not re-arm the chain when a poll is already waiting: a "Sync now" pressed mid-chain *is*
the next link, and a second would run the rest of the search twice and end on an empty
"fresh" run. The API reads the source's newest open poll job — spelled `state = 'ready' OR
state = 'running'` so `jobs_ready_idx` and `jobs_stale_idx` answer it, which an `IN` list
does not — and its open and failed extract jobs created since the run began, on migration
0008's index, so the `done` rows that grow without bound are never scanned. From those it
derives one `stage`:
`queued`, `retrying`, `connecting`, `listing`, `fetching`, `done` or `failed`. Pulled in is
what the run queued less what is still open or failed. `found_is_lower_bound` is true while
listing, and both clients say "at least" beside it.

**`waiting_on_worker` is computed server-side** from the newest heartbeat and whether any of
the sync's jobs is running, with the Processing panel's five minutes — so a sync nothing will
run says so instead of animating. Both clients poll every two seconds for as long as a sync
is in flight and moving, rather than for a fixed window, and drop to ten seconds while it
waits on a worker. `done` and `failed` are reported for an hour.

**`worker_starting` is the same trap read from the other end, and the heartbeat alone cannot
see it.** Where the API starts the worker itself (motet#71) the worker is one-shot: it drains,
writes its heartbeat and exits. So at the moment anybody presses "Sync now" the newest
heartbeat is *always* older than the five minutes, and Cloud Run's job scheduling latency —
90–165 seconds, and unchanged by what caused the execution — is how long it stays that way.
Read by the heartbeat alone, every sync in such a deployment opens by announcing that nothing
will move until a worker runs, over a container that is booting because of that very request.
That is production, and it is what "stuck on Syncing…" was (Tadas, 2026-09-19, on
TestFlight). **Inferring "nothing is coming" from "nothing has run" is the same mistake as
inferring "no errors" from "no data"**, and it is worth naming because the panel exists to
avoid the first one.

So `sync_progress` also takes whether this deployment nudges (`MOTET_DRAIN_TRIGGER`), and a
poll job younger than `WORKER_STARTING` — four minutes: past the 165-second ceiling, inside
`WORKER_FRESH` — reads as a worker *starting* rather than a worker missing. The two are
mutually exclusive, both clients say "a worker is starting" instead of the stalled sentence,
and the fast poll interval applies because it is about to move. **What it infers is that this
API asked for a worker when it wrote this poll job**, not that one will certainly arrive: the
nudge is fire-and-forget and leaves no record (`motet_api.drain`), so the claim is bounded in
both directions — never made where the switch is off, and expired by the window, so a nudge
Cloud Run refused reverts to the heartbeat's own reading rather than promising a worker
forever. **The copy stops at the ask for the same reason** — "a worker has been asked for",
never "then it moves" — because `MOTET_DRAIN_TRIGGER` is configured-not-proven in exactly the
sense `/internal/health`'s field is, and an environment whose service account lacks the
`run.invoker` grant has every ask refused with a 403 the panel cannot see. Read **only** off
the poll job, because that is the one the API enqueues and nudges for; an extract job or a
further link of a chain is written by the worker, which fires no trigger, and a worker that
just wrote one is alive anyway — a gate that rests on the heartbeat being written before each
claim, on `Queue.POLL` leading `queues.PIPELINE`, and on `WORKER_STARTING` sitting a minute
inside `WORKER_FRESH`.

**A route answering a single source answers with its progress too, and it does so through a
default argument nothing else mentions.** `_source_response`'s `progress` parameter carries a
`Literal[False]` sentinel and computes the reading itself when a caller omits it, so all three
single-source routes — the consent callback, "Sync now", and a label-sync save — answer with
`sync_progress` without saying so. The poll job is written in the transaction that read is
made in, so the answer already says `queued`. **The cost of that shape is that no route would
notice the default being dropped**, and the client it would break is the one that keeps the
answer: the iOS app puts the row straight into its list and watches it (`SourcesModel.replace`),
so a null progress there leaves the button falling back to "Sync now" and the detail screen
polling nothing, where the SPA throws the answer away and re-fetches the list regardless.
`api/tests/test_sync_progress.py` pins all three routes for that reason.

**How long a sync has been running is shown while it is in flight**, from `started_at`,
recomputed on each poll rather than by a timer of its own — because "is this slow or is it
stuck" is the question somebody watching a first sync of a large mailbox is actually asking,
and every other field answers a different one. Not on a `done` or `failed` sync: that clock
would keep counting over something that has stopped.

**The run and the job rows are two statements, so a snapshot can straddle the last link's
commit** and show a `listing` run beside no open poll. That is a sync finishing, so a
`listing` run with no poll reads as stopped only once it is two minutes stale
(`BROKEN_CHAIN_GRACE`); read as `failed` at once, it stopped both clients' watch mid-sync.

**The run's timestamps are the database's transaction time**, not the worker's clock: the
extract jobs of the run's first page are stamped by the same transaction's `now()`, and a
Python timestamp a few milliseconds later would leave them out of every count; `updated_at`
is aged against the database's clock too.

**Extraction's skip note is a merged key** (`merge_source_sync_state`), not a rewrite of the
document it read. Extraction is not serialized against the poll, so the rewrite could put
back a cursor and a run total that a poll of the same mailbox had committed in between.

**The invariant-12 reading:** a key in `sync_state` used as that column is already used, a
read of the job table as `list_ingestion` already reads it, and an optional field on an
existing response. No deployable, table, migration, queue mechanism, stage or model call.

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
integration — Gmail, Paste, and two honestly disabled "Coming soon" — and one panel for
the account(s) behind the one you pick, opened in the grid directly under its card and
scrolled into view (it used to follow the whole grid, which put the connect form off-screen
on a phone, 2026-09-19). **The catalog is static**, because
`GET /v1/sources` lists *accounts* and something not yet connected has no row to render.

The last sync's result, the filter and the first-sync window are motet#94's fields and are
read, not re-derived. What "Sync now" shows while a sync runs is `sync_progress`, the
server's reading of the whole poll chain, re-fetched on an interval for as long as it is in
flight — see "A sync in flight reports its step and its count" above. It used to watch
`last_sync.at` move for two minutes, which is one link of the chain and gave up on long ones.

Two things the API grew for it are decisions rather than fields:

- **`sources.disconnected_at` (migration 0016) is what separates a disconnected mailbox
  from an abandoned consent**, which are otherwise the same row. The disconnect route sets
  it only when it actually deleted a credential — as does label sync's account check, which deletes
  a mismatched grant before marking the source disconnected (motet#96) —, so "disconnecting" a row that never held
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
- **`MOTET_API_TOKEN` still works against the API.** The RSS feed, any script. The iOS
  app stopped taking it on 2026-09-19 (see "The phone signs in through the web
  sign-in"). It stopped being something a *human types into a browser*; it did not
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

#### The phone signs in through the web sign-in, and a one-time code carries it back

`POST /v1/auth/native/start`, `POST /v1/auth/native/redeem`, migration 0019,
`ios/Sources/MotetKit/Auth/NativeSignIn.swift`. **Chosen by Tadas on 2026-09-13, in Zimmer
session 17805, as option A of the options put to him.** The rejected alternatives: **(B)**
native Google Sign-In, which needs a second, iOS-type Google OAuth client (a one-time human
step under invariant 9) and a Google SDK in the app, and makes the API trust a second
audience; **(C)** keep pasting `MOTET_API_TOKEN` into the phone, which leaves a
non-expiring, owner-equivalent credential on a device that can be lost; and a signed-in
"sign in on phone" code or QR on the web Settings screen, which needs the same backend work
as A and is clumsier to use.

The flow reuses everything the browser sign-in already has:

1. The app makes a PKCE pair and calls `native/start` with the challenge. The API writes an
   ordinary sign-in `oauth_states` row plus the challenge (`handoff_challenge`). It builds
   the redirect URI from `MOTET_APP_BASE_URL` and does not take one from the caller: the app
   knows the API's address, not the web app's.
2. The app opens the returned URL in the system sign-in sheet (`ASWebAuthenticationSession`).
   Google returns the sheet to the web app's `/oauth/callback`, the redirect already
   registered on the one OAuth client, so **no new Google client exists**. The web app posts
   the code as it always does. The ID token is verified and the allowlist checked, exactly
   as for a browser.
3. Because the pending row carries a challenge, `/v1/auth/google/callback` mints **no
   session**. It stores a handoff in `auth_handoffs` and answers with
   `handoff_url = motet://signed-in?code=…`. The SPA stores nothing, **asks the person to
   confirm** that they just tapped Sign in in the Motet app, and only then navigates to the
   link, on which the sheet closes.
4. The app calls `native/redeem` with the code and its verifier, and gets an ordinary
   session in `auth_sessions`. That session is revoked by `/v1/auth/logout`, expires in
   thirty days and is re-checked against the allowlist on every request.

What makes this safe, and each point is pinned in `api/tests/test_native_sign_in.py`:

- **The session token never travels in a URL.** The link carries a code, stored only as a
  hash and valid for two minutes, and consumed by the redeem that succeeds. A refused redeem
  rolls back with its request and leaves the code for the app that holds the verifier, so a
  wrong guess cannot burn the real app's sign-in.
- **The code is worthless without the verifier.** Another app can register the `motet`
  scheme and read a code meant for Motet. What it cannot have is Motet's verifier.
- **What PKCE does not stop, and what the universal link is for.** Any app on the phone can
  call `native/start` itself, hold *its own* verifier, and wait for the link, so a sign-in it
  started ends in a session it holds. Nothing in the protocol tells that apart from Motet —
  a custom URL scheme is a claim any app may make. **Tadas approved the stronger fix on
  2026-09-13** and it is the section below. The confirmation stays, because it is what the
  scheme fallback has; the SPA also refuses any `handoff_url` that is neither
  `motet://signed-in?…` nor this origin's own `/app/signed-in?…`, so no value there can run
  as script in its origin.
- **The browser that finished the sign-in holds nothing.** The sheet shares Safari's
  storage, so a session left there would outlive the flow in a browser nobody is looking at.
- **The link is the API's.** Its scheme, host and single `code` parameter are literals in
  `main.py`, never built from anything a caller sent.
- **The allowlist is asked twice**: at the callback, and again at redeem, which is the moment
  the session is written. An address removed in between gets nothing.

##### The handoff comes back on a verified https link where the deployment can serve one

**Approved by Tadas on 2026-09-13**, in this session, as the answer to the paragraph above.
An https callback is the one callback iOS will not hand to an app that has not proved it
owns the domain, so a hostile app can no longer receive a Motet sign-in at all — where the
deployment is set up for it. It is **off by default and degrades to the scheme**, because
three independent facts have to line up and none of them is knowable from the code:

| Fact | Set by | Absent means |
|---|---|---|
| `MOTET_IOS_APP_LINK=1` on the API | the private repo | the API builds `motet://signed-in` as before |
| `MOTET_IOS_APP_ID=<TEAMID>.<bundle id>` on the web image | the private repo | the container serves no app-site-association file, so Apple verifies nothing |
| the entitlement in the build | `MOTET_IOS_APP_DOMAIN` in the `testflight` environment | the app never asks the sheet for an https callback |

**The service is `webcredentials`, not `applinks`, and that distinction is the feature.**
An https callback to `ASWebAuthenticationSession` is *not* a universal link: it is verified
through the shared-web-credentials service, and a session asked for one on a domain the app
claims only under `applinks` refuses to start — "Using HTTPS callbacks requires Associated
Domains using the webcredentials service type". Apple documents neither half of that
clearly, and the first draft of this change got it wrong in both the entitlement and the
served file. `applinks` is **deliberately absent** as well as insufficient: claiming
`/app/signed-in` would route every tap on that URL anywhere on the phone into an app that
has no handler for one. `webcredentials` claims no URL at all, so nothing about the web app
leaves the browser.

**The app decides before the sign-in starts, and the API stores what was agreed.** This is
the half that is easy to get wrong, because the callback is made by the *browser*, which
knows nothing about the phone. The app sends `app_link_domain` — the host its own
entitlement names, and only where its iOS is 17.4 or newer — to `native/start`; the API
offers the https link only if that host is exactly its own, and records the answer on the
pending row (`oauth_states.handoff_app_link`, migration 0021). `/v1/auth/google/callback`
then builds the link from **that row**, never from the flag. Choosing from the flag alone
hands an https link to a sheet watching for `motet://`, and that sheet never closes: a
deployment that switched the flag on ahead of a build would have broken sign-in outright
rather than falling back. `api/tests/test_native_sign_in.py` pins it from both sides.

**An https callback that is refused still falls back.** The entitlement can be signed in
and not yet in force — Apple's CDN has not fetched the file, the capability is not ticked,
an export dropped it — and the refusal happens when the sheet *opens*, before anything has
happened. `SignInView` starts one fresh sign-in on the scheme rather than leaving a dead
sign-in screen; it has to be a fresh one, because the server has already committed this
one to the https shape. **iOS reports that refusal with `canceledLogin`, the same code as a
person pressing Cancel**, and the first build treated it as one — silently, with no
fallback, so "Sign in with Google" flashed "Signing in…" and returned to itself
(2026-09-19). `NativeSignIn.classifyCancellation` tells them apart by time: a "cancel"
inside a second is a refusal, because nobody loads Google's page and dismisses it that fast.
The case that surfaced it was a build installed before the web app served its association
file; iOS checks the association at install and update, so the device kept refusing.

**The web container writes the association file at start** from `MOTET_IOS_APP_ID`
(`web/docker-entrypoint.d/`), beside the `config.js` rewrite and for the same reason: one
image, configured where it runs, and no Apple team id in this public repo. nginx serves it
as `application/json` with `no-store`, and `bin/build-images` asserts that an unconfigured
container 404s it, that a configured one serves `webcredentials` for the app id with the
right content type, and that it claims no `applinks`.

**`MOTET_APP_BASE_URL` has to be https with no explicit port**, because
`Callback.https(host:path:)` takes a host and a path and has nowhere to put either. A
deployment whose origin cannot carry the link logs at ERROR and uses the scheme;
`/internal/health` reports the resolved `ios_app_link` rather than the raw flag, so a switch
that is set and inert does not look like one nobody set.

**The SPA has a landing page at that path** (`screens/AppHandoff.tsx`), because a universal
link is still a URL: opened where the app is not installed — a desktop browser, a phone
without the app — it must read as something rather than as the backlog with a stray address.
It reads nothing out of the URL, so the code in the query is never touched by script.

**The entitlement is its own file.** `App/Motet/WebCredentials.entitlements` asks for the
associated domain and nothing else; `App/Motet/Motet.entitlements` asks for CarPlay, which
Apple grants by manual review, and an ungranted entitlement fails a build to *sign*. Keeping
them in one file would couple this to that grant. `ios/bin/testflight` enforces it: exactly
one entitlements file may be signed in, it must be that one, it must carry a non-empty
domain, and the guard asks the **parsed** plist what it requests rather than grepping the
file — whose own comment explains why it is not the CarPlay one, and so contains the word.

Ticking **Associated Domains** on the App ID is invariant 9's human half, like the App ID
itself. What no test here can tell you is whether Apple's CDN has fetched the file: the first
real sign-in on a phone is the evidence.

What this adds under invariant 12: two routes on the existing API, one table used the way
`oauth_states` and `auth_sessions` are already used, and a nullable column on `oauth_states`.
It adds no vendor, no second OAuth client, and nothing in the private infrastructure repo.
The app no longer takes a pasted `MOTET_API_TOKEN` (Tadas, 2026-09-19): sign-in is its
front door and the only way in, and a launch removes a token an earlier build stored. The
API still accepts the token everywhere else. As with every
change to sign-in, a green CI run proves nothing about the real consent screen, so a human
signs in on a phone once after this ships.

#### A personal access token is a third key to the same lock

**Sign-off: Tadas, 2026-09-20.** He asked for *"functionality in motet to create 'app
passwords' that bypass the human oauth flow … the mechanism we'll have the agent leverage
when testing on staging"*, was given the two shapes — a staging-only shared secret login,
or a real per-user token system — and answered *"Go straight to PATs."* `api_tokens`
(migration 0024), `motet_db.api_tokens`, `POST/GET /v1/auth/tokens`,
`DELETE /v1/auth/tokens/{id}`, `deps.require_session`, `motet_api.throttle`,
`web/src/screens/credentials/AccessTokens.tsx`.

**The problem it answers is that an agent cannot sign in.** Google refuses an automated
browser at the identifier step — that is settled, not untried, and the sign-in section
above says so — so until now the only non-interactive way into `/v1` was the shared
`MOTET_API_TOKEN`, which belongs to no person, cannot be revoked by a request, and is
rotated by a deploy. A staging harness driving the product end to end needs a credential
of its own.

**Tokens, not passwords, and that is the one place this departs from the words he used.**
Motet has no password store at all: sign-in is OAuth-only, so `auth_sessions` is the whole
credential surface. An "email + app password" login would mean introducing password
hashing, a reset flow and lockout logic that nothing else in the product needs, for
ergonomics a bearer token already has. A token is the smaller surface and the same
affordance.

**A PAT is a third bearer in the same header, and resolves to the same user and the same
checks.** `require_caller` tries the shared token first (constant-time), then routes on
the `mot_` marker: a bearer carrying it is looked up in `api_tokens`, anything else in
`auth_sessions`. So the feed tooling's path is unchanged and costs no extra probe, and a
PAT never costs a session probe. Nothing about "one account" changed — `user_id`
references the same seeded `motet-owner` row, and
`test_every_token_belongs_to_the_one_seeded_account` says so.

**What a PAT may not do is the short list, and each entry is a different escalation.**

| Refused | Because |
|---|---|
| Mint, list or revoke a token | A credential that issues its own successors makes revocation unbounded: revoke the one you know about and it has already produced three you do not, with no surface that could show you the tree. A token is a leaf. |
| `/v1/admin/*` | The operator view is the one route family that returns every user's data. `is_admin` already required a session; a PAT is not one, for the reason an MCP grant is not. |
| Anything the API itself cannot do | Invariant 8 is untouched: the API holds `DekWrapper` and no `unwrap`, and the deployed service account has `useToEncrypt` and not `useToDecrypt`. A PAT inherits the process's capability, which does not include reading a source credential. |

**The shared `MOTET_API_TOKEN` cannot mint one either, and neither can an MCP client's
grant — that second one is the trap.** An MCP access token *is* an `auth_sessions` row
(motet#111), so `how` reads `"session"` for it and a plain check on that alone would have
admitted it: a one-hour delegated grant, revocable by deleting the client registration,
minting a credential that outlives the grant, the revocation and the registration
together. `require_session` spells the check the same way `is_admin` does so the two
cannot drift. The shared token is refused for a different reason: rotating it is a deploy
and that rotation is the recovery for it having leaked, so a token minted from it would
make the recovery silently incomplete.

That leaves a signed-in browser, or `motet_db.mint_session` — the staging deploy's own job
entrypoint, which is **how the agent this feature is for gets its first token** without a
human at a consent screen.

**It is hashed at rest and shown exactly once.** Only the hex SHA-256 is stored; the
plaintext is in the mint response body and nowhere else. Lookup is by that digest, which
makes verification one full-length index probe — `auth_sessions`' argument — and what
comes back is compared again with `hmac.compare_digest`, so "the comparison is
constant-time" is a property of `motet_db.api_tokens` rather than a claim about what
Postgres does inside an index. A lost token is revoked and re-minted, never recovered;
that is the opposite of the feed token's trade, and the feed token's section says why it
goes the other way.

**The prefix is the incident-response affordance and is display only.**
`mot_<environment>_<first 8 of the secret>` — `mot_` so a leaked credential is
recognisable at a glance and greppable, and the environment so it is obvious *what* it
opens. **The marker routes the lookup and never decides it**: a session token is 43 random
url-safe characters and one in 16.7 million begins `mot_`, so a bearer the token table
does not recognise falls through to the session table rather than being refused forever. **No new variable is required in the private repo**: the label falls back to
`deployment.environment.name` out of `OTEL_RESOURCE_ATTRIBUTES`, which every deployment
already sets because GlitchTip labels errors with it, so staging mints `mot_staging_…`
unconfigured. `MOTET_TOKEN_LABEL` exists only so a deployment can pick a shorter spelling
(`stg`, `live`) without renaming the environment every span and every error report wears
— the same shape as `OTEL_INGEST_TOKEN` beside `OTEL_EXPORTER_OTLP_HEADERS`. A label that
will not slugify falls back rather than refusing: a deployment must not be unable to mint
a token because of how it labels its telemetry.

**Revocation is a stamp, not a `DELETE`, and the row stays in the list.** With no database
shell (invariant 10) that list is the only place "which tokens existed, and when did each
stop" can be asked. Expiry is optional at creation and enforced in the lookup predicate
rather than by a sweep, so a token that lapsed a second ago stops now. `last_used_at` is
written at most every five minutes, which is `auth_sessions.last_seen_at`'s argument on
the credential most likely to meet it — an agent makes concurrent requests on one token,
and a write per request would take a row lock held to commit and serialize them.

**A token dies with the address that minted it.** The row carries the allowlisted email of
the session that created it, and `require_caller` re-checks it on every request exactly as
it does a session's — de-listed has to mean gone, and a PAT outlives the browser session,
so without this, taking somebody off `MOTET_ALLOWED_EMAILS` would revoke their sessions and
leave their long-lived tokens working. The revocation is committed on the spot, because
the 401 it raises rolls the request back.

**`/v1/auth/logout-all` deliberately does not touch tokens, and a token may not call
it.** Folding tokens into it would mean a person signing out everywhere silently killed
the credential an agent is running on; the token list is the lever instead, and reaching
it from another device needs only a sign-in, which is the property `logout-all` exists
for. **The second half is the one a review had to find**: a PAT reaching that route could
delete the owner's browser session on a loop, and a session is the only credential that
can reach the revoke route — so a leaked token would be able to out-race its own
revocation, which is exactly the weakness "rotating the shared secret is a deploy" has and
that this feature is supposed to improve on. A PAT therefore gets a 403 there.

**The failed-auth throttle is small and its value is narrow — read `motet_api.throttle`
before relying on it.** It is not what stops a token being guessed; a 256-bit secret is.
What it bounds is the *work* a stranger can make one API process do: a database probe per
attempt, on an API whose connection is per request.

**The bucket is the process, with no key at all, and that is a correction a review
forced.** The first draft keyed on `request.client.host` — and the API is served by
`uvicorn --forwarded-allow-ips='*'`, because Cloud Run's front end is the peer, so uvicorn
rewrites `request.client` from the **left-most** `X-Forwarded-For` entry: whatever the
outermost caller typed. The key was therefore attacker-chosen, a fresh value per request
bought a fresh budget, and the limiter bounded nothing while *looking* like it bounded
something. There is no unspoofable per-caller key available here — the right-most entry is
Google's front end, one value for the whole internet — so one counter for the process is
the honest shape, and `test_a_spoofed_forwarded_for_buys_no_fresh_budget` pins it.

Three properties keep that from being a liability:

- **It is consulted only after a request has already failed, and only when the request
  presented a bearer.** A valid credential never touches it, so no amount of hammering can
  lock the owner out; and a request carrying *no* credential cost no lookup, so there is
  nothing to bound — which is also what keeps an MCP client's unauthenticated discovery
  probe answering 401 with its RFC 9728 pointer rather than 429.
- **The worst an attacker does to somebody else is turn their 401 into a 429**, and the SPA
  reads both as "not signed in" on `/v1/auth/session` for exactly that reason: a 429 there
  means this credential was refused, since a valid one is never throttled, and reading it
  as an outage would leave a dead token in storage behind a sentence nobody can act on.
- **It is in-process and per-instance, and it fails open.** The window is *fixed* rather
  than sliding, so the honest ceiling is **twice** sixty in a span straddling a boundary,
  times the instance count — accepted, because a sliding window costs a deque per bucket
  to halve a number whose only job is to be finite. A cross-instance limiter would be a
  row and a lock on the one path whose job is to be cheap, which is the shape
  `motet_api.waitlist` already declines — a new mechanism, and invariant 12's business.

`motet.api.auth_failures{outcome}` is what makes it falsifiable, because a queue of 401s
and a queue of 429s look identical in an access log. One object per process also means
**`api/tests/conftest.py` resets it between tests** — without that, a module asserting a
401 gets whichever status the module before it left the budget at, which is the throttle
working and a leak all the same.

**Every local on the auth path that holds a credential is named `token` or `secret`, and
that is load-bearing.** `sentry_sdk` captures frame locals into an error report and its
default scrubber redacts by *variable name*; both of those are on its denylist and
`presented`, which `require_caller` used to call it, is not. So an unhandled exception on
the auth path redacts the bearer instead of shipping it to GlitchTip.

**A name is not enough for an object that prints its fields, which is the half the second
review found.** `require_caller`'s other local is a `Settings`, and `config` is a name no
denylist knows — so the bearer was redacted while the *shared owner-equivalent token* and
the Cloud SQL URL, password and all, went to GlitchTip in the same frame, by that
dataclass's default repr. `Settings.api_token` and `Settings.database_url` are
`field(repr=False)` for that, and `test_settings_does_not_print_its_secrets` pins it —
including that the harmless fields still print, or the assertion would pass on a repr
that had been blanked wholesale. This one predates the feature; it is fixed here because
this is the section that claims the property.
`api/tests/test_api_tokens.py` asserts the names against the installed SDK's own list,
because a rename on either side would otherwise be silent. `MintedToken.secret` is out of
the dataclass's repr for the same reason `motet_api.waitlist.Submission.email` is, and
`CreatedApiTokenResponse.token` carries `Field(repr=False)` because Pydantic's default
repr prints every field and that model is a frame local of the route that returns it.
**The residue is named rather than hidden**: the encoder's own frames and the rendered
body still hold the bytes, under names (`obj`, `content`) no scrubber denylist covers, so
an unhandled exception raised *inside* serialization on that one route could still carry a
token. Closing that would mean not building a response model for it at all, which is a
worse trade than saying so.

**The SPA affordance is a panel at the foot of Credentials**, offered only to a caller
`/v1/auth/session` says is a session — the sidebar-and-403 split the Admin screen already
keeps. The minted value gets a panel of its own with a copy button and a "this will not be
shown again" line, is held in component state and never in `localStorage` or a URL, and
the list beside it shows prefix, label, created, last used and state, because the API has
no secret to send it. The account menu has a `pat` arm of its own, because a PAT carries an
address and is *not* a sign-in: `/v1/auth/logout` is a no-op for one, so offering Sign out
there would be a button that does nothing.

**The invariant-12 reading, recorded as invariant 12 asks.** This adds a second credential
kind on the existing bearer slot, one table used the way `auth_sessions` already is, three
routes on the existing API, one in-process counter, and a panel on an existing screen: no
deployable, datastore, vendor, seam, protocol, queue mechanism, inference stage or model
call, and **no resource, secret or required variable in the private infrastructure repo**.
It is above the line all the same — it is an authentication path, and "when in doubt, it
counts" — so the sign-off it rests on is the owner's own answer above, given against the
alternative he was shown, not the size of the diff.

**What no test here can tell you** is whether the first real token minted in a deployed
environment is minted by a *human's* browser session or by the staging mint, because both
paths need something CI does not have — a Google consent screen, or the private repo's
deploy job. What is pinned offline is the whole decision procedure against a real Postgres:
mint, authenticate, refuse a wrong one, refuse a revoked one, refuse an expired one, and
read the row back to prove it holds a hash and not the token.

### The staging test harness is four routes behind a flag that refuses to boot in production

`api/src/motet_api/fixtures.py`, `db/src/motet_db/fixtures.py`, `/v1/testing/*`.
**Asked for by Tadas through the 2026-09-20 staging-e2e workstream** (Zimmer sessions
#19242 and #19254), which names the three capabilities, the flag, and the requirement that
the application refuse to boot in production with the flag on. That request is the sign-off
this section records; the choices below are the ones it left open.

The goal is an agent driving Motet end to end on staging with nobody in the loop —
authenticate, connect the test inbox, sync it, build an episode, check the result, reset,
repeat. Three things stood between that and the API as shipped.

**Seeding a Gmail credential, because there is no machine-to-machine OAuth for a consumer
`@gmail.com` account.** Google's only mechanism for a service account to act as a mailbox
is domain-wide delegation, which needs Workspace. So the consent is performed once by a
human — invariant 9's human half, exactly as written — and its refresh token goes into
Secret Manager as `MOTET_STAGING_GMAIL_REFRESH_TOKEN`, injected into the staging service
like every other secret. `POST /v1/testing/gmail-source` re-establishes the connected state
from it on demand, which is invariant 9's *other* half: re-connecting after the one-time
consent is a routine operation, and a routine operation that needs a human is a defect.

**Unset means "not provisioned yet", and the 503 says so in those words.** Cloud Run refuses
to create a revision whose secret has no enabled version, so the mount is off until a human
places the value once — which makes *unset* the expected state of a freshly deployed staging
rather than a fault. A generic "could not seed" there would send somebody to read this code
instead of to place the token. `MOTET_STAGING_GMAIL_ADDRESS` is the non-secret half, set
beside it: an address is configuration rather than a credential, and having it lets the seed
record the expected account **without a profile call** — which the API has no business
making, since it speaks to no vendor.

**Sealing widens no decrypt, which is why this lives in the API rather than the worker.**
The route calls `phase2.store_source_credential` over `deps.dek_wrapper` — the same call,
over the same encrypt-only `DekWrapper`, that the OAuth callback makes — so the row is
envelope-encrypted with a per-record DEK under the same KEK and the same
`user_id:source_id:provider` AAD, and the API's service account still holds `useToEncrypt`
and not `useToDecrypt`. Invariant 8 is untouched in both directions. Routing the write
through the worker was the fallback if sealing had needed decrypt; it does not, and a
second process in the path would have bought coupling rather than safety.
`api/tests/test_fixtures_api.py` opens the sealed row with a real `KeyManager` rather than
checking the columns are non-null, which is also what pins the AAD.

**The refresh token in plaintext in a service's environment is the one real trade, and it
is bounded rather than assumed.** That is not how a *user's* mailbox token reaches these
processes and must not become one: what makes it acceptable here is that it is one
throwaway test inbox (invariant 13 — staging's secrets are the non-sensitive kind by
construction), it exists in staging alone, and the flag is what stops the variable being
read anywhere else.

**A refused *refresh* names the publishing status, which is the one cause nothing here
can see.** Google's `invalid_grant` is returned for three different facts and distinguishes
none of them: access was revoked, the token went six months unused, or **the OAuth client is
still in "Testing" publishing status, which expires every refresh token it issued after about
seven days.** The third is the one that breaks an unattended loop on a weekly cadence with
nothing pointing at it, because the publishing status is a console setting on a client defined
in the private repo — and the carve-out people reach for does not apply, since Google exempts
only clients asking for name, email and profile and Motet asks for `gmail.readonly`. So
`motet_sources.gmail.REFRESH_REJECTED` names all three, on a *refresh* alone: `invalid_grant`
on the first code exchange is a spent authorization code and has nothing to do with it. The
message lands on `sources.last_error`, which is what `GET /v1/testing/jobs/{id}` reports — so
the harness caller sees it without reading a log.

**`mailbox` is recorded on the seeded source, and the account check is what makes it
worth recording.** A refresh token in Secret Manager cannot be read back, so "is this for
the inbox we think" is precisely the question the seed cannot answer for itself. It does
not have to: `ingest.check_mailbox` asks Gmail which account a grant reaches before reading
with it (motet#96), so a token for some other inbox **disconnects** the source with both
addresses in `last_error` rather than quietly ingesting it. A test asserts that, and it is
the reason the round-trip test records the fake mailbox's own address.

**Reset is `motet_db.fixtures.reset_user`, and what it keeps is as much the design as what
it removes.** Jobs go first — a job is resolved to a user by joining its payload to the row
it is about, so one whose source item or episode is already gone resolves to nobody, is
left behind, and fails on the next drain against a row that does not exist. The join tables
are deleted explicitly rather than left to `ON DELETE CASCADE`, so the per-table counts in
the response are a baseline a caller can assert rather than trust. `RESET_KEEPS` is the
other list, with a reason each: the account, **the session the caller is holding** (a reset
that revoked it would log the agent out halfway through its own run), every authorization
in flight and every grant a client holds (`oauth_states`, `api_tokens` and the sign-in and
MCP tables — the same argument one key along, and the personal access token is the very
credential the agent driving the loop holds; a mailbox consent for a deleted source still
cascades from `sources`), the feed token a podcast client is subscribed to, the connectors a human
added behind a one-time step, the spend ledger, and the seeded `src_paste` row — deleting
which would take paste-in down in a way that reads as an application bug.

**The trigger routes add a job id, not a trigger.** `POST /v1/sources/{id}/poll` and
`POST /v1/episodes` already enqueue exactly these jobs, and `POST /v1/testing/jobs` calls
the same two helpers rather than a second definition of either. What the product routes
cannot answer is *which* job they produced. `GET /v1/testing/jobs/{id}` then reports the row
**and the queue's heartbeat**, and the pair is the deliverable: a job in `ready` looks
identical whether a worker is working through a backlog or whether none has run for a week,
so a caller given only the state has to infer liveness from elapsed time — which is
motet#38's trap, and why sessions #19159 and #19202 had to exist in the first place.

**A seed takes a per-user transaction lock, because it is a check-then-insert.**
`sources` has no unique index on `(user_id, kind, name)`, and the realistic double-submit is
not a second tab but a *retry* — a client that timed out on a cold start plus a KMS round
trip. Two mailboxes of one name is not a cosmetic duplicate: the trigger route then has no
single source to poll and the unattended loop stops. Its own lock namespace, beside the
held-claim lock's, because it serializes seeds against seeds and has no business making an
"Ingest now" wait.

**"Holds a credential" and "would read the mailbox" are different questions, and the poll
trigger asks both.** `ingest` pauses a source on a permanently refused refresh and **keeps**
the credential — which is exactly what an OAuth client in "Testing" status produces every
week — and `handle_poll` then short-circuits on a paused source and returns *normally*. A
trigger that offered it would enqueue a job that finishes `done` with no error, so a caller
reads a successful sync of a mailbox nothing opened: a false **green**, on the surface built
to remove that ambiguity. So a paused source is a 409 naming its `last_error`, and re-seeding
is the repair.

**The recorded scopes are validated rather than taken.** They are what the worker decides
"may this grant write" from (motet#96), and the callback is careful to record only
`asked ∩ granted` so a read-only source can never *look* writable; a seed that wrote whatever
a caller sent would give that gate a wrong answer. Only the scopes Motet asks Google for are
accepted.

**The production safety property is a boot refusal, not a permission check.**
`MOTET_TEST_FIXTURES` must be exactly `1` — `mint_session`'s rule, not `config._truthy`'s,
because the symmetric mistake here switches a destructive surface *on* — and
`fixtures.check_startup` runs in the API's lifespan before anything else, raising where the
resolved deployment environment is production. Cloud Run reports a failed revision and
never shifts traffic to it. The environment comes from the `deployment.environment`
attribute the deploy already stamps on every telemetry record
(`motet_obs.resolve_deployment_environment`) rather than from a variable invented for this:
a second name for the environment is a second thing that can disagree about which one this
is. **An unknown environment is allowed and logged at WARNING**, because a laptop and CI set
no resource attributes and the harness has to work in both — so reaching production through
that gap needs *two* independent private-repo diffs a reviewer sees, which is
`mint_session`'s three-interlocks argument with the in-repo one deliberately the smallest.
The match is the two exact spellings **plus any name containing `prod`**, so `prod-eu` and
`motet-production` refuse too — a denylist over a string written in a repo this one cannot
read is the wrong shape, and that substring is as far as it can be pushed from here. The
residue is stated rather than closed: a production environment named `prd` or `live` would
boot. **The refusal logs at ERROR and then flushes telemetry before it propagates**, because
the lifespan's own `finally` starts at the `yield` and a flush ships only what was emitted —
a refusal that merely raised would leave uvicorn's traceback in Cloud Logging, which no agent
can read, and nothing on the obs stack saying why the revision failed. `/internal/health`
reports `test_fixtures` for `vault_ready`'s reason: an agent about to drive a staging loop
can ask before it seeds, and the routes still need a bearer, so the field advertises nothing
a caller could use without one.

**`RESET_KEEPS` is complete against the schema, and a test holds it so.** It is reported on
the wire as "tables a reset never touches", which a caller asserting a baseline reads as
exhaustive; the first draft was silently short by four. `db/tests/test_fixtures_reset.py`
requires every table the migrations create to be either deleted or kept, so a new table is a
red run and a decision — `test_mcp_parity`'s rule, one layer down.

**The routes are registered unconditionally and answer 503 when the flag is off**, rather
than being registered only when it is on. A conditional route table would make
`openapi.yaml`, the reserved-path walk and the MCP parity table describe one app per
environment, and every route-walking test in this repo would pass or fail on the importing
process's environment. Hiding them would buy no secrecy either — the code is in a public
repo. They are in `mcp/registry.py`'s `EXCLUDED` for the same reason they are not a product
capability, and `test_fixtures_api.py` walks `app.routes` to prove no route under
`/v1/testing` escaped the guard, which is the `/v1/admin` walk one surface along.

**A personal access token reaches every one of these routes with nothing added here.**
They take `User` like the rest of `/v1`, and motet#142 taught `require_caller` a third key,
so the credential the agent holds is the credential the harness takes — asserted end to end
in `test_fixtures_api.py`, mint through reset and back, because the integration point that
was left "obvious" is only obvious once it is a passing test.

**What is reachable with the flag on, stated so the change can be reviewed on that basis:**
an authenticated `/v1` caller can seed a Gmail source from the staging refresh token, delete
their own sources, items and episodes, enqueue a poll or an episode and get the job id, and
read any job of theirs. Two of those destroy data. With the flag unset every one is a 503
that reads nothing.

**The invariant-12 reading, recorded as invariant 12 asks.** This adds four routes on the
existing API, two modules, a flag, and a variable in the private infrastructure repo — the
last of which is the seventh bullet's territory, and "when in doubt, it counts". The
sign-off is the owner's request above rather than the size of the diff. What it does *not*
add: no deployable, no datastore, no migration, no queue mechanism, no new role for an
existing table, no vendor, no seam, no inference stage and no model call. The vault gains no
third kind of record and no wider grant — the credential it writes is
`source_credentials`' own, written by `source_credentials`' own function.

**The error reporter had to stop attaching frame locals for this to be safe, and that is
a change to `obs/` rather than to this surface.** `sentry_sdk` attaches per-frame variables
on a switch of its own that defaults to *on*, and `send_default_pii=False` does not cover it
— that governs request bodies, headers and identity. Two places hold a mailbox refresh token
in a local inside a `try` and log with `exception()` there **by design**, because a traceback
is the only thing that tells a genuine bug apart from a KMS refusal: `oauth_callback`, whose
`grant` is a *real user's* Gmail token, and the seed, whose `secret` is the fixture's. With
locals on, one unreachable keyring posts a live credential to GlitchTip — searchable, in the
store invariant 8 spent a subsystem keeping it out of. So `include_local_variables=False`,
and the cost is stated rather than hidden: every event loses the per-frame variables that are
the first thing a person reads. `obs/tests/test_alert_scoping.py` asserts it off the wire
against a local named **`grant`** rather than `secret`, because `sentry_sdk`'s own scrubber
filters the second *by name* and a test using it is green against a leak it never prevented.

**What no test here can tell you** is whether the refresh token in staging's Secret Manager
is live: invariant 9 keeps vendors out of CI, so what runs offline is the decision procedure
over the fake mailbox — which serves complete RFC 822 messages, so the pipeline meets the
shapes it will meet in staging. The first real seed is against staging, once the private
repo has created the secret and injected it.

**Deliberately not done, with the reason.** The reset removes no **audio objects**: the
object store's interface has no delete, so a rendered episode's bytes outlive its row. In
staging that is one unreferenced object per rendered episode, reachable only through the row
the reset removes; adding deletion means a method on the storage seam, which is a larger
change than this one.

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

### Motet is an MCP server, and the route table is what keeps it at parity

`api/src/motet_api/mcp/`, `/mcp` on `motet-api`, migration 0020,
`web/src/screens/McpAuthorizeCallback.tsx`. **Decided by Tadas, 2026-09-13, on motet#111.**
The issue laid out the options and recommended one of each. The implementing session put them
to him as a one-line list (the design comment on the issue), and he answered in Zimmer session
17819: *"Take the recs, except C2, H2,"*. That reads as A1 B1 C2 D1 E1 F1 G1 H2 I1. His go
on the batch had been *"I have a few more issues coming in - monitor for and get started on
em"*, which started the work and chose nothing — which is why the list was asked.

What was chosen, and what was rejected with it:

| | Chosen | Rejected |
|---|---|---|
| Where | **A1**: mounted on `motet-api` at `/mcp` | a separate `motet-mcp` service calling the API over HTTP (a deployable, an image, IAM, a second copy of every route's shape: zimmer's shape before zimmer#129 replaced it); doing nothing |
| Tools | **B1**: hand-written, in-process, one module per group, held at parity by a test | tools generated from OpenAPI with the third-party `fastmcp` package (HTTP-shaped names, two tools for one fact, and it still needs the exclusion list); a capability registry that routes and tools are both built from (rewriting every working route for one new client) |
| Auth | **C2**: per-user OAuth now, *against* the recommendation | the `/v1` bearer as the only way in — it still works, below |
| Admin | **D1**: an opt-in group, `?tool_groups=admin` | a separate `/mcp/admin` mount |
| Surface | **E1, F1**: tools only | MCP resources for episodes and transcripts; a `make_todays_briefing` prompt |
| Voice | **G1**: the voice service's binding and credential are a follow-up issue | minting a scoped voice credential now |
| New routes | **I1**: the voice-session route is an exclusion | a tool for it |

**The parity rule: a route without a registry entry or an exclusion is a red run.**
`api/tests/test_mcp_parity.py` walks `app.routes` and fails on any operation that is neither in
a tool's `covers` in `mcp/registry.py` nor in `EXCLUDED` with a written reason, and on any entry
naming an operation that no longer exists. Zimmer's MCP server has no such test — its parity is a
pre-PR skill and a hand-written table, and its drift is a standing list of issues — and this is
the part of zimmer's precedent that was deliberately *not* copied. Adding a route therefore means
deciding its MCP counterpart in the same PR. The server also refuses to build when the registry
and the functions in `mcp/tools/` disagree, so a tool cannot be half-added either.

**A tool calls its route's handler, not a copy of its logic.** The issue proposed moving the
logic out of the handlers that hold it (paste, episodes, connect, integrate) into functions both
callers share. Calling the handler itself is the stronger form of the same rule — there is
nothing in a tool to drift — and it needed no refactor of a single route. `mcp/context.run` gives
the handler `deps.connection` itself, so the commit, the rollback and the post-commit drain nudge
(motet#71) happen for a tool call exactly as for a request, and an `HTTPException` becomes a tool
error in the route's own words: `404: No such episode.` A limit a route enforces with FastAPI's
`Query` is the one thing a direct call skips, and the tools that take one check it themselves.

**Stateless, JSON, and three traps**, each verified against the installed SDK rather than taken
from its docs:

- **DNS-rebinding protection is off.** On, every request whose `Host` is not localhost is a
  `421 Misdirected Request`. Cloud Run's frontend owns `Host`, the credential is an explicit
  header rather than a cookie, and the hostnames are a private-repo fact this public repo cannot
  allowlist. The tests use a non-localhost `Host` so that a regression is a failure.
- **A session manager runs once per instance**, and a process starts the app's lifespan many
  times — every test that opens a `TestClient` does. So `McpMount.running()` builds a fresh
  transport inside each lifespan, and `/mcp` answers 503 outside one.
- **`/mcp` is a `Route`, not a `Mount`.** A mount answers `POST /mcp` with a 307 to `/mcp/`,
  which the SDK's own client follows and plenty of clients do not.

`stateless_http` and `json_response` mean no request depends on the instance that served the one
before, which is what several Cloud Run instances need; a test alternates every request between
two freshly built apps. `/mcp` and the OAuth endpoints are plain Starlette routes, so `openapi.yaml`
does not describe them — it gains only the SPA's callback route below and two health fields.

**Who gets in is `deps.require_caller`, run before the transport sees a byte.** The shared API
token in constant time, else a session row with the allowlist re-checked. The feed token is
refused — a URL in a podcast app must not be the whole API. Groups come from `?tool_groups=` in
the query string and never the body, an unknown group is a 400 rather than a quietly smaller
surface, every group has a `<group>_readonly` variant, and an admin tool still answers only a
caller its route's guard would. That makes the admin group reachable only with a signed-in
browser's session token: the shared token is never an admin and neither is an MCP grant (below).
`motet.mcp.tool_calls{tool,outcome}` counts every call, and `/internal/health` reports
`mcp_tools`.

**A guard that is a person reading something is not a tool argument.** `POST /v1/connectors`
refuses an MCP-server row without `acknowledge_risk`, and that checkbox exists because the agent
the server is handed to reads untrusted pages. An MCP client is exactly such an agent, so
`create_connector` refuses `kind="mcp"` outright rather than passing the flag through; sites
still go through it. A future route whose control is "a person saw this" gets the same treatment.

#### C2: Motet is its own OAuth issuer, and Google only says who is at the keyboard

`mcp/oauth.py`, `motet_db.mcp_oauth`. Per-user auth needs something that issues tokens a client
can obtain by itself, and Google cannot be it: its access tokens are not bound to Motet as an
audience, and it offers no dynamic client registration, so a generic MCP client could not even
start. So Motet runs the authorization server the MCP spec describes — RFC 9728 and 8414
metadata, RFC 7591 registration, PKCE, RFC 8707 resource indicators, revocation — on the SDK's
own handlers, behind a provider backed by Postgres.

- **An access token is an `auth_sessions` row** with `mcp_client_id` set and an hour's life, so
  `require_caller` verifies it without knowing MCP exists, and the allowlist is re-checked on
  every request as it is for a browser. A refresh token is a row in `mcp_oauth_refresh_tokens`,
  rotated on every use with the old access token deleted beside it, and the allowlist is checked
  again at every issue. `/v1/auth/logout-all` deletes refresh tokens as well as sessions.
- **Google returns the person to the SPA's registered `/oauth/callback`**, with an `mcp.` state
  beside sign-in's `login.`, so no redirect URI had to be registered on the Google OAuth client
  (a human-owned step, invariant 9). The SPA posts the code to `POST /v1/auth/mcp/callback`,
  which verifies the identity exactly as sign-in does and mints the client's authorization code.
- **The consent screen is the security control.** The callback's answer carries the code inside
  the URL that sends the person back to the client, and the SPA navigates there only after the
  person has seen which client is asking, where the grant will go and which account it acts as,
  and pressed Allow. Registration is unauthenticated by design, so without that step a link to
  `/authorize` with anyone's registered client would complete silently for somebody already
  signed in to Google. So a redirect URI carrying a username or password is refused on both
  sides, and the screen names the host rather than `netloc`: `https://claude.ai@attacker.example`
  would otherwise read as claude.ai. The client's name is its own claim and proves nothing.
- **Scopes limit nothing.** A client's requested scopes are stored and echoed, and an access
  token is a session row, so it reaches all of `/v1` as well as `/mcp`. What bounds a grant is
  that it is never an operator and that the allowlist decides who may approve one.
- **Registration is bounded per row, not in count**: ten redirect URIs and 8 KB each, with
  clients unused for a day swept on the next registration. There is no rate limit, for the
  waitlist's reason: nowhere to keep one that is not a new mechanism.
- **It runs wherever it is configured, and that is not the same as "a human switched it
  on".** It needs `MOTET_PUBLIC_BASE_URL` (the issuer), `MOTET_APP_BASE_URL` and a working
  sign-in; without them the OAuth endpoints are 404s, `/mcp` takes only the bearer, and
  `/internal/health` says `mcp_oauth_configured: false`. **None of those three is an OAuth
  variable.** `MOTET_PUBLIC_BASE_URL` is the RSS enclosure origin (`deps.public_base_url`),
  set in a deployed environment so the feed does not advertise `run.app` links; the other two
  are the SPA's origin and the sign-in allowlist. So both deployed environments satisfy the
  condition already, and the authorization server came up on the first image bump after
  this shipped rather than on a deliberate act — which is what the merge gate held motet#111
  to say out loud, and what the owner was asked to confirm before it merged. **A future
  capability whose activation is a security boundary should not infer its own switch from a
  variable set for another purpose** — give it its own, and say in the PR which environments
  it turns on in.

**What no test here can tell you** is whether a real MCP client completes discovery against a
deployed issuer, and whether a person gets through the real Google sign-in and the Allow screen.
An agent cannot sign in with Google (see the sign-in section), so the first real connection is a
human's.

**What this adds, read against invariant 12:** a second contract on the existing API (`/mcp`
and the authorization server's endpoints), one SDK on `motet-api` only (`mcp>=2.2,<3`, and
`bin/build-images` asserts it is in the image), three tables and two columns used for exactly one
thing each (migration 0020), and one route. No deployable, datastore, queue mechanism, stage or
model call.

### The landing page is a static site Cloudflare builds, and its one write is the waitlist

`site/`, `POST /v1/waitlist` (`motet_api.waitlist`), `GET /v1/admin/waitlist`, migration
0017, `web/src/screens/AdminWaitlist.tsx`. **Asked for by Tadas, 2026-09-13, in Zimmer
session 17816's goal**: *"a homepage landing page for motet, in accordance with the
in-flight brand guidelines · deploy it on cloudflare pages, similar to how we do e.g. Zimmer
docs · signup is just a waitlist for now. have it submit to an endpoint that collects the
form and shows any submissions in the admin screen for now."* That names the deployable,
the host, the endpoint and where submissions are read, which is invariant 12's sign-off for
each of them; the choices below are the ones it left open.

**The site is Cloudflare Pages' Git integration building `site/`, the way `tadasant/zimmer`'s
`docs/` is built** — root directory `site`, `npm run build`, output `dist`. Cloudflare pulls
from the repository; nothing here pushes to Cloudflare. That is what reconciles it with the
decision the SPA's hosting records in the private repo, where Pages lost to Cloud Run
because a direct upload needs an account-level `Cloudflare Pages: Edit` token in CI: the Git
integration needs no credential in CI at all, so the reason that decided the SPA does not
apply here, and the SPA stays where it is. Creating the project and connecting the
repository (a GitHub App consent) are one-time human steps under invariant 9, and so is
writing the apex record: the private repo's Motet invariants make DNS a named boundary of its
own — agents read the zone, a human sets records — which overrides the "adding a DNS record"
example invariant 9 gives above. The runbook belongs to the private repo, not to this file.

- **No framework and no dependencies.** The brand allows two webfonts and nothing else, so
  the page is HTML, one stylesheet and one small script, and `build.mjs` is standard-library
  Node. `bin/ci` runs the same `npm run build` Cloudflare does, plus the build's own tests.
  **One script on the page is not ours and never appears in this tree**: Cloudflare injects
  its Web Analytics beacon into every proxied HTML response on the zone (auto-install, on
  since 2026-08-23), and the page's `_headers` carries the two sources that let it run —
  `https://static.cloudflareinsights.com` on `script-src`, and `'self'` on `connect-src`,
  because automatic injection reports to this origin's own `/cdn-cgi/rum` rather than to
  `cloudflareinsights.com`. Until motet#141 the policy blocked it and the site produced no
  analytics at all. **The invariant-12 reading is that this repo adds no vendor**: the zone
  already injects the beacon whatever `_headers` says, the dashboard setting is the owner's
  and outside this repo, and all a policy can do is decide whether what arrives runs. The
  host is pinned and the *path* is not, deliberately — `_headers` gives the reason, and it
  is robustness rather than narrowness.
- **The API origin is the build's one input**, `MOTET_API_BASE_URL`, set per Pages
  environment — the same reason `web/`'s container reads it at start: no deployment's
  hostname lives in this public repo. It is filled into the form's `action` and into the
  Content-Security-Policy in `_headers`. **On Cloudflare an unset value fails the build**,
  because a form posting to localhost looks perfect and collects nothing.
- **The page works without its script.** Each form is a real `<form>`; the script only
  keeps the visitor on the page.

**The endpoint needs no CORS configuration, and that is the design rather than a shortcut.**
The form posts `application/x-www-form-urlencoded` with no custom header — a CORS simple
request, sent cross-origin with no preflight — and the route answers with
`Access-Control-Allow-Origin: *`. A wildcard is wrong everywhere else in `/v1` and right
here, because nothing on this route is credentialed: a hostile page can do nothing through
a visitor's browser that `curl` cannot do directly. So the API gains no variable naming the
landing page's origin, and a Pages preview posts to staging unchanged. JSON is refused with
a 415, because a JSON body would be preflighted and the SPA's exact-origin policy would
refuse that preflight. A caller that does not ask for JSON — a form posted without
JavaScript — gets a small HTML page, deliberately not a redirect back: knowing where "back"
is means an origin variable or echoing a caller-supplied URL into `Location`, which is the
shape `Settings.callback_uri_allowed` already declines on an unauthenticated route.

- **One row per address, held by the database.** The address is trimmed and lowercased and
  `INSERT … ON CONFLICT` on it, so a repeat bumps `submissions` rather than adding a row.
- **A known address and a new one get the same answer**, and so does a filled honeypot
  field (`motet_hp`, a name no autofill recognises, so a real person is never quietly
  caught by it) — the route is not an oracle for who is on the list, and a bot is not told
  what caught it.
- **No address reaches a log line, a metric or the error reporter.** Outcomes are counted
  on `motet.api.waitlist_submissions{outcome}` — `joined`, `already_listed`, `honeypot`,
  `invalid`, `too_large`, `unsupported_media_type`, `store_failed` — and logged by outcome
  alone. A failed write is caught and logged by exception *type*, answered 503: escaping, it
  would carry the address to GlitchTip as a frame local, and a constraint violation's message
  quotes the row. The
  refusals are counted because a form posting somewhere wrong and a waitlist nobody joins
  are otherwise the same empty table.
- **No rate limit**, stated rather than overlooked: there is nowhere in this stack to keep
  one that is not a new mechanism, nothing is sent or granted, and a flood of fresh
  addresses costs rows. The body is capped at 4 KB.
- **`waitlist_signups` is not `users`.** Signup is still out of scope; an address here is a
  request to be told, and nothing references the table.

**It is read on the operator view, under the operator view's guard.** `/v1/admin/waitlist`
takes `Admin` like every `/v1/admin` route, is keyset-paged on the id like the jobs list,
and is fetched when the screen opens rather than on its three-second poll. It is its own
component so that `Admin.tsx` gains one line.

**What this adds, read against invariant 12:** one deployable (the static site, on a host
the owner named), one table, one public and one admin route on the existing API, and no
vendor, seam, queue mechanism, model call or resource in the private infrastructure repo
beyond the Pages project and its DNS, which are human-owned. Deliberately not added: a
confirmation email (a vendor), a redirect variable, and a rate limiter.

#### A stored address also tells Slack, and the webhook is the whole of the configuration

`motet_api.slack`, `deps.waitlist_alert`, `motet.api.waitlist_alerts{outcome}`.
**Asked for by Tadas on 2026-09-20**, who named the mechanism as well as the outcome: an
incoming webhook, posting to `#motet-updates` in the Tadasant Slack, whose URL Cloud Run
injects from Secret Manager as `SLACK_WEBHOOK_URL`. The table was already the record and
the operator view already showed it; what was missing is that nobody was told, and a
waitlist you have to remember to go and look at is a waitlist nobody reads.

**The invariant-12 reading, recorded as invariant 12 asks.** A new vendor and a secret in
the private repo are two of its bullets, so this needs a sign-off and the owner's request
*is* it — he chose the vendor, the credential shape and the variable's name, and a parallel
session on the `motet-production` root is provisioning the secret. What it adds beyond that
is one module, one field on an existing response, a counter, and a second best-effort call
on the post-commit hook the drain nudge already uses: no deployable, datastore, queue
mechanism, inference stage or model call. Deliberately not added: a retry, a queue, a
second variable naming the channel, and a second variable naming the environment.

- **It is the drain nudge's shape, one route along.** The route arms the alert beside the
  write and `deps.connection` sends it after `conn.commit()` — so an alert is never sent
  for a row that rolled back, and a request that fails on its way to the response sends
  nothing. Deliberately **not** a background task, for the reason `deps.connection` already
  records: Cloud Run throttles a container's CPU between requests, so a task scheduled
  after the response may not run until the next one arrives. The cost is a bounded three
  seconds inside the request, which is shorter than the drain trigger's five because it
  buys less — a drain that does not fire delays somebody's paste, an alert that does not
  fire costs a message about a row that is already safely stored.
- **Off unless `SLACK_WEBHOOK_URL` is set, and that is the *normal* state rather than an
  error.** This merges and runs in both environments before the secret exists, so unset is
  one DEBUG line per submission and no request. A URL that is *set* and is not https with a
  host says so at ERROR at startup — somebody meant to wire it and the value is wrong,
  which is a different thing — and the refusal never repeats the value.
  `/internal/health` reports `waitlist_alerts` for `vault_ready`'s reason: a deployment
  nobody has wired and one whose webhook was revoked look identical from outside.
- **The URL is the credential, so it is never logged, never echoed and never in a repr.**
  Anyone holding it can post into the channel. Failures are reported by exception *type*
  and status code, never with `logger.exception` — a traceback out of httpx carries the
  request URL, and the error reporter captures frame locals. Slack's refusal body *is*
  logged, because `invalid_token` is what tells a revoked webhook from a broken one, and it
  is **redacted first**: Slack does not echo the URL today, and that is a promise about a
  vendor rather than a property of this code. A test asserts it against a body that does.
- **No channel is sent.** An incoming webhook binds its own destination when it is created,
  so the payload is `{"text": …}` and nothing else, and this repo names no channel.
- **The address is the payload, and the promise one section up is intact.** "No address
  reaches a log line or a metric" still holds: the address goes to Slack the way it already
  goes to the table and the admin screen, and to nowhere else. The metric carries an
  outcome and nothing else. The address is escaped for Slack's markup before it goes,
  because `normalize_email` is deliberately loose and does not exclude the three characters
  Slack reserves.
- **Which deployment sent it is read from what the deploy already sets**, never from a
  variable of its own: `deployment.environment` in `OTEL_RESOURCE_ATTRIBUTES` — the label
  every span and metric already wears — falling back to the *host* of
  `MOTET_PUBLIC_BASE_URL`, which a deployed environment sets anyway. Both environments can
  post to a webhook, so an unlabelled alert is ambiguous, and inventing a second pair of
  variables for a fact the deploy already states is what invariant 11 warns against.
- **A resubmission of a known address is announced too, and says so.** Every outcome that
  carries a real address arms an alert; a refusal has none, and the honeypot's 200 is a lie
  told to a bot on purpose, so alerting on it would make the endpoint an oracle in a
  channel instead of in a response. The cost, named rather than discovered: there is no
  rate limit on this route by design, so a script replaying one address posts one Slack
  message per submission. `motet.api.waitlist_alerts{outcome}` is what would say so, and
  the one-line fix is to arm only on `joined`.

**What no test here can tell you** is whether the real webhook works: nothing in this repo
reaches Slack (every test drives the real client over `httpx.MockTransport`), the secret
does not exist yet in either environment, and the first real alert is a visitor's.
### Models, spend, and the settings that only staging honours

**Sign-off: PENDING-TADAS — this line is replaced with the owner's answer before the PR
merges (motet#92's design session).**

`GET/PUT /v1/admin/llm-config`, `GET /v1/admin/llm-spend`, migration 0015,
`motet_db.settings`, `motet_db.llm_usage`, `motet_workers.llm_context`,
`web/src/screens/ModelsAndSpend.tsx`. Two questions an operator asks about the LLM seam
had no surface: *which model is dedup on, and why?* — motet#85 was an afternoon of a shell
export winning over a `.env` line with nothing saying so — and *what is this costing, per
stage and per user?* The metric answers the fleet and a log line answers one episode;
nothing could be summed per user.

**Three pieces of it are invariant 12's. Each is built to the design session's proposed
default, and the sign-off line above is what says whether the owner took it:**

1. **`llm_usage` is a ledger: one row per completion, appended by the worker, summed by
   the API, never updated.** It is the only shape that yields per-user spend, because the
   metric must not carry an id. It is also a second source of truth beside
   `motet.llm.tokens`, and the two *will* disagree — a row the worker could not write is
   logged and dropped, a metric batch the exporter could not ship is lost the other way.
   Neither is the other's audit.
2. **`settings` is runtime model configuration, and production never reads it.**
   `MOTET_SETTINGS_WRITABLE` (parsed once, in `motet_db.settings`, fail closed) gates the
   *read* as well as the write: unset, the API answers a `PUT` with 409 and the worker
   loads no row at all. So a row that reached the table some other way — a restored dump,
   a flag switched off after a save — is inert, and production resolves from the
   environment alone. A laptop (`bin/local-env` writes the flag) and staging set it.
3. **Where settings are honoured, "an unknown slug is a startup crash" is traded for "a
   bad row is an ERROR and the job runs on env".** The rule survives in production intact.
   Where rows are read, the worker reads them **once per job**, validates them with
   `validate_overrides` — the same function the `PUT` writes through and the boot check
   calls — and, if they do not resolve, ignores *all* of them and says so at ERROR. A
   dropdown must never be able to stop the pipeline. Once per job rather than per
   completion, so dedup's first pass and its second look cannot straddle a change. The
   worker's boot log adds a line saying which rows are in force, because the `llm:` line
   above it no longer describes every job; `/internal/health` reports
   `settings_writable` and `llm_overrides_in_force` for the same reason. **That makes the
   health route touch the database where settings are writable** — the one exception to
   its answering without one, which is why the answer is cached for thirty seconds and the
   query carries a statement timeout: the route is public and is the platform's probe.

**"A row resolves" has to mean "no request built from it is refused"**, and the fresh-eyes
review of the first draft found the hole: `validate_overrides` ran `load_config`, but the
catalogue checks `build_request` makes per request — the output ceiling and the 1h cache
TTL — never ran, so `dedup → openai/gpt-5.1` saved cleanly and then every paste was
refused. `STAGES_CACHING_ONE_HOUR` now declares which stages ask for the hour, `_check_model`
refuses a model without it for env and row alike, the dropdown offers only
`models_for(stage)`, and two tests pin the declaration to the real prompt builders and every
stage's ceiling to every catalogue model. **A key under `llm.` that names no current stage
is ignored with a warning, not refused** — refusing an orphan would let one stale row
disable every other override and block every save, with no route able to delete it.

**The screen resolves against the API's environment, and only the worker's decides what a
job runs.** They are separate service definitions, so `MOTET_SETTINGS_WRITABLE` and any
`MOTET_LLM_*` must match on both — set on the API alone, a save answers 200 and no job ever
reads it. The screen says so; reporting the worker's own view would be new structure.

**A row is gated harder than an environment variable, deliberately.** A model row must be
in the catalogue even where `MOTET_LLM_ALLOW_UNLISTED_MODEL` is set: that escape hatch is a
property of a deployment, for the hour between a vendor shipping a model and this repo
catching up, and a row outside the catalogue is a typo. `off` is offered on every stage,
because it is legal on every catalogue model and the only pairing for one with no effort;
the dropdown marks each stage's default so turning thinking off is visibly a departure
from it. The `PUT` locks the table against other writers for its transaction, so two saves
racing on one stage cannot each validate half of a pairing that together does not resolve.

**Spend is priced where it is read, per model and per cache TTL, and three details are
load-bearing:**

- **A response's `model` is the served snapshot**, and `anthropic/claude-sonnet-4.6`
  answers as `anthropic/claude-4.6-sonnet-20260217` — a shape no suffix rule recovers.
  `ModelSpec.canonical_slug` maps it back; without it every real completion would price
  as an unknown model.
- **A cache write is billed at its TTL's rate**, and dedup caches for an hour (1.6× the
  five-minute rate) while the script stage caches for five minutes. The usage block does
  not say which, so the request's `cache_ttl` rides onto the response and into the row.
- **A model the catalogue cannot price is counted, not treated as free**:
  `unpriced_completions`, and the SPA shows the total as a floor.

Prices and snapshots are drift-checked by `bin/check-openrouter-models` beside efforts;
they used to be a dated comment. The aggregate reads a seven-day column and a total over
what is retained; retention is **90 days**, swept by `prune_jobs` on the same bounded,
autocommit, oldest-first shape as the job rows. The ledger is written **after** `_execute`'s
transactions settle, because a completion billed inside a job that then rolled back is
exactly the row that must not vanish with it — and a write that fails is logged, never a
job failure. No foreign keys: a row outlives the item or episode it is about, and `user_id`
is resolved from the subject at insert so the aggregate never joins for money.

**Voice spend stays a metric.** The voice service has no database (invariant 2), so it
installs no sink and its turns are in no ledger row — the screen says so. Queue spend maps
`integrate` to dedup and its second look and `script` to the script stage alone; revisit
if the script queue grows a second model call.

**Rejected:** a Grafana-only spend panel over `motet.llm.tokens` (one source of truth and
no table, but no per-user number, which is the one this was for); runtime config in
production (it would give up the startup-crash rule where it matters most); and `costs` on
`/v1/admin/overview`, which the prototype did — that route is polled every three seconds for
queue state, and a sum over the ledger is neither cheap enough nor fresh enough to re-ask
at that rate, so spend is its own route, loaded on demand.

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

### Credentials are a second kind of sealed record, and adding a site is the opt-in

`db/src/motet_db/connectors.py`, `api/src/motet_api/connectors.py`,
`motet_sources.mcp_oauth`, `web/src/screens/Credentials.tsx`, migration 0018. **Decided by
Tadas on 2026-09-13, in motet#102's design session** (Zimmer session 17776), which put eight
questions to the owner against the prototype on `wip/11-credentials-enrichment`. The picks,
so the record is whole before the pipeline that uses them lands:

| | Question | Picked | Rejected |
|---|---|---|---|
| A | How an item is chosen for fetching | **A2** a deterministic rule — a link to a site the owner added — with no model call | A1 model triage on every item; A3 both |
| B | On by default or opt-in | **B3** opt-in per domain: a `site` row *is* the allowlist | B1 on for every item; B2 per source |
| C | Spend and time bounds | **C2** wall clock, tool calls, a per-item and a per-user-per-day dollar cap; hitting one is a recorded skip | C1 the prototype's timeout alone |
| D | Where the agent runs | **D2** its own deployable and service account, with no KMS and no database reach | D1 a subprocess of the worker; D3 no image yet |
| E | The mailbox as the magic-link channel | **E1** remote MCP servers over OAuth 2.1, **with the risk made clear when connecting** | E2 one narrow login-link tool; E3 no mailbox |
| F | Which publishers first | **F2** no publisher-specific code | F1 one publisher's shortcuts |
| G | The stealth browser | **G3** stealth only on sites the owner added, navigation locked to the article's site | G1 stealth everywhere; G2 a plain browser |
| H | Where the article is stored | **H1** over `source_items.text`, the preview kept in `original_text` | H2 raw bytes in object storage (#91's deferred question) |

D2's resources are a change to the private infrastructure repo and are tracked there
(tadasant-internal#2837). This section is about the table the rest of it reads from.

**A `site` row is the allowlist as well as the credential** (B3). Nothing is fetched from a
domain without one, so its username and password are both optional: a site readable from
the newsletter's own link needs neither, and one that emails a code needs only the
address. A password with no username is refused by the table, because nobody can log in
with it. The domain is normalized to a bare host — scheme, `www.`, path and port gone — and
an IP literal is not a domain, because a site is a publication. **That is not an SSRF
guard**: a host name can resolve inwards, so whatever fetches from a site checks the address
it resolves to at fetch time.

**An MCP server is kept (E1), and the risk is stated at the moment it is added rather than
buried.** The agent that is handed a server also reads pages nobody at Motet wrote, so a
hostile page can steer it into using the server with the owner's account. The Add panel
says that in two paragraphs and a checkbox; **the API refuses `POST /v1/connectors` for an
`mcp` row without `acknowledge_risk`**, so a client that never rendered the warning cannot
skip it, and `risk_acknowledged_at` records when it was given. The screen is the explanation
and the API is the control — the same split the admin view keeps between the sidebar link
and the 403.

**It is the vault's second kind of sealed record, on `source_credentials`' exact terms.**
Envelope columns nullable as a group, AAD `user_id:connector_id:kind`, sealed by the API's
`DekWrapper` and opened only by a worker's `KeyManager` (invariant 8). The IAM grant is
still the control; the split is what stops a well-meaning route from needing it widened.
No route answers with a secret — `has_secret` is the whole of what the screen knows.

**Authorizing a server is the third flow on `/oauth/callback`**, told apart by a
`connector.` state prefix exactly as sign-in is by `login.` — keep
`motet_api.connectors.CONNECTOR_STATE_PREFIX` and `web/src/oauth.ts` in step — and the mailbox
callback refuses a connector state before consuming it, for the reason it already refuses a
sign-in's. The state row rides `oauth_states` with a `connector_id` column beside
`source_id`, which is a foreign key to `sources` and could not be reused.

**Nothing on the connector changes until consent completes.** What discovery and registration
produced for an authorization — issuer, client id, token endpoint, resource, and whether the
server promises RFC 9207's `iss` — rides the state row (`oauth_states.oauth_client`) and is
written onto the connector by the callback, beside the grant it issued. So an abandoned
re-authorize leaves a working server's client and grant as they were, and a failed one
records its reason without demoting a `ready` row. A recorded client is reused only while
its issuer *and* the redirect URI it was registered with are unchanged, and that redirect
URI must be this deployment's own callback, checked as sign-in checks it: a dynamically
registered client accepts whatever it was registered with, so the server's own check proves
nothing. A server that promises `iss` and omits it is refused, because that is the response
a mix-up attacker strips.

**The OAuth 2.1 client is in `motet_sources`, not the API**, because a worker has to refresh
a token set before handing the server to the agent and the worker cannot import the API.
It is the MCP specification's composition of RFCs 9728, 8414, 7591, 7636, 8707 and 9207 and
nothing more: a public client only, and a server without dynamic client registration is
refused with a sentence rather than half-supported, since a pre-registered client id is a
human step this does not automate. **Every URL a server hands it is untrusted, so two guards
stand in front of them.** Each request must be `https` to a host that resolves to a public
address — a metadata document must not be able to point the API at the metadata server —
and the authorization endpoint must be `https` too, because the SPA hands it to
`window.location` and a `javascript:` URL there would run in Motet's own origin. "Public"
also excludes the NAT64 and IPv4-compatible IPv6 blocks `ipaddress` counts as global, and
the HTTP client ignores ambient proxy settings, since through a proxy the guard would be
checking a host the connection never reaches. The address check runs before the request
rather than pinning the connection, so a DNS answer that changes in between is not covered;
the docstring says so rather than implying more.

**The invariant-12 reading, recorded as invariant 12 asks.** This adds a table, a new role
for the vault, API routes and a screen — every one inside the design session above. Nothing
reads a connector yet: the enrichment pipeline that does is its own change, and the screen
says nothing is fetched until enrichment is switched on for the deployment.

### The article behind the newsletter is fetched by an agent in a container that holds nothing

`enrich/` (`motet-enrich`), `motet_workers.enrich`, `motet_db.enrichment`, migration 0022,
`GET /v1/source-items/{id}/enrich-transcript`. **Decided by Tadas on 2026-09-13, in
motet#102's design session** (Zimmer session 17776) — the same session that chose the
Credentials half above, and the picks table in that section is this section's too. This is
options **A2, C2, D2, F2, G3 and H1** built; **B3** and **E1** shipped with the connectors.

A newsletter is usually a preview and a link, and a briefing made from the preview is a
briefing made from an advertisement for the article. So an item whose links reach a site the
owner has added goes to an agent first: it opens the link in a stealth browser, logs in if
the site walls it, and returns the article, which replaces the preview in
`source_items.text` (H1) with the newsletter kept in `original_text`.

```
"Ingest now" → does this item link to a site the owner added?
    no  → integrate, exactly as before
    yes → enrich job → [motet-enrich runs the agent] → integrate (enriched)
```

**The decision is a rule, not a model call** (A2), and the prototype's was the other way
round. A `triage` stage asked Haiku about every ingested item — a fifth `LlmStage`, a model
call where none existed, and a per-item cost on the volume line. What replaced it is one
sentence: *the newsletter carries a link whose host belongs to a `site` connector*. Adding
that connector is the opt-in (B3), so there is no second switch to forget, nothing is
fetched from a domain the owner never named, and the decision costs nothing.

**The links had to be kept for it, and that is the one change this makes to ingestion.**
`motet_sources.extract` throws every href away on purpose — a briefing is spoken and a
600-character tracking redirect is not a sentence — so the rule had nothing to read.
`source_items.links` is the answer: collected from every part of the message, under the same
visibility rules the text is (a link inside a hidden preheader is machinery), and `text` is
byte-for-byte what it was. **Rows written before migration 0022 have an empty array and can
never be enriched**, because the raw message is not retained; a re-poll inside the window is
the repair and after the window there is none.

**The rule ties a link to a site and not to the sender, which is a consequence of A2 + B3
rather than a gap.** A `site` row is the allowlist, and it matches subdomains because a
publisher's click-tracking host is one — so *any* newsletter the owner ingests can point the
agent at a subdomain of a site they added, with that site's cookies seeded and its password
in the browser server's environment. Binding the rule to the sender as well would be a
different rule than the one the design session picked; it is the obvious next tightening if
it ever matters.

**A tracking link on an unrelated host is invisible to the rule, and that is stated rather
than fixed.** `url3396.example.com` matches `example.com` because it is a subdomain; a
generic `ct.sendgrid.net` wrapper does not, because the only way to learn where it lands is
to follow it — which is a fetch from a domain the owner never named. F2 is why there is no
publisher table to fix it in.

#### Why it is a deployable of its own

**Any process in a Cloud Run container can mint that container's service-account token from
the metadata server.** The enrichment run is third-party npm — Pi, `pi-mcp-adapter`,
`playwright-stealth-mcp-server`, Chromium — driving a browser over pages nobody at Motet
wrote, with the owner's mailbox in reach through an MCP connector. Running it inside
`motet-worker` would hand that code KMS decrypt and Cloud SQL whatever its *environment*
looked like, because the environment is not the boundary; the identity is. So option D2
puts it in a container whose service account has no project roles at all, and the
infrastructure half is tadasant-internal#2837.

That makes the split of duties the contract:

| | Holds | Does |
|---|---|---|
| `motet-worker` | the KMS decrypt path (invariant 8), the database | opens the one site login, the MCP token sets and the sealed browser state this run needs, sends them in the request, seals and stores what comes back |
| `motet-enrich` | nothing | runs the agent, answers with the article, a **redacted** transcript and the browser's new cookies |

**A service, not a job**, which is the infra issue's refinement: a Cloud Run job takes
per-run input only through execution overrides, which would record the decrypted credentials
in the execution's spec and need `run.jobs.runWithOverrides` — a permission the worker does
not and should not hold. A request body carries them in transit and nowhere else.

**The contract lives in `motet_enrich.contract` and the worker imports it**, so
`motet-workers` depends on `motet-enrich` for exactly one module. A hand-written second copy
of these shapes would be two definitions of one HTTP contract, and the first field either
grew would make them disagree silently. The arrow goes one way, and the enrich image is
built with `--package motet-enrich`, so none of the worker's tree is in it.

**Two doors.** Cloud Run's IAM check — a Google ID token in `X-Serverless-Authorization`,
audience the service URL, granted to the worker's service account alone — is consumed by the
platform before a byte reaches the process. `MOTET_ENRICH_SERVICE_TOKEN` is the inner one,
compared in constant time, and it is what makes a container that becomes reachable some
other way still refuse to spend an OpenRouter key.

#### What bounds a run, and what bounds the damage

**The caps are two-sided** (C2), because the two halves can see different things. The
service enforces the wall clock, the tool-call count and the per-item dollar figure, from
the agent's own event stream — Pi has no "stop after N calls", so the runner reads the
stream and kills the process group the moment a cap is passed. The **per-user rolling 24-hour
dollar cap** is the worker's, because only it can see `enrich_runs`. Hitting either is a
*recorded skip*, never a retry: the cap will still be spent in ten minutes, and the answer
would cost the same money to learn.

A request may ask for **less** than the service's own limits and never for more, which
matters the day a worker is rolled out ahead of the service.

**It runs under the user's serialization key, and that costs throughput on purpose.**
Invariant 6 already serialized `integrate` per user; an `enrich` job takes the same key, so
"one browser session per user at a time" comes free and two runs cannot both be writing that
user's cookies for one domain. The cost is real and worth stating: ingesting ten items that
each need an article is ten runs in series, and a run is 50–250 seconds. The lease keeper is
what makes that safe (motet#53) — a run is well inside `MAX_LEASE_EXTENSION_SECONDS` — and
shortening it would mean either a second browser per user or giving up the one-session
guarantee, neither of which this session chose.

**One item is never enriched twice, and that is the rule money rides on.** The job queue's
work fence cannot help here: everything that records a run's cost is inside the handler's
transaction, which does not commit until the agent's answer comes back. So the *one* thing
written before the money is spent — `enrich_status = 'running'`, on a side connection — is
also a replay guard: a second claim of an item in that state never starts a second run. And
a transport failure talking to the service is **not retried**, because the client's timeout
is shorter than the service's own and a read timeout more often means "the run is still
going" than "nothing happened". Without both, five attempts of a $0.50 cap is $2.50 on one
item, and `enrich_runs` — which the daily cap is summed from — would have no row for any of
it.

**Enrichment never fails the item.** `blocked`, `capped`, `timeout`, `failed`, a cap-skip
and an agent that cannot be reached at all end the same way: keep the newsletter's preview,
record the run, queue integrate. A briefing made from a preview is better than no briefing,
and that is why the `enrich` failure recorder does not mark the source item failed the way
`integrate`'s does. `MIN_ARTICLE_CHARS` is the other half of it — an `ok` answer carrying
200 characters of consent notice is *not* an article, and replacing a 1,400-character
newsletter with one is the single way this feature makes a briefing worse.

**Three guards on the browser, of decreasing strength, and the order is the point.**

1. **The container's identity holds nothing.** This is the one that actually holds. Whatever
   an injected instruction talks the agent into, the reachable blast radius is the
   credentials this one run was handed: one site's login, one browser's cookies, and the MCP
   servers the owner connected on purpose.
2. **The navigation lock** (G3), in `enrich/harness/browser-mcp.mjs`. Three rules, each
   bounding a different thing: a **top-level navigation** off the run's allowlist is
   aborted, so is a **sub-frame** one (an injected `<iframe src=…>` puts an attacker's
   origin in the page without navigating it), and so is an off-site **`fetch`/XHR/WebSocket**
   (the shape an injected instruction uses to send what it read somewhere). An empty
   allowlist refuses everything rather than disabling the lock.

   **Two limits are stated rather than closed.** Passive sub-resources — images,
   stylesheets, fonts, scripts — are not filtered, because blocking a publisher's CDN
   breaks the page outright; an `<img src="https://…/?d=…">` beacon therefore still gets
   out, and exfiltration cannot be closed in a browser that renders third-party pages. And
   a **redirect continuation is followed**, because a newsletter's tracking link is an
   allowed URL that 302s onward — which means an *open redirect* on an allowed host can
   carry the browser off-site. Refusing redirects would refuse the only link most
   newsletters carry.

   **Read the whole of it honestly**: `browser_execute` evaluates the model's JavaScript in
   the harness's own Node process, so this is a lock on the browser and not a sandbox on
   the process. What bounds the blast radius is (1).
3. **The prompt**, which says page and message text is data and never an instruction. A
   mitigation, and named as one so that nobody later mistakes it for a control.

**The browser server's environment is deliberately tiny, and that is a control rather than
hygiene.** Because the model's JavaScript runs in that process, everything in its
environment is readable by whatever the model was talked into writing. It therefore never
sees `OPENROUTER_API_KEY`, the service token, or any MCP bearer — `inheritEnv: false`, and
an explicit short list. The one secret it does hold is the **site password**, as
`MOTET_SITE_PASSWORD`, which the agent fills into a password field without ever being told
the value: so the password is in no prompt, no tool argument and no transcript. Certificate
validation is turned back **on**; the published server defaults it off for container
convenience, which on the open internet is the difference between a paywall and anyone on
the path reading the owner's session.

**Each MCP bearer is a 0600 file read by a `!command` header hook**, never a value in the
MCP document — so a token is not sitting in a config file the agent's own toolchain can
read with no tool call at all. A server's tool namespace is its **connector id**, not the
owner's label, because the namespace is what decides transcript retention and a label is
free text: a connector called "browser" must not be able to buy itself retention.

#### The transcript is redacted where it is produced

`motet_enrich.redact`. The raw stream is the worst thing in this system to store: one spike
run held the owner's mailbox search results, the body of a sign-in email, a single-use magic
link and eighteen cookies including a live session. Two mechanisms, and **the first is the
one that matters**:

1. **A non-browser tool's result is never stored at all** — not redacted, *replaced*, by a
   note giving its size and its tool. The mailbox search that finds the login email is a
   non-browser tool, so this is the rule that keeps the email's body out of the database,
   and it holds for whatever a future connector returns because it is a rule about which
   server answered rather than about what the answer looked like.
2. **Everything kept goes through the patterns**: this run's known secrets by exact match,
   then a bearer header, a credential-carrying query value (`eu=`, `token=`, …), a long
   opaque URL path segment — which is what a magic link is — a cookie `"value"`, an address.

**Rule 1 is about *which server answered*, so it covers a tool's result and not the model's
account of it.** A model told to read a code out of an email can restate it in its own
message, where only rule 2 stands between it and the database. What narrows that is that the
only assistant text stored is the **final answer with the article's fenced block removed** —
the agent's two-line verdict and its reasoning about being blocked, not a running narration
of the mailbox. Keeping the article out is also why the largest row in `enrich_runs` is not
a second copy of `source_items.text`.

**The pattern half is a backstop and cannot be complete**, which is the honest statement of
what it buys: a site that puts a session token in a shape none of them matches would have it
stored. That is why rule 1 is first and needs to guess nothing. Redaction happens **on the
service, before the transcript crosses the network**; the API route that serves it back
redacts nothing and must not start to, because a second pass at read time would be a second
definition of what is safe and the one that matters is the one that decided what got written
down.

#### What is stored, and the two new tables

Migration 0022. `source_items` learns nine columns — `links`, the triage-free decision's
`article_url` and `enrich_domain`, `enrich_status`, `enrich_error`, `enriched_at`,
`original_text`. `enrich_runs` is **a table used as a log**: one row per run, appended and
never updated, holding the redacted transcript, the cost and the tool-call count — and it is
what the rolling daily cap is summed from, which is why per-item spend here is a row where
dedup's is a metric and a log line.

**`browser_states` is the vault's third kind of sealed record**, after `source_credentials`
and `connectors`: a Playwright storage state per user per domain, which is what makes "log
in once per domain" true. AAD `user_id:<domain>:browser_state`, so a ciphertext moved onto
another user's row or another domain's fails to authenticate rather than logging one account
into another's site. The cookie *count* is plaintext and is the only thing that is: "a
session was saved and it is empty" and "no session was saved" are otherwise the same row to
anyone debugging a login that will not stick. The state comes back on **every** outcome
including a timeout, because the harness writes it after every browser call — so a run that
logged in and then ran out of clock still bought the next run a login.

**The claim's two readers learn about the new queue.** `repo._HELD_WHERE` and
`INGESTION_SQL` both ask "does this item have a job", and both asked it of `integrate`
alone. An item waiting on `enrich` would have read as *held* — on the panel, and claimable
into a second agent run — and would have been on neither surface once claimed, breaking the
property those two queries hold between them. Both ask about `queue IN ('integrate',
'enrich')` now, and 0022 adds 0005's twin index so the OR is still answered off an index
rather than by a sequential scan of every job ever run (motet#49).

#### What no test here can tell you

Invariant 7 keeps vendors out of CI, so **every test in this repo runs the fake runner**:
nothing starts a browser against a real site, reaches OpenRouter, or spends a cent. What is
pinned offline is the decision procedure and the *configuration* — the MCP document, the
model row priced from the shared catalogue, the argv, the environment each child gets —
because a typo in any of those ships green and fails at the vendor. `bin/build-images
enrich` drives the real browser inside the real image for the three claims only a running
Chromium can make.

What is left is whether a real agent gets past a real paywall, and **the first live run is
the owner's**: the issue gate forbids an agent from logging into a real site or using the
owner's mailbox to prove this works. Turning it on is configuration, and the two halves are
deliberately not the same: the **worker** needs `MOTET_ENRICH`, `MOTET_ENRICH_SERVICE_URL`
and the shared token, while the **API** needs only `MOTET_ENRICH`. The API decides whether
an "Ingest now" goes to the `enrich` queue and never calls the service, so telling it where
one is would put a fact about the private estate into the internet-facing service's
configuration for nothing. `/internal/health`'s `enrich_enabled` is therefore the API's
routing switch and not a claim that a run would succeed; the two can disagree, and the
disagreement is safe either way — a worker with no URL records every queued item as
`skipped` and integrates it on its preview. Production stays off until a human flips it.

**The per-item cap is checked before a run, so real spend reaches at most
`MOTET_ENRICH_MAX_USD_PER_DAY` plus one item's cap.** Inherent to checking a budget before
spending against it; named here so the figure in a bill is not a surprise.

**Deliberately not built, each for a stated reason.** Refreshing an MCP access token from
the handler: a refresh is an HTTP round trip *and* a re-seal, so it belongs with the OAuth
client in `motet_sources.mcp_oauth` rather than inside a job handler holding this user's
serialization lock — until it is wired, an expired grant shows up as the mailbox tool failing
and the run reporting `blocked`, recoverable by re-authorizing. The SPA's own view of the
enrich block and the transcript: the API carries both and the lifecycle drawer does not
render them yet. Raw-message retention in object storage, which motet#91 deferred to its own
session and H1 declines for now.

**The invariant-12 reading, recorded as invariant 12 asks.** This adds a deployable, a queue,
two tables, nine columns, a vault role, a vendor toolchain and two API routes — every one of
them inside motet#102's design session, whose picks are the table in the section above. The
sign-off is the owner's issue and that session, not the size of the diff. What it does *not*
add is a new inference stage or a model call in the pipeline: option A2 is precisely the
choice not to have one, and the agent's own completions are the enrichment service's, priced
from the existing catalogue and counted on their own instruments rather than folded into
`llm_usage` — which the service could not write to, having no database.

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

**The markup is structure first and brand second.** `content:encoded` is a heading per story
with a `<blockquote>` of the span its lead claim cites and the source's title in a `<cite>`
— one claim beside its source, not the whole transcript, because the feed carries every
episode on every poll. Most clients strip styling, so it must read as bare HTML; the few
that keep inline `style` attributes get `brand/GUIDELINES.md`'s type stacks and nothing
else of the palette. **No colour is set, only opacity**, because a client that keeps
inline styles paints them over its own theme, and ink on a dark theme is dark on dark.
Ink-soft is ink at .66, so the client's text colour at .66 follows the same rule on either
ground. **The finished document has every character XML 1.0 forbids stripped**
(`feed._xml_safe`). Titles and quotes are text a newsletter or a paste supplied, and one
control character anywhere made the whole feed unparseable.
Never a `<style>` block or a webfont link: clients drop the first, and the second would be a
third-party request from a private feed. Copy follows the guidelines too — "podcast" and
"episode", and citations are not the closing line.

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
