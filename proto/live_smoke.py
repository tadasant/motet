"""PROTOTYPE — drive the voice service's live (speech-to-speech) path with synthesized speech.

Not part of `bin/ci` and never will be: it reaches two vendors for real (Cartesia to
synthesize the question, and — through the voice service — the realtime provider). Run it
by hand against a voice service on :8100 started in realtime mode (proto/live-scope.md):

    UV_ENV_FILE=.env uv run python proto/live_smoke.py
    UV_ENV_FILE=.env uv run python proto/live_smoke.py "What was that number they just said?" 33000
    UV_ENV_FILE=.env uv run python proto/live_smoke.py "Who led it?" 15000 --client-barge-in

It synthesizes the question to 16 kHz PCM (cached beside this file as `.live_smoke_*.wav`),
opens a session with a timed transcript, starts narration at the given offset, streams a
second of quiet room and then the question as 200 ms mic packets followed by silence so
the provider's server VAD ends the turn, and prints what came back — **the interruption
comes from the service's own detector**, exactly as it does for a browser's open mic.
`--client-barge-in` sends an explicit `barge_in` frame first instead, which is the
push-to-talk shape; `--gated-mic` makes the quiet lead a browser mic with noise
suppression on (-65 dBFS, under the detector's absolute floor), which is the shape that
never fired before the noise floor learned to seed from quiet frames. Either way it
prints: `interrupted_at` with its position context, both transcripts, the `audio_chunk`
format, and the latency from the last mic frame to the first reply chunk. Prints no
secret.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import random
import struct
import sys
import time
import wave
from pathlib import Path

import httpx
from motet_inference.cartesia import CartesiaConfig
from websockets.asyncio.client import connect

BASE = "http://127.0.0.1:8100"
HERE = Path(__file__).parent

TRANSCRIPT = [
    {
        "title": "Acme raises a Series B",
        "start_ms": 0,
        "end_ms": 20_000,
        "claims": [
            {
                "start_ms": 0,
                "end_ms": 10_000,
                "spoken_text": "Acme raised a forty million dollar Series B on Tuesday.",
            },
            {
                "start_ms": 10_000,
                "end_ms": 20_000,
                "spoken_text": "The round was led by Example Ventures, with Northwind Capital "
                "participating.",
            },
        ],
    },
    {
        "title": "Helion's reactor timeline",
        "start_ms": 20_000,
        "end_ms": 50_000,
        "claims": [
            {
                "start_ms": 20_000,
                "end_ms": 30_000,
                "spoken_text": "Helion says its first reactor will deliver power by 2028.",
            },
            {
                "start_ms": 30_000,
                "end_ms": 40_000,
                "spoken_text": "The company has raised 425 million dollars to date.",
            },
            {
                "start_ms": 40_000,
                "end_ms": 50_000,
                "spoken_text": "Microsoft is signed as the first customer.",
            },
        ],
    },
    {
        "title": "A court ruling on voter rolls",
        "start_ms": 50_000,
        "end_ms": 70_000,
        "claims": [
            {
                "start_ms": 50_000,
                "end_ms": 70_000,
                "spoken_text": "The court found that 185 voter IDs had been flagged in error.",
            }
        ],
    },
]


def synthesize(text: str) -> bytes:
    """The question as 16 kHz mono int16 PCM, via Cartesia, cached on disk by content."""
    cached = HERE / f".live_smoke_{hashlib.sha1(text.encode()).hexdigest()[:10]}.wav"
    if cached.exists():
        with wave.open(str(cached), "rb") as handle:
            return handle.readframes(handle.getnframes())
    config = CartesiaConfig()
    config.validate()
    response = httpx.post(
        f"{config.base_url}/tts/bytes",
        json={
            "model_id": config.model,
            "transcript": text,
            "language": config.language,
            "voice": {"mode": "id", "id": config.voice_id},
            "output_format": {"container": "raw", "encoding": "pcm_s16le", "sample_rate": 16_000},
        },
        headers={"X-API-Key": config.api_key, "Cartesia-Version": config.version},
        timeout=60,
    )
    response.raise_for_status()
    with wave.open(str(cached), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16_000)
        handle.writeframes(response.content)
    return response.content


def ambient(size: int, *, level_dbfs: float = -50.0) -> bytes:
    """``size`` bytes of a quiet room: white noise at ``level_dbfs``, deterministic."""
    rng = random.Random(size)
    amplitude = int(32767 * 10 ** (level_dbfs / 20))
    samples = (rng.randint(-amplitude, amplitude) for _ in range(size // 2))
    return struct.pack(f"<{size // 2}h", *samples)


async def main(question: str, offset_ms: int, *, client_barge_in: bool, gated_mic: bool) -> None:
    pcm = synthesize(question)
    print(f"question: {question!r} → {len(pcm) / 32:.0f} ms of speech")
    config = {
        "persona": {
            "name": "Motet",
            "instructions": (
                "You are Motet, the voice of a news briefing the listener is hearing right "
                "now. They have just interrupted the narration to ask you something. Answer "
                "from the briefing material in one or two spoken sentences, then stop."
            ),
        },
        "context": {
            "episode_id": "ep_smoke",
            "spoken_through_ms": offset_ms,
            "notes": "\n".join(c["spoken_text"] for s in TRANSCRIPT for c in s["claims"]),
            "transcript": TRANSCRIPT,
        },
    }
    started = httpx.post(f"{BASE}/v1/voice/sessions", json=config)
    started.raise_for_status()
    session = started.json()
    print("start:", {k: session[k] for k in ("arm", "conversational")})

    async with connect("ws://127.0.0.1:8100" + session["websocket_path"], max_size=None) as ws:
        await ws.send(
            json.dumps(
                {"type": "authenticate", "token": session["session_token"], "config": config}
            )
        )
        ready = json.loads(await ws.recv())
        print("ready:", (ready.get("detail") or "")[:100])
        await ws.send(json.dumps({"type": "narration_delivered", "duration_ms": 70_000}))
        await ws.send(json.dumps({"type": "playback_position", "spoken_through_ms": offset_ms}))
        if client_barge_in:
            await ws.send(json.dumps({"type": "barge_in"}))

        chunk = 6_400  # 200 ms at 16 kHz
        # A second of quiet room first, so the detector's noise floor has settled and the
        # narration clock is visibly running when the speech arrives — the browser's mic
        # is open long before the listener says anything. Faint noise rather than digital
        # zero: a real microphone is never exactly silent, and the energy VAD refuses to
        # seed its floor from a frame that is.
        lead_level = -65.0 if gated_mic else -50.0
        lead = [] if client_barge_in else [ambient(chunk, level_dbfs=lead_level)] * 5
        packets = (
            lead + [pcm[i : i + chunk] for i in range(0, len(pcm), chunk)] + [bytes(chunk)] * 6
        )
        last_mic_at = 0.0

        async def feed() -> None:
            nonlocal last_mic_at
            for packet in packets:
                await ws.send(packet)
                last_mic_at = time.monotonic()
                await asyncio.sleep(0.2)

        feeder = asyncio.create_task(feed())
        first_audio_at: float | None = None
        chunks = 0
        audio_ms = 0
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            try:
                event = json.loads(await asyncio.wait_for(ws.recv(), 5))
            except TimeoutError:
                print("... waiting")
                continue
            kind = event["type"]
            if kind == "audio_chunk":
                if first_audio_at is None:
                    first_audio_at = time.monotonic()
                    print(
                        f"first audio_chunk: format={event.get('format')} "
                        f"rate={event['sample_rate']} "
                        f"(+{(first_audio_at - last_mic_at) * 1000:.0f} ms after last mic frame)"
                    )
                chunks += 1
                audio_ms += event["duration_ms"]
            elif kind == "interrupted_at":
                print(
                    "interrupted_at:",
                    event["offset_ms"],
                    f"trigger={event['decision'].get('trigger')}",
                    event.get("context"),
                )
            elif kind == "transcript":
                print(f"transcript[{event['speaker']}]: {event['text']}")
            elif kind == "session_state":
                print("state:", event["state"], "·", (event.get("detail") or "")[:60])
                if event["state"] == "ready" and event.get("detail") == "reply complete":
                    break
            else:
                print(kind, {k: v for k, v in event.items() if k != "type"})
        await feeder
        print(f"audio_chunks={chunks} total_reply_audio_ms={audio_ms}")
        await ws.send(json.dumps({"type": "close"}))
        print("close:", json.loads(await ws.recv())["state"])


if __name__ == "__main__":
    args = [arg for arg in sys.argv[1:] if not arg.startswith("--")]
    text = args[0] if args else "What did the company raise?"
    offset = int(args[1]) if len(args) > 1 else 15_000
    asyncio.run(
        main(
            text,
            offset,
            client_barge_in="--client-barge-in" in sys.argv,
            gated_mic="--gated-mic" in sys.argv,
        )
    )
