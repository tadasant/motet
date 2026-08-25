# Voice service — Phase 2

Motet's voice surface, and the **barge-in measurement harness** that settles the provider
question with a number instead of an argument.

```
POST /v1/voice/sessions                      StartSession(...) -> session_token
WS   /v1/voice/sessions/{id}/stream          audio in; transcripts, tool calls,
                                             audio chunks, interrupted_at and the
                                             advisory grounding verdict out
GET  /internal/health                        what is wired, and what is dormant
```

Runs on Cloud Run: `uvicorn motet_voice.app:create_app --factory`.

---

## The walk — what to do outside

**This is the part that matters.** The harness cannot answer the question on its own; it
needs ten minutes of real weather. Everything else is built so that those ten minutes turn
into a number.

**Carry:** your phone. Nothing else. No laptop, no live session, no signal required.

### 1. The adversary — 8 minutes, and *do not say a word*

Open the phone's voice recorder and start it. Then walk: wind, traffic, your own footsteps,
the dog, a bus, a door. **Say nothing for the entire eight minutes.** If you speak by
accident, note roughly when and mention it afterwards.

Not talking is not laziness — it is what makes the measurement exact. On a recording with no
speech in it, *every* barge-in the detector produces is a false positive, by construction, so
there is nothing to annotate and nothing to remember.

### 2. The check — 2 minutes, in the same conditions

Keep walking, same coat, same pocket. Now say **"okay motet, what was that"** roughly every
fifteen seconds — about eight times. Speak to the phone the way you would to a person beside
you, and leave real gaps.

One habit that matters: **walk for ten seconds before the first sentence.** The detector's
noise floor spends the first second or two learning the street, and a barge-in inside that
window can be missed for a reason that has nothing to do with the weather.

### 3. Back inside — four commands

Export both recordings as **WAV** (any voice-memo app can share as WAV; so can QuickTime or
`ffmpeg -i memo.m4a memo.wav`), then:

```bash
uv run motet-voice ingest quiet.wav  --run runs/quiet  --label "windy-walk-quiet"
uv run motet-voice ingest spoken.wav --run runs/spoken --label "windy-walk-spoken" --spoke \
    --label-window 12000:14000 --label-window 27000:29000   # roughly, one per sentence
uv run motet-voice replay runs/quiet
uv run motet-voice replay runs/spoken
```

`replay` runs **both provider arms against every config variant** on the same audio and
prints the table.

### 4. How to read it

- **`false/min` on the quiet run is the answer.** Below ~0.1 — one spurious interruption per
  ten minutes — is comfortable. Above ~0.5, open-mic barge-in is not the product and
  push-to-talk is; that is a real outcome and worth knowing in ten minutes rather than in a
  month.
- **Check it against the spoken run.** A variant with zero false positives that also caught
  nothing is not a winner, which is why `caught` sits next to `false/min` in the table.
- **Listen to the snippets.** Each decision wrote a short WAV under
  `runs/<name>/replays/<arm>__<variant>/snippets/`, named for when it happened, with two
  seconds of lead-in. Twenty clips is ten minutes on the sofa and it is the difference
  between a number and an explanation.
- `decisions.jsonl` beside them carries the evidence per decision — VAD probability, adaptive
  noise floor, SNR, zero-crossing rate.

Keep the recordings. **A new idea about thresholds costs a re-run, not another walk**, and
the numbers stay comparable because the audio is byte-identical.

Nothing needs a laptop outdoors: `motet-voice upload runs/quiet` pushes a whole run through
the object-storage seam afterwards.

---

## What is dormant, and on which credential

| Thing | Status |
|---|---|
| Barge-in harness, composed arm's turn detection | **Live.** No credential of any kind |
| `openai_realtime` arm — live vendor session | **Dormant:** `OPENAI_API_KEY` is not provisioned |
| `openai_realtime` arm — offline turn detection | Runs, as a **labelled emulation** of that vendor's documented server-VAD parameters. Not a measurement of the vendor, and it stays an emulation even once the key exists — see below |
| Composed arm — LLM leg | Live, through the existing OpenRouter seam (Claude Sonnet 5) |
| Composed arm — TTS leg | Live, through the existing Cartesia adapter |
| Composed arm — STT leg | **Dormant:** no speech-to-text vendor provisioned. Does not affect barge-in |
| `save_highlight`, `get_item_detail` | Coded to the contract; live when their API routes ship |
| `mark_read` | **Live** — the route exists today |
| `start_research` | **Dormant:** `EXA_API_KEY` is not provisioned |

**A key wakes the conversation, not the measurement**, and the distinction is deliberate.
`OPENAI_API_KEY` plus `MOTET_VOICE_ARM=openai_realtime` gives the arm a live vendor session
with no code change. Offline *replay* keeps using the labelled emulation, because a replay
sends recorded audio to a detector rather than to a socket — and the vendor's relay reports
what its socket says, so a replay through it would produce **zero decisions**, score a perfect
zero false positives per minute, and be crowned the winner of the comparison this harness
exists to run. `build_turn_detector` therefore never hands back the relay; the live path uses
`build_live_turn_detector` explicitly, and until something streams a recording through a real
socket, every realtime row stays marked `*(emulated)*`. A report also flags any configuration
that produced no decisions at all and excludes it from the winner, so that failure cannot
reappear silently in some other guise.

