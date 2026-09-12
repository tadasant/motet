# Contributing to Motet

## Motet doesn't accept pull requests

Not a snub — a process decision. Feature work here is done by running agent sessions
against this repo, on a pipeline built to produce reviewed, CI-green changes. A patch
arriving out of band skips that pipeline, so it is easier and safer to feed it than to
bypass it. PRs opened from outside are closed automatically, with a pointer here.

**What helps most is a detailed issue.** A precise bug report — exact reproduction steps,
real output, what it cost you — or a concrete feature request gets triaged quickly, and the
change you had in mind may well get built.

**Forking is very welcome.** The [MIT license](LICENSE) means you can build on this freely.

## Development setup

You need **Python 3.13**, **Node 22**, **[uv](https://docs.astral.sh/uv/)**, and a
**Postgres 16**.

```bash
uv sync --all-packages     # Python workspace: api, db, inference, obs, storage, workers
npm --prefix web ci        # the SPA
```

A local Postgres, however you prefer to run one:

```bash
docker run -d --name motet-pg \
  -e POSTGRES_USER=postgres -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=motet_test \
  -p 5432:5432 postgres:16
```

Then copy `.env.example` to `.env`. `bin/ci` defaults `DATABASE_URL` to
`postgresql://postgres:postgres@localhost:5432/motet_test`, so the container above needs no
further configuration.

> Without a `DATABASE_URL`, the migration-apply tests **skip** rather than fail, so a quick
> `uv run pytest` works with no database. CI always has one, so that path is always covered
> there — but a green local run with Postgres missing has not exercised it.

**Each pytest run creates its own database** — `motet_test_run<pid>_<n>_<timestamp>`, from
the server `DATABASE_URL` points at — and drops it at the end. `DATABASE_URL` names one
database, so without this two runs on one machine truncate each other's tables mid-test
(motet#15): the loud half is a deadlock, the quiet half is a row that was written and is
not there. Two runs at once is now fine, and so is `bin/ci` while another one is going.

Set `MOTET_TEST_KEEP_DATABASE=1` to keep the database after a failing run — the name is in
pytest's header line, so `psql` on it is a copy and paste.

## One CI command

```bash
bin/ci
```

Migrations, lint, typecheck, tests, the contract drift checks, the golden set, and the SPA
build — everything CI runs, in the order CI runs it. The GitHub Actions workflow calls this
and nothing else.

**If you add a check, add it to `bin/ci`**, not to the workflow. A check that lives only in
YAML cannot be run on a laptop, and it will rot.

Individual pieces, when you want a faster loop:

```bash
uv run pytest                    # Python tests, including the golden set
uv run ruff check . && uv run mypy
npm --prefix web test            # SPA tests
npm --prefix web run typecheck
```

## The contract

`openapi.yaml` is **generated from the FastAPI app** and committed; the TypeScript client
in `web/src/api/schema.gen.ts` is generated from it in turn. `bin/ci` regenerates both and
fails on any diff.

Never hand-edit either file. Change the route or model, then:

```bash
bin/generate-openapi    # app  -> openapi.yaml
bin/generate-client     # yaml -> web/src/api/schema.gen.ts
```

## Models

The LLM provider seam lives in `inference/src/motet_inference/llm/`. Which model each
stage uses is environment configuration (`MOTET_LLM_MODEL`, plus a per-stage override) and
is validated at startup against a committed catalogue of slugs.

```bash
bin/check-openrouter-models          # catalogue vs OpenRouter's live model list
bin/check-openrouter-models sonnet   # also list live slugs matching a substring
```

It is **not** part of `bin/ci`, because CI is offline by design. Run it when adding a model
or when a slug looks stale, and update `KNOWN_MODELS` from what it reports.

**No test here calls a vendor**, not even behind an opt-in flag — invariant 7 is
absolute. The adapter is covered end to end against a stub transport instead. To confirm
a slug or a reasoning config against the live API, do it by hand outside the suite.

## Running the pipeline locally

Nothing happens on the request thread: the API writes a row and enqueues a job, and a
worker does the work. So a local run needs both.

```bash
uv run uvicorn motet_api:app --reload                 # the API
uv run python -m motet_workers.runner all --poll-seconds 2   # ...and a worker
npm --prefix web run dev                              # the SPA
```

`all` sweeps every queue in pipeline order on each pass, so a paste integrates and an
episode assembles, scripts and renders without you starting anything per stage. Name a
single queue instead (`runner integrate`) when you want one stage on its own.

Without `--poll-seconds` a drain exits as soon as its queue is empty, which is what a
Cloud Run *job* wants. **Something still has to start that job**, and for a long time
nothing did — the SPA told the user a worker would take their paste "within a few seconds"
while the only thing that drained the queue was a workflow dispatch in the private
infrastructure repo (motet#38). The polling shape is the answer to that, and
`/v1/processing` reports when a worker last ran so the SPA can say which situation a
queued item is in rather than assuming.

With `MOTET_INFERENCE_MODE=fake` (the default) none of this touches a vendor: the fakes
produce deterministic news items, a script whose claims quote their sources verbatim, and
silent WAV audio whose length tracks the text. That is enough to exercise every seam.

**Signing in locally, if you want to exercise that seam too:**

```bash
MOTET_ALLOWED_EMAILS=owner@motet.test uv run uvicorn motet_api:app --reload
```

The fake identity provider answers as `owner@motet.test` and its consent URL redirects
straight back to the SPA, so the whole round trip runs with no Google client and no
network. Without the allowlist, "Sign in with Google" answers 503 saying so — unset means
deny everybody, deliberately.

You do not *need* it: with `MOTET_API_TOKEN` unset the API is open, `/v1/auth/session`
reports `how: "open"`, and the SPA skips the sign-in screen and just works. Reach the SPA
at `localhost` rather than `127.0.0.1` if you are testing either OAuth flow — Google
matches a redirect URI as an exact string, and only one of those two is registered.

## Local development, real mode

Everything above runs against the fakes. **Real mode on a laptop needs exactly one thing
that is not a public tool: a service account key** with read access to the local-dev
secrets. Minting that key is a one-time human step (invariant 9, AGENTS.md) — everything
after it is `bin/local-env`.

> **This spends real money.** Real mode calls OpenRouter and Cartesia for real, against
> **staging's keys and staging's spend caps, shared with staging**. A full backlog is
> several dollars of grounding alone (see the grounding section in AGENTS.md), and a
> laptop draining a queue in a loop spends exactly like a deployed worker does. Run it
> deliberately, and go back to `MOTET_INFERENCE_MODE=fake` when you are done.

### Prerequisites

The dev setup above, plus:

- The key at `~/.config/motet/local-dev.json` (or anywhere — the path is yours). It is
  minted by a human, once, per the manual-setup runbook in the private infrastructure
  repo. **Do not try to mint one from a session**; that is the boundary, not a gap.
- A `motet_dev` database on the local Postgres, which is what the generated `DATABASE_URL`
  points at — `motet_test` is pytest's, and a real-mode run should not share it:

  ```bash
  docker exec motet-pg createdb -U postgres motet_dev
  ```

### The one export, then the script

```bash
export GOOGLE_APPLICATION_CREDENTIALS=~/.config/motet/local-dev.json   # the one thing
bin/local-env                                                          # writes .env
export UV_ENV_FILE=.env                                                # uv reads it per-run
```

`bin/local-env` takes the project id from the key file's own `project_id` field — nothing
in this repo names a project — lists every Secret Manager secret in it labelled
`motet-local=true`, and writes `NAME=value` for each, followed by the localhost overrides
(`MOTET_INFERENCE_MODE=real`, `MOTET_VAULT_BACKEND=kms`, local storage, `localhost` URLs).
It **never prints a value**, refuses to overwrite an existing `.env` without `--force`, and
writes the file `0600`. Re-run it with `--force` after a rotation or a roster change.

**`export UV_ENV_FILE=.env` is not optional.** `uv run` does not read `.env` on its own;
that variable is what makes every `uv run` below pick the file up. Set it once per shell,
or pass `--env-file .env` to each command.

### The loop

```bash
uv run python -m motet_db.migrate                             # against motet_dev
uv run uvicorn motet_api:app --reload                         # the API
uv run python -m motet_workers.runner all --poll-seconds 2    # ...and a worker
npm --prefix web run dev                                      # the SPA
```

Then open **`http://localhost:5173`** — `localhost`, never `127.0.0.1`, because Google
matches a redirect URI as an exact string — sign in, and paste something.

> **Verify, do not assume, that `http://localhost:5173/oauth/callback` is a registered
> redirect URI** on the OAuth client the secrets belong to. Three URIs are registered, one
> per environment, and whether a dev one is among them is a fact about the private repo
> that nothing here can check. If it is not, sign-in and Gmail connect both fail at
> Google's consent screen with `redirect_uri_mismatch`; adding one is a human step on the
> OAuth client.

### Checking it took

```bash
curl -s localhost:8000/internal/health | python3 -m json.tool
```

Three fields are the answer:

| Field | Wanted | If it is wrong |
|---|---|---|
| `inference_mode` | `real` | the `.env` is not loaded — `UV_ENV_FILE` |
| `vault_ready` | `true` | the key cannot reach the KEK, or `MOTET_VAULT_KMS_KEY` is not in the roster |
| `telemetry_exporting` | `true` | `OTEL_EXPORTER_OTLP_ENDPOINT` or the ingest token is not in the roster |

`revision` reads `local` rather than a commit, and `service` reads `motet-local`, which is
what keeps a laptop's spans out of the staging panels.

**Two things prove the pipeline actually ran, and neither is a flag:**

1. **A news item a model wrote.** Paste two write-ups of one story. The fakes dedup by a
   deterministic rule; a real model merges them and writes a headline neither text
   contains.
2. **Audio bytes on disk.** `ls -lh .motet-storage/` after an episode renders. The fake TTS
   writes silent WAV whose length tracks the text; Cartesia writes an MP3 that plays.

**If a worker refuses to start naming a variable** — `MOTET_TTS_VOICE_ID` and
`OPENROUTER_API_KEY` are the two that fail closed at boot — that variable is missing from
the roster rather than from this repo. Which secrets carry `motet-local=true` is the
private infrastructure repo's decision (tadasant-internal#2804), which is exactly what
keeps the roster out of a public repo.

### Going back

Delete `.env` (or `unset UV_ENV_FILE`). `bin/ci` never reads it: it pins
`MOTET_INFERENCE_MODE=fake` itself and needs no key, so the offline, free, deterministic
path is untouched by any of this.

## Testing against staging

Deployed environments are a different problem, and it has one answer:
[`docs/testing-staging.md`](docs/testing-staging.md). Short version — use the
`MOTET_API_TOKEN` bearer, because Google refuses to sign an automated browser in, and a
green run there still does not prove a human's sign-in works.

## Migrations

Plain numbered SQL in `db/migrations/`, named `NNNN_lower_snake_case.sql`, applied in order
and recorded in `schema_migrations`.

```bash
bin/migrate
```

**Forward-only.** Never edit a migration that has been applied anywhere — write a new one.
The runner does not track checksums, so an edited file simply never re-runs and the schema
quietly diverges between environments.

## Before you start

Read **[AGENTS.md](AGENTS.md)**. It holds the invariants — the decisions that are settled,
and why — plus the tripwires that say when the project has gone off the rails. It is the
first thing to read and the thing to update when a decision changes.
