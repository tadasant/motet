# Play Live — practical notes (local prototype)

Companion to `proto/issues/04-play-live.md`. How to run the thing, not why.

## Ports

| Process | Port | Started by |
|---|---|---|
| API | 8000 | `bin/dev` (already running) |
| SPA (Vite) | 5173 | `bin/dev` (already running) |
| **Voice service** | **8100** | you, by hand — `bin/dev` does not know about it |

## Starting the voice service

The service reads `.env` for `MOTET_INFERENCE_MODE=real`, `OPENROUTER_API_KEY`,
`CARTESIA_*`, `MOTET_VOICE_API_BASE_URL=http://localhost:8000` and (now) `OPENAI_API_KEY`.
It has no CORS middleware and the browser calls it cross-origin from `:5173`, so the
prototype launches it through a ~15-line wrapper that adds CORS around `create_app()`:

```bash
# from the repo root; the wrapper lives in the session scratchpad, not in the repo
UV_ENV_FILE=.env OTEL_SERVICE_NAME=motet-voice-local \
  unset OPENAI_API_KEY OPENROUTER_API_KEY CARTESIA_API_KEY CARTESIA_VERSION   # ~/.zshrc exports stale ones (motet#85)
  uv run uvicorn --app-dir <scratchpad> voice_local:app --port 8100
```

Health: `curl -s localhost:8100/internal/health | jq`. On the composed arm expect
`arm: composed`, `inference_mode: real`, `arm_conversational: false` (STT is dormant — a
`text` turn still works), `start_session_authenticated: false` (fine locally; the browser
mints its own session).

### The realtime arm — speech in, speech out (owner's decision, 2026-09-12)

**The arm is chosen by one environment variable on the voice service:
`MOTET_VOICE_ARM=openai_realtime`** (default `composed`). Nothing in the session config
picks an arm — a client never names a vendor (invariant 1). It needs `OPENAI_API_KEY` in
`.env` and `MOTET_INFERENCE_MODE=real`; without either the arm is dormant and says so on
`/internal/health` (`arm_dormant_reason`).

```bash
# (a) restart the voice service on :8100 in realtime mode — from the repo root.
# `unset` first: ~/.zshrc exports stale vendor keys that would shadow .env (motet#85).
unset OPENAI_API_KEY OPENROUTER_API_KEY CARTESIA_API_KEY CARTESIA_VERSION
kill $(lsof -tnP -iTCP:8100 -sTCP:LISTEN) 2>/dev/null
UV_ENV_FILE=.env OTEL_SERVICE_NAME=motet-voice-local MOTET_VOICE_ARM=openai_realtime \
  uv run uvicorn --app-dir <scratchpad-with-voice_local.py> voice_local:app --host 127.0.0.1 --port 8100
curl -s localhost:8100/internal/health | jq '{arm, arm_conversational, inference_mode}'
# expect: "openai_realtime", true, "real"
# then prove the process holds .env's key, by hash only — never print the value:
PID=$(lsof -tnP -iTCP:8100 -sTCP:LISTEN)
ps eww -o command= -p $PID | tr ' ' '\n' | grep ^OPENAI_API_KEY= | cut -d= -f2- | shasum
grep -E '^OPENAI_API_KEY=' .env | cut -d= -f2- | shasum   # the two hashes must match
```

Then on the socket the `ready` frame's `detail` starts with **`live conversation open`**
when the vendor socket came up, or **`live conversation unavailable (<reason>); answering
typed questions with the composed arm`** when it did not — `reason` is also a field of its
own on the frame (`insufficient_quota`, `arm_dormant`, `close_1013`, …; on 2026-09-12 it was
`insufficient_quota`, a billing fact rather than a code one). The session still opens either
way; barge-in is local and a typed question is answered by the composed arm (`text_arm` on
`/internal/health`), never by the realtime arm's own turn path — see "Typed fallback must
not depend on the live socket" in `proto/issues/04-play-live.md`.

```bash
# (b) the live verification — no browser needed. The interruption comes from the
# service's own detector (a second of quiet room, then the question, as mic packets);
# `--client-barge-in` sends the explicit frame instead, `--gated-mic` uses a -65 dBFS lead
# (a browser mic with noise suppression on — the shape that used to never fire).
UV_ENV_FILE=.env uv run python proto/live_smoke.py
UV_ENV_FILE=.env uv run python proto/live_smoke.py "What was that number they just said?" 33000
UV_ENV_FILE=.env uv run python proto/live_smoke.py "What was that number they just said?" 33000 --gated-mic
# expect: interrupted_at with trigger=openai_server_vad_emulated and context {segment_title,
# claim_text}; transcript[user] with the recognisable question; state: speaking; first
# audio_chunk: format=pcm16 rate=24000 with the latency after the last mic frame;
# transcript[assistant]; state: ready · reply complete. Seen 2026-09-12: "The number was
# 425 million dollars." with speech_stopped_to_first_audio_ms=420 (286 on the gated-mic run).
# The service log (voice.log) carries `noise floor seeded at … from a quiet|measurable frame`,
# `listener audio: … route=… rms_dbfs=… floor_dbfs=… snr_db=…` every ten seconds of mic audio,
# `barge-in: … trigger=… offset_ms=…`, `live reply latency: … speech_stopped_to_first_audio_ms=…`
# and `live turn usage: … input=… (audio=… cached=…) output=… (audio=…)`.
```

