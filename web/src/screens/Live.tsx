// PROTOTYPE: "Play Live" — listen to a ready episode through the voice service and
// interrupt it by voice. See proto/issues/04-play-live.md for what this is and is not.
//
// The shape follows the two-audio-paths split in AGENTS.md: narration is the rendered
// file the Episode screen's player already holds and plays *locally*; only the
// interaction is live. The voice service never streams the episode — this client tells
// it whether narration is playing and where (`narration_delivered`, `playback_position`),
// so `spoken_through_ms` stays ours (invariant 4), and it sends listener audio for the
// service's own VAD to decide a barge-in on. Nothing here names a vendor (invariant 1).
//
// Hand-typed contract, from voice/src/motet_voice/contract.py. Deliberately not generated:
// the voice service has no OpenAPI seam into this SPA yet, and that is one of the things
// the issue draft says the real implementation needs.

import { useCallback, useEffect, useRef, useState, type RefObject } from 'react'

import type { Episode } from '../api/client'

// ---- contract ---------------------------------------------------------------------------

type TimedClaim = { start_ms: number; end_ms: number; spoken_text: string }
type TimedSegment = {
  title: string
  start_ms: number
  end_ms: number
  news_item_id: string | null
  claims: TimedClaim[]
}

type StartSessionRequest = {
  persona: { name: string; instructions: string; voice?: string }
  tools?: { name: string; defaults?: Record<string, unknown> }[]
  context: {
    episode_id: string | null
    spoken_through_ms: number
    notes: string
    news_item_ids: string[]
    // The episode as a *timed* transcript, so the service can tell the model what was
    // playing when the listener interrupted — from its own clock (invariant 4), never a
    // lookup (invariant 2). `notes` stays as the whole-episode backdrop.
    transcript: TimedSegment[]
  }
  turn_policy?: {
    mode: 'open_mic' | 'push_to_talk'
    speech_probability_threshold?: number
    consecutive_speech_frames?: number
    min_snr_db?: number
    refractory_ms?: number
    require_narration_playing?: boolean
  }
}

type StartSessionResponse = {
  session_id: string
  session_token: string
  expires_at: string
  websocket_path: string
  arm: string
  conversational: boolean
}

type SessionEvent =
  // `reason` rides on the first `ready` of a session whose arm offers a live channel that did
  // not open: a short code (`insufficient_quota`, `arm_dormant`, …) so "no credits" and "no
  // key" are two different sentences. Absent or null everywhere else.
  | { type: 'session_state'; at_ms: number; state: 'ready' | 'listening' | 'speaking' | 'closed'; detail: string | null; reason?: string | null }
  | { type: 'transcript'; at_ms: number; speaker: 'user' | 'assistant'; text: string; final: boolean }
  | { type: 'audio_chunk'; at_ms: number; pcm_base64: string; sample_rate: number; duration_ms: number; format?: string }
  | { type: 'tool_call'; at_ms: number; call_id: string; name: string; arguments: Record<string, unknown> }
  | { type: 'tool_result'; at_ms: number; call_id: string; name: string; ok: boolean; result: Record<string, unknown>; error: string | null }
  | {
      type: 'interrupted_at'
      at_ms: number
      offset_ms: number
      decision: Record<string, unknown>
      context?: { clock?: string; segment_title?: string; claim_text?: string }
    }
  | { type: 'error'; at_ms: number; code: string; message: string }

// ---- where the voice service is ----------------------------------------------------------

// Not in config.js and not proxied by Vite: the service is started by hand on this port
// (proto/live-scope.md). Override with localStorage 'motet.voiceBaseUrl'.
const DEFAULT_VOICE_BASE = 'http://localhost:8100'
const LAUNCH_HINT = 'start it on :8100 — see proto/live-scope.md'

function voiceBaseUrl(): string {
  try {
    return localStorage.getItem('motet.voiceBaseUrl') || DEFAULT_VOICE_BASE
  } catch {
    return DEFAULT_VOICE_BASE
  }
}

function wsUrl(base: string, path: string): string {
  return base.replace(/^http/, 'ws') + path
}

// ---- audio plumbing ----------------------------------------------------------------------

/** What the service's VAD expects: 16 kHz mono int16, 20 ms frames (any packetisation). */
const TARGET_RATE = 16_000

