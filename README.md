# Motet

> *A motet layers several different texts sung simultaneously into one coherent piece —
> many sources, one thing worth hearing.*

**[getmotet.com](https://getmotet.com)** — an interactive podcast built from your own
reading backlog. Newsletters and X go in; a briefing comes out that you listen to on a walk
or a drive, and can interrupt, question, and steer.

Newsletters and bookmarks accumulate faster than they get read. The backlog is guilt, not
value. Meanwhile there are 20–30 minutes a day — dog walk, commute — where ears are free
and eyes aren't.

## Status

**Phase 1 — Infra MVP.** Paste text in, get an episode out, listen on a dog walk. One
hardcoded user, shipped as a private authenticated RSS feed rather than a player.

The Phase 1 path is built end to end: paste text in, it is deduplicated into news items,
an episode is assembled from what is unread, scripted, grounding-validated, synthesized,
and published to a private authenticated feed you can subscribe to in Overcast or Apple
Podcasts. Three plain SPA screens cover paste-in, the backlog, and an episode's transcript
with every claim shown beside the source span it came from.

The factory around it is the real deliverable: one CI command, the OpenAPI contract, fake
adapters behind every vendor seam, and a twenty-case golden set.

## How it fits together

| Component | Runtime | Directory |
|---|---|---|
| API | FastAPI, Cloud Run | [`api/`](api) |
| Ingestion workers | Cloud Run jobs | [`workers/`](workers) |
| Inference adapters | library | [`inference/`](inference) |
| Schema + migrations | library | [`db/`](db) |
| Object storage | library | [`storage/`](storage) |
| Web SPA | Vite + React, static files on Cloud Run | [`web/`](web) |
| Voice service | Pipecat, Cloud Run — *Phase 2* | [`voice/`](voice) |
| iOS app | Swift — *Phase 2* | [`ios/`](ios) |
| Golden set | CI harness | [`goldens/`](goldens) |

Postgres holds the data *and* the job queue. Audio lives in GCS behind signed URLs. No
Redis, no vector store.

The core design bet: **don't run narration through the realtime model.** Pre-render the
briefing as audio, play it locally, and spin up a realtime session only when the user
speaks. That makes offline possible, makes grounding enforceable, and cuts realtime spend
to a small fraction of session minutes.

## Getting started

```bash
bin/ci        # migrations, tests, typecheck — everything CI runs
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for setup, and **[AGENTS.md](AGENTS.md) for the
invariants** — the settled decisions this project does not re-litigate.

## Repository split

This repo is **public** and holds application code. Infrastructure, staging and production
configuration, and deploy workflows live privately in `tadasant/tadasant-internal` under
`motet/`. No secret, project id, or topology detail belongs here.

## Contributing

Motet doesn't accept pull requests — see [CONTRIBUTING.md](CONTRIBUTING.md). Detailed
issues are genuinely useful. Forking is welcome under the [MIT license](LICENSE).