The browser version of the same check: open the Episode tab, seek the player into a claim
with a number (7.5 s of "First real briefing" is "…roughly 300 million dollars"), **Play
Live**, and speak — or, headless, stub `getUserMedia` with a `MediaStreamDestination` and
play a Cartesia-synthesized WAV of the question into it. What to look for: the status line
goes *listening…*, the "interrupted during: …" line names the claim, "You (heard): …" is
the vendor's transcript, "Motet: …" the reply, and the reply audio starts playing before
the transcript lands. The socket-level version of all of that was seen on 2026-09-12; the
browser version with a real microphone has not fired yet — the mic meter now shows the
RMS in dBFS, and a mic that sits under -55 dBFS between utterances is the case the
detector's floor was fixed for (draft 04, "Interruption is local on every arm"). If
`barge_ins` is still 0, the service log's `listener audio:` lines say what the detector
saw.

Fake mode: `MOTET_INFERENCE_MODE=fake` in the launcher's environment — free, no vendor,
silent reply audio, composed arm only (the realtime arm has no fake conversation and is
dormant in fake mode by design).

Env the service reads (all optional): `MOTET_VOICE_ARM`, `MOTET_INFERENCE_MODE`,
`MOTET_VOICE_SESSION_SECRET` (unset = ephemeral, warns), `MOTET_VOICE_SESSION_TTL_SECONDS`,
`MOTET_VOICE_API_BASE_URL`, `MOTET_VOICE_API_TOKEN` (unset — the local API is open),
`MOTET_VOICE_START_SESSION_TOKEN` (unset = open StartSession), `MOTET_LLM_MODEL_VOICE`,
`MOTET_LLM_EFFORT_VOICE` (default `off`), `MOTET_VOICE_OPENAI_REALTIME_MODEL`,
`OPENAI_API_KEY`, `EXA_API_KEY`.

## Using it

1. Open a **ready** episode on the Episode tab. The prototype player must have loaded
   (it waits on `/v1/feed`).
2. **Play Live**. The browser asks for the mic. Wear headphones, or the narration will be
   what the VAD hears.
3. Narration plays from the player. Talk over it: the status line flips to *listening…*,
   shows the barge-in evidence (`snr_db`, `speech_probability`, offset) and — new — the
   story and sentence that were playing ("interrupted during: …").
4. **Realtime arm:** keep talking — the question goes to the vendor as you say it, the
   vendor's VAD decides when you stopped, "You (heard): …" shows what it understood, and
   the reply *streams* back (you hear the first word before the sentence is done) with
   "Motet: …" behind it; narration resumes when the reply is complete. Talk over the reply
   to cut it off. The typed box stays as a fallback. **Composed arm:** type the question
   (that arm has no speech-to-text), press Enter — *replying* → the reply transcript, then
   its audio, then narration resumes.
5. **Stop Live** closes the session (the session summary lands in the voice log — on the
   realtime arm it carries `live_speech_starts`, `live_replies` and `live_usage`, the
   vendor's audio/text token counts).

Voice base URL override: `localStorage.setItem('motet.voiceBaseUrl', 'http://...')`.

## Logs

The voice service logs to the scratchpad `voice.log`. A closed session logs
`voice session closed: {...}` with `barge_ins`, `llm_completions`, `llm_tokens` (the
`replies_checked` / `replies_ungrounded` counters went with the advisory grounding check,
removed upstream in #82).

## Costs

**Composed arm:** each question is one OpenRouter completion (Sonnet 5, effort off, ≤400
output tokens) plus one Cartesia synthesis of the reply. Barge-in itself is free.

**Realtime arm:** billed per *audio token* in and out (plus text tokens for the
instructions and the episode context, which the vendor caches across turns). Audio is
forwarded only from the barge-in to the end of the reply — never the narration — so a
question costs roughly its own length plus the reply's. The per-turn line `live turn usage`
in the voice log is the number to read; the session summary sums it. Materially dearer than
the composed arm per question; the composed arm stays the cheap fallback.