function downsample(input: Float32Array, fromRate: number): Int16Array {
  if (fromRate === TARGET_RATE) return toInt16(input)
  const ratio = fromRate / TARGET_RATE
  const length = Math.floor(input.length / ratio)
  const out = new Int16Array(length)
  for (let i = 0; i < length; i += 1) {
    const pos = i * ratio
    const lo = Math.floor(pos)
    const hi = Math.min(lo + 1, input.length - 1)
    const frac = pos - lo
    const sample = (input[lo] ?? 0) * (1 - frac) + (input[hi] ?? 0) * frac
    out[i] = Math.max(-32768, Math.min(32767, Math.round(sample * 32767)))
  }
  return out
}

function toInt16(input: Float32Array): Int16Array {
  const out = new Int16Array(input.length)
  for (let i = 0; i < input.length; i += 1) {
    out[i] = Math.max(-32768, Math.min(32767, Math.round((input[i] ?? 0) * 32767)))
  }
  return out
}

function base64ToBytes(b64: string): Uint8Array {
  const bin = atob(b64)
  const bytes = new Uint8Array(bin.length)
  for (let i = 0; i < bin.length; i += 1) bytes[i] = bin.charCodeAt(i)
  return bytes
}

/**
 * `format` says what is in `pcm_base64` — `pcm16` is raw and everything else is a container
 * for the decoder (the composed arm sends Cartesia's MP3, the fake a WAV). The sniff stays
 * as the fallback for an older service that does not label the field.
 */
async function decodeReply(ctx: AudioContext, bytes: Uint8Array, sampleRate: number, format?: string): Promise<AudioBuffer> {
  const isId3 = bytes[0] === 0x49 && bytes[1] === 0x44 && bytes[2] === 0x33
  const isMpegSync = bytes[0] === 0xff && ((bytes[1] ?? 0) & 0xe0) === 0xe0
  const isRiff = bytes[0] === 0x52 && bytes[1] === 0x49 && bytes[2] === 0x46 && bytes[3] === 0x46
  const container = format ? format !== 'pcm16' : isId3 || isMpegSync || isRiff
  if (container) {
    const copy = bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength) as ArrayBuffer
    return ctx.decodeAudioData(copy)
  }
  const samples = new Int16Array(bytes.buffer, bytes.byteOffset, Math.floor(bytes.byteLength / 2))
  const buffer = ctx.createBuffer(1, samples.length, sampleRate)
  const channel = buffer.getChannelData(0)
  for (let i = 0; i < samples.length; i += 1) channel[i] = (samples[i] ?? 0) / 32768
  return buffer
}

// ---- the session config, assembled from what the screen already holds -------------------

/**
 * Claim timings, apportioned by text length inside each measured segment — the same rule
 * the TTS stage uses (`apportion_claim_timings`, AGENTS.md "Claim timings are apportioned").
 * The DB holds these numbers but the episode API's `ClaimModel` does not expose them yet, so
 * the prototype re-derives them here; exposing `start_ms`/`duration_ms` on the claim is the
 * one-line API change the real version wants, and this becomes a copy.
 */
function timedTranscript(episode: Episode): TimedSegment[] {
  return episode.segments.map((segment) => {
    const weights = segment.claims.map((claim) => Math.max(1, claim.text.length))
    const total = weights.reduce((sum, weight) => sum + weight, 0)
    let offset = 0
    const claims: TimedClaim[] = segment.claims.map((claim, index) => {
      const last = index === segment.claims.length - 1
      const duration = last
        ? Math.max(0, segment.duration_ms - offset)
        : Math.round((segment.duration_ms * (weights[index] ?? 1)) / total)
      const timed = { start_ms: segment.start_ms + offset, end_ms: segment.start_ms + offset + duration, spoken_text: claim.text }
      offset += duration
      return timed
    })
    return {
      title: segment.news_item_title,
      start_ms: segment.start_ms,
      end_ms: segment.start_ms + segment.duration_ms,
      news_item_id: segment.news_item_id,
      claims,
    }
  })
}

/**
 * Invariant 2 says the *caller* assembles the context because the voice service cannot
 * look anything up. In the prototype the caller is this browser, from the Episode it
 * already fetched; deployed, this belongs behind Motet's API (issue 04).
 */
