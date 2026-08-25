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
uv run uvicorn motet_api:app --reload          # the API
uv run python -m motet_workers.runner integrate  # ...and one drain per stage
uv run python -m motet_workers.runner assemble
uv run python -m motet_workers.runner script
uv run python -m motet_workers.runner tts
npm --prefix web run dev                       # the SPA
```

Each drain exits when its queue is empty, which is what a Cloud Run job wants. Pass
`--poll-seconds 2` to keep one running while you work.

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
