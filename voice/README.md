# Voice service — Phase 2

Motet's voice surface, and the **barge-in measurement harness** that settles the provider
question with a number instead of an argument.

```
POST /v1/voice/sessions                      StartSession(...) -> session_token
WS   /v1/voice/sessions/{id}/stream          audio in; transcripts, tool calls,
                                             audio chunks and interrupted_at out
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
| `mark_read`, `save_highlight` | **Live**, through Motet's MCP server — where a session binds the `motet` slug and `MOTET_VOICE_API_BASE_URL` is set. Dormant otherwise, with the reason |
| `get_item_detail`, `start_research` | **Gone** (motet#120). Neither named a tool any server has: one needed a single-news-item route that does not exist, the other needed Exa *and* a route nobody has designed |

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

## Nothing checks what this path says

Invariant 3 — every reported claim carries a source span — is now about the *structure* a
claim carries rather than a check anything performs. The advisory conversational check that
used to run behind every reply here, and the hard gate that used to sit in front of TTS on
the narration path, were both removed in
[#75](https://github.com/tadasant/motet/issues/75). The reasoning, and the risk it accepts,
are in that issue.

What is left is prompt-level containment, and it is not a guarantee: the model is given
context the caller assembled from an episode's own claims and their source spans, and told to
answer only from that or from a tool result, to decline rather than fill gaps, and to quote or
fetch numbers rather than recall them. A reply can still assert something no span supports,
and nothing will say so.

**That bounds how far this path may be widened without reopening the question.** Giving it a
new source of material — research results once Exa lands, a second corpus, memory across
sessions — changes the risk from paraphrase over sourced text to assertion from unsourced
text, with no instrument behind the reply at all.

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
| `tools/` | The platform tools, as MCP `tools/call`s on a bound server |
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
| `MOTET_VOICE_API_BASE_URL` | Where Motet's API is. The `motet` MCP slug resolves to `<this>/mcp`; unset means the slug resolves to nothing and every platform tool is dormant |
| `MOTET_VOICE_API_TOKEN` | Bearer for that API, and — unless the next variable is set — what the MCP connection presents |
| `MOTET_VOICE_MCP_TOKEN` | A credential for `/mcp` alone. Unset falls back to `MOTET_VOICE_API_TOKEN`, which is the whole-API owner token (motet#120, option **a**); setting this is the whole of option **b** |
| `MOTET_VOICE_MCP_TOOL_GROUPS` | The `?tool_groups=` selection, default `backlog,highlights` — the two groups holding the two tools a conversation calls. A deployment can narrow it, and an unknown group is a 400 from the server rather than a quietly smaller surface |
| `MOTET_VOICE_START_SESSION_TOKEN` | Bearer required to mint a session. **Unset means open**, and an open `StartSession` is a confused deputy: a session's tools carry the credential above. `/internal/health` reports which it is |
| `MOTET_LLM_MODEL_VOICE` | Conversation model for the composed arm; falls back to `MOTET_LLM_MODEL`, then the seam's default. The per-stage seam in `motet_inference.llm`, not a variable of this service's own — so the slug is checked against the catalogue at startup |
| `MOTET_LLM_EFFORT_VOICE` | Thinking depth for a spoken turn. Defaults to `off` |
| `MOTET_VOICE_OPENAI_REALTIME_MODEL` | Realtime model slug |
| `OPENAI_API_KEY` | Not provisioned. Wakes the realtime arm |