function buildConfig(episode: Episode, spokenThroughMs: number): StartSessionRequest {
  const NOTES_CAP = 60_000
  let notes = `Episode: ${episode.title}\n\n`
  for (const segment of episode.segments) {
    const block =
      `## ${segment.news_item_title} (news_item_id ${segment.news_item_id}, starts at ${Math.round(segment.start_ms / 1000)}s)\n` +
      segment.claims.map((claim) => `- ${claim.text}`).join('\n') +
      '\n\n'
    if (notes.length + block.length > NOTES_CAP) break
    notes += block
  }
  return {
    persona: {
      name: 'Motet',
      instructions:
        'You are Motet, the voice of a news briefing the listener is hearing right now. ' +
        'They have just interrupted the narration to ask you something. Answer from the ' +
        'briefing material you have been given, in one or two spoken sentences, then stop ' +
        'so the narration can resume.',
      voice: 'narrator',
    },
    tools: [{ name: 'mark_read' }, { name: 'get_item_detail' }, { name: 'save_highlight' }],
    context: {
      episode_id: episode.id,
      spoken_through_ms: Math.max(0, Math.round(spokenThroughMs)),
      notes,
      news_item_ids: episode.segments.map((segment) => segment.news_item_id),
      transcript: timedTranscript(episode),
    },
    turn_policy: { mode: 'open_mic' },
  }
}

// ---- the component ------------------------------------------------------------------------

type Phase = 'idle' | 'unreachable' | 'connecting' | 'narrating' | 'listening' | 'replying' | 'resuming' | 'error'

type Line =
  | { kind: 'user' | 'assistant'; text: string }
  | { kind: 'tool'; text: string }
  | { kind: 'event'; text: string }

const PHASE_LABEL: Record<Phase, string> = {
  idle: 'idle',
  unreachable: 'voice service not reachable',
  connecting: 'connecting…',
  narrating: 'narrating — talk over it to interrupt',
  listening: 'listening… — narration paused, ask your question',
  replying: 'replying…',
  resuming: 'resuming narration',
  error: 'error',
}