---

## Grounding, on the path that speaks

Invariant 3 — every reported claim carries a source span, validated before TTS — is a **hard
gate** on the narration path and **advisory** on the conversational reply path in this
directory. That asymmetry is a decision, made by Tadas on
[#10](https://github.com/tadasant/motet/issues/10), not an implementation that has not caught
up: a reply is generated inside a spoken turn with a listener waiting for it, and the batch
validator is a max-effort model call that cannot live in that budget. A gate there is a
silence.

**Advisory is not absent.** `motet_voice.grounding` checks every reply — for fabricated
*specifics*: a number, a name or a quotation the session's material does not contain — and
runs *behind* the reply rather than in front of it, so nothing is delayed. The checker is
ours: local, deterministic, free, no credential, no fake/real split, the same verdict in CI
as in production.

Every verdict is recorded, four ways, and that recording *is* the invariant here:

| Where | What |
|---|---|
| obs stack | `motet.voice.conversational_replies{grounded="false", checker, arm}`, plus `motet.voice.unsupported_specifics{kind}` |
| log | a warning carrying the offending number, name or quotation |
| wire | a `grounding` event, after the `audio_chunk` it judges |
| session summary | `replies_checked`, `replies_ungrounded` |

The Grafana question is `sum(rate(motet_voice_conversational_replies_total{grounded="false"}[1h]))`.
`/internal/health` reports `grounding_checker`, `grounding_advisory` and `telemetry_exporting`,
so "which check runs here, and is anything actually being exported" is answerable without
reading code. **A change that removes the recording removes the invariant**, whatever it
leaves behind.

The prompt-level containment is still there and still matters — the model is given context the
caller assembled from *already-grounded* narration, and told to answer only from that or from
a tool result, to decline rather than fill gaps, and to quote or fetch numbers rather than
recall them.

**Two things the check does not do.** It does not judge paraphrase or entailment; it catches
invented specifics, which is invariant 3's own named failure mode. And it does not license
giving this path a new source of material — research results once Exa lands, a second corpus,
memory across sessions — without reopening the question, because that is the point at which
paraphrase over grounded text stops being the whole of the risk. A model-backed entailment
check drops in behind `ConversationGroundingChecker` without touching a caller.

## Two invariants this directory exists to keep

**It never touches the news DB.** No database credential, no schema knowledge, no `motet-db`
dependency — `tests/test_no_database_access.py` fails the build if either appears. A session
arrives with its config complete and reaches Motet only through tools. That is what lets this
service be reused, by Zimmer among others, instead of being welded to Motet's data model.

**`spoken_through_ms` is ours.** `motet_voice.clock.PlaybackClock` owns playback position. A
provider's idea of where the listener is gets recorded as drift and ignored — measured, so the
disagreement is visible, and never acted on. Across an interruption the two clocks *always*
diverge: the provider stops generating at one offset while the client is still playing out a
buffer that ends at another, and only one of those is what the listener heard.

---

## Layout

| Module | What it is |
|---|---|
| `contract.py` | The wire protocol. No vendor is named anywhere in it |
| `app.py` | FastAPI + WebSocket, stateless, Cloud-Run-shaped |
| `session.py` | One live session: clock, detector, tools, arm |
| `clock.py` | `spoken_through_ms` |
| `tokens.py` | Signed stateless session tokens |
| `audio.py` | PCM, framing, WAV, resampling — stdlib only |
| `vad.py` | The VAD seam: energy, WebRTC (optional), scripted fake |
| `bargein.py` | The policy, the decision record, the turn-detector seam |
| `realtime/` | Both provider arms behind one interface |
| `tools/` | The four platform tools, over HTTP to Motet's API |
| `harness/` | Capture, replay, score, report — and a synthetic walk for CI |
| `cli.py` | `motet-voice demo \| ingest \| replay \| report \| upload` |

`motet-voice demo` runs the whole harness on synthetic outdoor audio, in about a second, with
no microphone. It is how you check the harness works before putting a coat on — and it is what
CI runs.

## Configuration

Every variable is optional; the service starts with none of them set.

| Variable | Meaning |
|---|---|
| `MOTET_VOICE_ARM` | `composed` (default) or `openai_realtime` |
| `MOTET_INFERENCE_MODE` | `fake` (default) or `real`. Parsed by `motet_inference.mode`, never here |
| `MOTET_VOICE_SESSION_SECRET` | HMAC key for session tokens. Unset mints an ephemeral one and warns |
| `MOTET_VOICE_SESSION_TTL_SECONDS` | Token lifetime, default 3600 |
| `MOTET_VOICE_API_BASE_URL` | Where the platform tools call Motet's API |
| `MOTET_VOICE_API_TOKEN` | Bearer token for that API |
| `MOTET_VOICE_START_SESSION_TOKEN` | Bearer required to mint a session. **Unset means open**, and an open `StartSession` is a confused deputy: a session's tools carry the credential above. `/internal/health` reports which it is |
| `MOTET_VOICE_LLM_MODEL` | Conversation model override; falls back to `MOTET_LLM_MODEL` |
| `MOTET_VOICE_OPENAI_REALTIME_MODEL` | Realtime model slug |
| `OPENAI_API_KEY` | Not provisioned. Wakes the realtime arm |
| `EXA_API_KEY` | Not provisioned. Wakes `start_research` |