export function Live({ episode, player }: { episode: Episode; player: RefObject<HTMLAudioElement | null> }) {
  const [phase, setPhase] = useState<Phase>('idle')
  const [arm, setArm] = useState<string>('')
  const [error, setError] = useState('')
  const [lines, setLines] = useState<Line[]>([])
  const [question, setQuestion] = useState('')
  const [lastInterrupt, setLastInterrupt] = useState<{
    offset_ms: number
    decision: Record<string, unknown>
    context?: { clock?: string; segment_title?: string; claim_text?: string } | undefined
  } | null>(null)
  // Peak of the last mic buffer (0..1) and its RMS in dBFS. The dBFS figure is the one that
  // matters: the service's detector seeds its noise floor from what the mic delivers
  // *between* utterances, and a browser mic with noise suppression on can sit under the
  // detector's absolute floor (-55 dBFS) while nobody is speaking. Showing the number is
  // how "the meter moves when I talk but nothing interrupts" becomes diagnosable.
  const [micLevel, setMicLevel] = useState(0)
  const [micDbfs, setMicDbfs] = useState(-100)
  // Whether replies arrive as streamed speech (the live channel opened) or as a whole
  // container from the fallback arm. **Nothing about the microphone depends on it**: the
  // mic is captured and streamed for as long as the session is open, on every arm, because
  // the interruption is decided by the service's own detector and not by the vendor
  // (voice/session.py, `receive_audio`). `live` only changes how a reply is played and
  // whether the user transcript line is the service's or our own echo.
  const [live, setLive] = useState(false)
  // Why the live channel is not there, when the arm offered one: the server's `reason` code
  // plus its prose. Rendered as one line so the owner can tell "no credits" from "no key".
  const [liveUnavailable, setLiveUnavailable] = useState<{ reason: string; detail: string } | null>(null)
  // The last turn's failure, shown inline under the question box — a vendor refusing one
  // reply is not a closed socket, and the box stays usable for the next question.
  const [turnError, setTurnError] = useState('')

  const ws = useRef<WebSocket | null>(null)
  const ctx = useRef<AudioContext | null>(null)
  const mic = useRef<MediaStream | null>(null)
  const processor = useRef<ScriptProcessorNode | null>(null)
  const pausedByUs = useRef(false)
  const lastPositionSent = useRef(0)
  // Streamed reply audio: pcm16 chunks are scheduled back to back on the AudioContext
  // clock as they arrive, so the first word plays before the sentence is finished.
  const nextChunkAt = useRef(0)
  const playing = useRef<AudioBufferSourceNode[]>([])
  const readySeen = useRef(false)
  // `handleEvent` is bound to the socket once, in the render `start` ran in, so state it
  // reads is frozen there; the ref is what it consults, the state is what renders.
  const liveRef = useRef(false)
  const setLiveMode = (next: boolean) => {
    liveRef.current = next
    setLive(next)
  }
  const questionBox = useRef<HTMLInputElement>(null)
  const phaseRef = useRef<Phase>('idle')
  const setPhaseBoth = (next: Phase) => {
    phaseRef.current = next
    setPhase(next)
  }
  const push = (line: Line) => setLines((prev) => [...prev, line])

  // Reachability probe, so "nothing happens" and "the service is not running" are two
  // different sentences — the never-infer-"no errors"-from-"no data" rule in miniature.
  useEffect(() => {
    let cancelled = false
    fetch(`${voiceBaseUrl()}/internal/health`)
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(String(r.status)))))
      .then((h: { arm: string; inference_mode: string }) => {
        if (cancelled) return
        setArm(`${h.arm} · ${h.inference_mode}`)
      })
      .catch(() => {
        if (!cancelled) setPhaseBoth('unreachable')
      })
    return () => {
      cancelled = true
    }
  }, [])

  const send = (frame: Record<string, unknown>) => {
    const socket = ws.current
    if (socket && socket.readyState === WebSocket.OPEN) socket.send(JSON.stringify(frame))
  }

  const stopMic = () => {
    processor.current?.disconnect()
    processor.current = null
    mic.current?.getTracks().forEach((track) => track.stop())
    mic.current = null
  }

  const teardown = useCallback(() => {
    stopMic()
    flushReplyQueue()
    const socket = ws.current
    ws.current = null
    if (socket && socket.readyState === WebSocket.OPEN) socket.close()
    void ctx.current?.close()
    ctx.current = null
    setPhaseBoth('idle')
  }, [])

  useEffect(() => teardown, [teardown])

  const resumeNarration = () => {
    const el = player.current
    if (!el) return
    setPhaseBoth('resuming')
    // `narration_delivered` is the only frame that starts the service's clock; 0 ms delivered
    // because the whole file was declared at play time (issue 04, "wire nits").
    send({ type: 'narration_delivered', duration_ms: 0 })
    send({ type: 'playback_position', spoken_through_ms: Math.round(el.currentTime * 1000) })
    pausedByUs.current = false
    void el.play().then(() => setPhaseBoth('narrating'))
  }

  const flushReplyQueue = () => {
    for (const source of playing.current) {
      try {
        source.stop()
      } catch {
        // already ended
      }
    }
    playing.current = []
    nextChunkAt.current = 0
  }

  const playReply = async (event: Extract<SessionEvent, { type: 'audio_chunk' }>) => {
    const audio = ctx.current
    if (!audio) return
    try {
      const buffer = await decodeReply(audio, base64ToBytes(event.pcm_base64), event.sample_rate, event.format)
      const source = audio.createBufferSource()
      source.buffer = buffer
      source.connect(audio.destination)
      if (event.format === 'pcm16') {
        // One chunk of a streamed reply: queue it on the audio clock behind the last one.
        // The service says when the reply is over (`session_state: ready`); a chunk's end
        // means nothing on its own.
        const startAt = Math.max(audio.currentTime + 0.02, nextChunkAt.current)
        source.start(startAt)
        nextChunkAt.current = startAt + buffer.duration
        playing.current.push(source)
        source.onended = () => {
          playing.current = playing.current.filter((node) => node !== source)
        }
      } else {
        // A whole reply in one container (the composed arm): play it, then resume.
        source.onended = () => resumeNarration()
        source.start()
      }
    } catch (err) {
      push({ kind: 'event', text: `could not decode reply audio: ${String(err)}` })
      resumeNarration()
    }
  }

  const handleEvent = (event: SessionEvent) => {
    switch (event.type) {
      case 'session_state':
        if (event.state === 'ready') {
          if (!readySeen.current) {
            // The first `ready` opens the session; narration starts.
            readySeen.current = true
            const opened = Boolean(event.detail?.startsWith('live conversation open'))
            setLiveMode(opened)
            // A live arm whose channel did not open says why in `reason`; a plain composed
            // arm sends neither the prefix nor a reason and this stays null.
            if (!opened && event.reason) setLiveUnavailable({ reason: event.reason, detail: event.detail ?? '' })
            if (event.detail) push({ kind: 'event', text: `ready · ${event.detail}` })
            beginNarration()
          } else {
            // Every later `ready` is a reply that has finished streaming: let the queued
            // audio drain, then narration picks up where it stopped.
            const audio = ctx.current
            const remaining = audio ? Math.max(0, nextChunkAt.current - audio.currentTime) : 0
            setTimeout(() => resumeNarration(), Math.round(remaining * 1000) + 150)
          }
        } else if (event.state === 'listening') {
          // Either the service just engaged the live channel, or the listener talked over a
          // reply and the service cut it off — drop whatever is queued and listen.
          flushReplyQueue()
          setPhaseBoth('listening')
          if (event.detail && event.detail !== 'live — speak your question') push({ kind: 'event', text: event.detail })
        } else if (event.state === 'speaking') {
          setPhaseBoth('replying')
        } else if (event.state === 'closed') {
          teardown()
        }
        return
      case 'interrupted_at': {
        const el = player.current
        pausedByUs.current = true
        el?.pause()
        setLastInterrupt({ offset_ms: event.offset_ms, decision: event.decision, context: event.context })
        setPhaseBoth('listening')
        const where = event.context?.segment_title
          ? ` during “${event.context.segment_title}”${event.context.claim_text ? ` — “${event.context.claim_text}”` : ''}`
          : ''
        push({ kind: 'event', text: `interrupted at ${(event.offset_ms / 1000).toFixed(1)}s${where}` })
        setTimeout(() => questionBox.current?.focus(), 0)
        return
      }
      case 'transcript':
        // On the live channel the user line is what the service *heard*; on the typed path it
        // is our own text echoed back, which `ask` has already shown.
        if (event.speaker === 'assistant') push({ kind: 'assistant', text: event.text })
        else if (liveRef.current) push({ kind: 'user', text: event.text })
        return
      case 'audio_chunk':
        void playReply(event)
        return
      case 'tool_call':
        push({ kind: 'tool', text: `→ ${event.name}(${JSON.stringify(event.arguments)})` })
        return
      case 'tool_result':
        push({ kind: 'tool', text: `← ${event.name}: ${event.ok ? 'ok' : `failed — ${event.error ?? ''}`}` })
        return
      case 'error':
        if (event.code === 'turn_failed' || event.code === 'arm_dormant') {
          // One reply failed. Not a closed socket: back to listening, the error under the
          // question box, and the box still takes the next question.
          setTurnError(`${event.code}: ${event.message}`)
          flushReplyQueue()
          setPhaseBoth('listening')
          setTimeout(() => questionBox.current?.focus(), 0)
          return
        }
        if (event.code === 'live_unavailable') {
          // The live channel died mid-session. Typed questions go to the text arm from here;
          // a user line is now our own echo, so stop rendering it twice. The mic and the
          // narration clock are untouched: barge-in is the service's local detector and
          // keeps working without any vendor.
          setLiveMode(false)
          setLiveUnavailable({ reason: event.code, detail: event.message })
        }
        push({ kind: 'event', text: `error ${event.code}: ${event.message}` })
        return
      default:
        return
    }
  }

  const beginNarration = () => {
    const el = player.current
    if (!el) return
    // The client holds the whole rendered file, so the delivered ceiling is the episode.
    send({ type: 'narration_delivered', duration_ms: episode.duration_ms })
    send({ type: 'playback_position', spoken_through_ms: Math.round(el.currentTime * 1000) })
    pausedByUs.current = false
    void el.play().then(() => setPhaseBoth('narrating'))
  }

  // Report the player's position while live, and treat a manual pause as the listener
  // taking the floor — there is no "narration paused" frame on the contract (issue 04).
  useEffect(() => {
    const el = player.current
    if (!el || phase === 'idle' || phase === 'unreachable' || phase === 'error') return
    const onTime = () => {
      const now = el.currentTime * 1000
      if (Math.abs(now - lastPositionSent.current) >= 1000) {
        lastPositionSent.current = now
        send({ type: 'playback_position', spoken_through_ms: Math.round(now) })
      }
    }
    const onPause = () => {
      if (pausedByUs.current || el.ended || phaseRef.current !== 'narrating') return
      send({ type: 'barge_in' })
    }
    el.addEventListener('timeupdate', onTime)
    el.addEventListener('pause', onPause)
    return () => {
      el.removeEventListener('timeupdate', onTime)
      el.removeEventListener('pause', onPause)
    }
  }, [phase, player])

  // Runs from `start` until the socket closes — never gated on `live`, never stopped by a
  // `live_unavailable` error. Barge-in is the service's local detector reading these
  // frames, so stopping them for any reason short of ending the session ends barge-in.
  const startMic = async (audio: AudioContext) => {
    const stream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 },
    })
    mic.current = stream
    const source = audio.createMediaStreamSource(stream)
    // ScriptProcessorNode is deprecated but is the smallest thing that hands us PCM; an
    // AudioWorklet is the real answer and not worth the extra file for a prototype.
    const node = audio.createScriptProcessor(4096, 1, 1)
    let frames = 0
    node.onaudioprocess = (e) => {
      const input = e.inputBuffer.getChannelData(0)
      const pcm = downsample(input, audio.sampleRate)
      const socket = ws.current
      if (socket && socket.readyState === WebSocket.OPEN) socket.send(pcm.buffer)
      frames += 1
      if (frames % 4 === 0) {
        let peak = 0
        let energy = 0
        for (let i = 0; i < input.length; i += 1) {
          const sample = input[i] ?? 0
          peak = Math.max(peak, Math.abs(sample))
          energy += sample * sample
        }
        setMicLevel(peak)
        const rms = Math.sqrt(energy / Math.max(1, input.length))
        setMicDbfs(rms > 0 ? Math.max(-100, 20 * Math.log10(rms)) : -100)
      }
    }
    source.connect(node)
    node.connect(audio.destination)
    processor.current = node
  }

  const start = async () => {
    const el = player.current
    if (!el) return
    setError('')
    setTurnError('')
    setLiveUnavailable(null)
    setLines([])
    setLastInterrupt(null)
    readySeen.current = false
    flushReplyQueue()
    setPhaseBoth('connecting')
    const base = voiceBaseUrl()
    try {
      const config = buildConfig(episode, el.currentTime * 1000)
      const response = await fetch(`${base}/v1/voice/sessions`, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify(config),
      })
      if (!response.ok) throw new Error(`StartSession → ${response.status} ${await response.text()}`)
      const started = (await response.json()) as StartSessionResponse
      setArm(`${started.arm}${started.conversational ? '' : ' (text turns only)'}`)

      let audio: AudioContext
      try {
        audio = new AudioContext({ sampleRate: TARGET_RATE })
      } catch {
        audio = new AudioContext()
      }
      ctx.current = audio
      await startMic(audio)

      const socket = new WebSocket(wsUrl(base, started.websocket_path))
      ws.current = socket
      socket.onopen = () => socket.send(JSON.stringify({ type: 'authenticate', token: started.session_token, config }))
      socket.onmessage = (message) => {
        try {
          handleEvent(JSON.parse(String(message.data)) as SessionEvent)
        } catch (err) {
          push({ kind: 'event', text: `unreadable event: ${String(err)}` })
        }
      }
      socket.onerror = () => {
        setError('the voice socket failed')
        setPhaseBoth('error')
      }
      socket.onclose = (closed) => {
        if (ws.current === socket) {
          ws.current = null
          stopMic()
          if (phaseRef.current !== 'idle' && phaseRef.current !== 'error') {
            push({ kind: 'event', text: `socket closed (${closed.code})` })
            setPhaseBoth('idle')
          }
        }
      }
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err)
      setError(/Failed to fetch|NetworkError/.test(message) ? `voice service not reachable at ${base} — ${LAUNCH_HINT}` : message)
      teardown()
      setPhaseBoth(/Failed to fetch|NetworkError/.test(message) ? 'unreachable' : 'error')
    }
  }

  const stop = () => {
    send({ type: 'close' })
    pausedByUs.current = true
    player.current?.pause()
    teardown()
  }

  const ask = (e: React.FormEvent) => {
    e.preventDefault()
    const text = question.trim()
    if (!text) return
    setQuestion('')
    setTurnError('')
    if (!liveRef.current) push({ kind: 'user', text })
    setPhaseBoth('replying')
    send({ type: 'text', text })
  }

  const running = phase !== 'idle' && phase !== 'unreachable' && phase !== 'error'

  return (
    <div className="live">
      <div className="row">
        {running ? (
          <button type="button" onClick={stop}>
            Stop Live
          </button>
        ) : (
          <button type="button" onClick={start} disabled={phase === 'unreachable'}>
            Play Live
          </button>
        )}
        <span className={phase === 'unreachable' || phase === 'error' ? 'error' : 'hint'} role="status" data-live-phase={phase}>
          {PHASE_LABEL[phase]}
          {phase === 'unreachable' && ` (${voiceBaseUrl()} — ${LAUNCH_HINT})`}
          {arm && ` · ${arm}`}
        </span>
        {running && (
          <span className="hint" aria-label="mic level" title="RMS of the last mic buffer; the service's detector ignores frames under -55 dBFS as silence and seeds its noise floor from the rest">
            mic {'▮'.repeat(Math.min(8, Math.round(micLevel * 16)))} {micDbfs.toFixed(0)} dBFS
          </span>
        )}
      </div>
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
      {running && liveUnavailable && (
        <p className="hint" data-live-unavailable={liveUnavailable.reason}>
          live conversation unavailable — <code>{liveUnavailable.reason}</code>; typed questions are answered by the
          fallback arm
        </p>
      )}
      {phase === 'listening' && (
        <form onSubmit={ask} className="row">
          <input
            ref={questionBox}
            value={question}
            onChange={(e) => setQuestion(e.target.value)}
            placeholder={live ? 'or type your question' : 'Your question (typed — this arm has no speech-to-text)'}
            aria-label="question"
            style={{ flex: 1 }}
          />
          <button type="submit">Ask</button>
          <button type="button" className="linkish" onClick={resumeNarration}>
            never mind, resume
          </button>
        </form>
      )}
      {running && turnError && (
        <p className="error" role="alert" data-live-turn-error>
          that reply failed — {turnError}
        </p>
      )}
      {lastInterrupt?.context?.segment_title && (
        <p className="hint" data-live-interrupted-during>
          interrupted during: <strong>{lastInterrupt.context.segment_title}</strong>
          {lastInterrupt.context.claim_text && <> — “{lastInterrupt.context.claim_text}”</>}
        </p>
      )}
      {lastInterrupt && (
        <p className="hint">
          last barge-in at {(lastInterrupt.offset_ms / 1000).toFixed(1)}s ·{' '}
          {Object.entries(lastInterrupt.decision)
            .filter(([key]) => ['trigger', 'snr_db', 'speech_probability', 'rms_dbfs', 'noise_floor_dbfs'].includes(key))
            .map(([key, value]) => `${key}=${typeof value === 'number' ? value.toFixed(2) : String(value)}`)
            .join(' ')}
        </p>
      )}
      {lines.length > 0 && (
        <ul className="live-log">
          {lines.map((line, index) => {
            switch (line.kind) {
              case 'user':
                return (
                  <li key={index} data-live-user-line>
                    You{live ? ' (heard)' : ''}: {line.text}
                  </li>
                )
              case 'assistant':
                return (
                  <li key={index}>
                    <strong>Motet:</strong> {line.text}
                  </li>
                )
              case 'tool':
                return (
                  <li key={index} className="hint">
                    {line.text}
                  </li>
                )
              default:
                return (
                  <li key={index} className="hint">
                    {line.text}
                  </li>
                )
            }
          })}
        </ul>
      )}
    </div>
  )
}
