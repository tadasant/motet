// Play Live — listen to a rendered episode and interrupt it by voice (motet#93).
//
// The shape follows the two-audio-paths split in AGENTS.md: narration is the rendered file
// the episode screen's player already holds and plays *locally*; only the interaction is
// live. The voice service never streams the episode — this client tells it whether
// narration is playing and where (`narration_delivered`, `narration_paused`,
// `narration_resumed`, `playback_position`), so `spoken_through_ms` stays ours
// (invariant 4), and it sends listener audio for the service's own detector to decide a
// barge-in on. Nothing here names a vendor (invariant 1).
//
// **The session is minted by Motet's API, not by this page.** The API assembles the
// episode's context from the database and calls the voice service with a start token this
// page never sees (invariant 2); it hands back a socket URL and the frame to open it with.
// And it is asked first whether there is a voice service at all: none is deployed in
// staging or production yet, and there the button is disabled with the reason beside it
// rather than reaching for a host that does not exist.
//
// The socket's event types are hand-typed from voice/src/motet_voice/contract.py: the voice
// service has no OpenAPI seam into this SPA.

import { useCallback, useEffect, useRef, useState, type ReactNode, type RefObject } from 'react'
import { createPortal } from 'react-dom'

import { api, type Episode, type VoiceSession } from '../api/client'
import { MicGlyph } from '../brand/Brand'

// ---- contract ---------------------------------------------------------------------------

type SessionEvent =
  // `reason` rides on the first `ready` of a session whose arm offers a live channel that did
  // not open: a short code (`insufficient_quota`, `arm_dormant`, …) so "no credits" and "no
  // key" are two different sentences. Absent or null everywhere else.
  // `live` says whether a speech-to-speech channel is open behind this state (on the first
  // `ready`, and on the `listening` a reopened channel engages with); absent means it says nothing.
  | { type: 'session_state'; at_ms: number; state: 'ready' | 'listening' | 'speaking' | 'closed'; detail: string | null; reason?: string | null; live?: boolean | null }
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

// ---- audio plumbing ----------------------------------------------------------------------

/** What the service's detector expects: 16 kHz mono int16, 20 ms frames (any packetisation). */
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

/**
 * RMS in dBFS, floored at -100 like the service's own `dbfs()`, so the number beside the mic
 * meter is the one the detector is comparing against its noise floor. A mic that shows
 * -65 between words and -25 while talking is a mic the detector can hear; one that shows
 * -25 throughout is a room it cannot tell a voice from.
 */
export function rmsDbfs(samples: Float32Array): number {
  if (samples.length === 0) return -100
  let total = 0
  for (let i = 0; i < samples.length; i += 1) total += (samples[i] ?? 0) ** 2
  const rms = Math.sqrt(total / samples.length)
  return rms > 0 ? Math.max(-100, 20 * Math.log10(rms)) : -100
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

// ---- the component ------------------------------------------------------------------------

type Phase = 'idle' | 'connecting' | 'narrating' | 'paused' | 'listening' | 'replying' | 'resuming' | 'error'

/** Whether this deployment can run Play Live at all, as the API answered. */
type Availability = { state: 'checking' } | { state: 'available' } | { state: 'unavailable'; reason: string }

type Line =
  | { kind: 'user' | 'assistant'; text: string }
  | { kind: 'tool'; text: string }
  | { kind: 'event'; text: string }

const PHASE_LABEL: Record<Phase, string> = {
  idle: 'idle',
  connecting: 'connecting…',
  narrating: 'narrating — talk over it to interrupt',
  paused: 'paused — press play to carry on',
  listening: 'listening… — narration paused, ask your question',
  replying: 'replying…',
  resuming: 'resuming narration',
  error: 'error',
}

/** The session's AudioContext, at the rate the service speaks where the browser allows it. */
function openAudioContext(): AudioContext {
  try {
    return new AudioContext({ sampleRate: TARGET_RATE })
  } catch {
    return new AudioContext()
  }
}

/**
 * Let a later, gesture-less `play()` through on browsers that gate playback per element.
 *
 * WebKit lifts an element's user-gesture restriction the first time `play()` is called inside
 * a tap, whether or not that play goes on to produce sound — so playing and pausing in the
 * same tick unlocks the element without a word of narration escaping. Only while paused: a
 * listener already playing has unlocked it, and pausing them would be a bug of our own.
 */
export function primePlayback(el: HTMLAudioElement) {
  if (!el.paused) return
  el.play()?.catch(() => undefined)
  el.pause()
}

export function Live({
  episode,
  player,
  micSlot = null,
}: {
  episode: Episode
  player: RefObject<HTMLAudioElement | null>
  /**
   * The player transport's pill slot. Given one, Play Live's primary control is drawn there
   * as the brand's mic pill (motet#110) instead of as a button in its own row. The
   * interaction is the one this component always had — a press starts Play Live, a press
   * while narrating interrupts — and nothing about it is hold-to-talk.
   */
  micSlot?: HTMLElement | null
}) {
  const [availability, setAvailability] = useState<Availability>({ state: 'checking' })
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
  const [micLevel, setMicLevel] = useState(0)
  const [micDbfs, setMicDbfs] = useState(-100)
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
  // Which Play Live this is. Stop, unmount and a new start all move it on, so a start still
  // awaiting the mint or the mic finds out it was abandoned instead of opening a socket and
  // starting the player after the listener left.
  const generation = useRef(0)
  // The drain-then-resume timer after a streamed reply, cleared by teardown and by a manual play.
  const resumeTimer = useRef<number | null>(null)
  const clearResumeTimer = () => {
    if (resumeTimer.current !== null) window.clearTimeout(resumeTimer.current)
    resumeTimer.current = null
  }
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

  // Asked of *our* API, which exists in every environment — never of the voice service,
  // which does not exist in most of them. "Not configured" is an answer, not a failure.
  useEffect(() => {
    let cancelled = false
    api
      .voiceStatus()
      .then((status) => {
        if (cancelled) return
        setAvailability(
          status.configured
            ? { state: 'available' }
            : { state: 'unavailable', reason: status.reason ?? "Live voice isn't configured in this environment." },
        )
      })
      .catch(() => {
        if (!cancelled) setAvailability({ state: 'unavailable', reason: 'Could not ask the API whether live voice is available.' })
      })
    return () => {
      cancelled = true
    }
  }, [])

  const send = (frame: Record<string, unknown>) => {
    const socket = ws.current
    if (socket && socket.readyState === WebSocket.OPEN) socket.send(JSON.stringify(frame))
  }

  const position = () => Math.round((player.current?.currentTime ?? 0) * 1000)

  const stopMic = () => {
    processor.current?.disconnect()
    processor.current = null
    mic.current?.getTracks().forEach((track) => track.stop())
    mic.current = null
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

  const teardown = useCallback(() => {
    generation.current += 1
    clearResumeTimer()
    stopMic()
    flushReplyQueue()
    const socket = ws.current
    ws.current = null
    // Whatever its state: a socket still CONNECTING would otherwise open, authenticate and
    // start narration for a session the listener already stopped.
    if (socket && socket.readyState !== WebSocket.CLOSED && socket.readyState !== WebSocket.CLOSING) socket.close()
    void ctx.current?.close()
    ctx.current = null
    setPhaseBoth('idle')
  }, [])

  useEffect(() => teardown, [teardown])

  const resumeNarration = () => {
    const el = player.current
    if (!el || !ws.current) return
    clearResumeTimer()
    setPhaseBoth('resuming')
    // Resume from the interruption offset, deliberately not rewound: whether a couple of
    // seconds of rewind makes the cut sentence easier to follow is an open question for
    // the owner (motet#93), and this is the one line that would change.
    send({ type: 'narration_resumed', spoken_through_ms: position() })
    pausedByUs.current = false
    startPlayer(el)
  }

  /**
   * Play the narration from a socket event, which is not a tap. The start primed the element
   * so a browser that insists on a gesture (iOS Safari) allows this; where it still refuses,
   * the session is told narration is paused — the frame a listener's own pause sends — and
   * the player's play button, a real tap, resumes it through the ordinary `narration_resumed`
   * path. Unhandled, the refusal left the pill on "connecting" with nothing playing.
   */
  const startPlayer = (el: HTMLAudioElement) => {
    const mine = generation.current
    const played = el.play()
    if (!played) {
      setPhaseBoth('narrating')
      return
    }
    played.then(
      () => setPhaseBoth('narrating'),
      () => {
        // A refusal that lands after a stop, or after a newer Play Live, is not this session's.
        if (!ws.current || generation.current !== mine) return
        send({ type: 'narration_paused', spoken_through_ms: position() })
        setPhaseBoth('paused')
        push({ kind: 'event', text: 'the browser would not start the narration — press play' })
      },
    )
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

  const beginNarration = () => {
    const el = player.current
    if (!el) return
    // The client holds the whole rendered file, so the delivered ceiling is the episode.
    send({ type: 'narration_delivered', duration_ms: episode.duration_ms })
    send({ type: 'playback_position', spoken_through_ms: position() })
    pausedByUs.current = false
    startPlayer(el)
  }

  const handleEvent = (event: SessionEvent) => {
    switch (event.type) {
      case 'session_state':
        if (event.state === 'ready') {
          if (!readySeen.current) {
            // The first `ready` opens the session; narration starts.
            readySeen.current = true
            const opened = event.live ?? Boolean(event.detail?.startsWith('live conversation open'))
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
            clearResumeTimer()
            resumeTimer.current = window.setTimeout(() => resumeNarration(), Math.round(remaining * 1000) + 150)
          }
        } else if (event.state === 'listening') {
          // Either the service just engaged the live channel, or the listener talked over a
          // reply and the service cut it off — drop whatever is queued and listen.
          flushReplyQueue()
          // A channel reopened mid-session says so here; the heard transcript shows again.
          if (typeof event.live === 'boolean') setLiveMode(event.live)
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
          // a user line is now our own echo, so stop rendering it twice.
          setLiveMode(false)
          setLiveUnavailable({ reason: event.code, detail: event.message })
        }
        if (phaseRef.current === 'replying') {
          // No `ready` is coming for a reply that errored. Back to listening, where the
          // question box and "never mind, resume" are — not stuck with only Stop Live.
          flushReplyQueue()
          setPhaseBoth('listening')
        }
        push({ kind: 'event', text: `error ${event.code}: ${event.message}` })
        return
      default:
        return
    }
  }

  // Report the player's position while live. A pause the listener made is `narration_paused`
  // — the clock stops and nothing is engaged, so nothing is billed — and pressing play again
  // is `narration_resumed`. Neither is a barge-in; "Interrupt" is.
  useEffect(() => {
    const el = player.current
    if (!el || phase === 'idle' || phase === 'error' || phase === 'connecting') return
    const onTime = () => {
      const now = el.currentTime * 1000
      if (Math.abs(now - lastPositionSent.current) >= 1000) {
        lastPositionSent.current = now
        send({ type: 'playback_position', spoken_through_ms: Math.round(now) })
      }
    }
    const onPause = () => {
      if (pausedByUs.current || el.ended || phaseRef.current !== 'narrating') return
      send({ type: 'narration_paused', spoken_through_ms: position() })
      setPhaseBoth('paused')
    }
    const onPlay = () => {
      const current = phaseRef.current
      if (current === 'listening' || current === 'replying') {
        // The listener pressed play on the player itself mid-exchange. That is a resume,
        // and the service has to hear it: otherwise its clock stays frozen and the mic keeps
        // going to the voice provider while the briefing plays.
        clearResumeTimer()
        flushReplyQueue()
        pausedByUs.current = false
      } else if (current !== 'paused') {
        return
      }
      send({ type: 'narration_resumed', spoken_through_ms: position() })
      setPhaseBoth('narrating')
    }
    el.addEventListener('timeupdate', onTime)
    el.addEventListener('pause', onPause)
    el.addEventListener('play', onPlay)
    return () => {
      el.removeEventListener('timeupdate', onTime)
      el.removeEventListener('pause', onPause)
      el.removeEventListener('play', onPlay)
    }
  }, [phase, player])

  /** Open the mic and wire it to the socket. Returns the handles rather than storing them, so
   *  a start that was abandoned while permission was pending never overwrites a newer one's. */
  const startMic = async (audio: AudioContext): Promise<{ stream: MediaStream; node: ScriptProcessorNode }> => {
    const stream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 },
    })
    const source = audio.createMediaStreamSource(stream)
    // ScriptProcessorNode is deprecated but is the smallest thing that hands us PCM; an
    // AudioWorklet is the real answer and a file of its own.
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
        for (let i = 0; i < input.length; i += 1) peak = Math.max(peak, Math.abs(input[i] ?? 0))
        setMicLevel(peak)
        setMicDbfs(rmsDbfs(input))
      }
    }
    source.connect(node)
    node.connect(audio.destination)
    return { stream, node }
  }

  const openSocket = (session: VoiceSession) => {
    const socket = new WebSocket(session.websocket_url)
    ws.current = socket
    socket.onopen = () => socket.send(JSON.stringify(session.authenticate_frame))
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
  }

  const start = async () => {
    const el = player.current
    if (!el) return
    // Everything a browser only allows inside a tap happens here, before the first await.
    // iOS Safari starts an AudioContext made outside a gesture suspended — no reply audio,
    // and a suspended context never runs the processor that feeds the mic to the socket —
    // and refuses `play()` on the element when the session's `ready` arrives over the socket
    // seconds later. Left until after the mint's round trip, both fail silently on a phone.
    primePlayback(el)
    let audio: AudioContext | null = null
    setError('')
    setTurnError('')
    setLiveUnavailable(null)
    setLines([])
    setLastInterrupt(null)
    readySeen.current = false
    flushReplyQueue()
    generation.current += 1
    const mine = generation.current
    const abandoned = () => generation.current !== mine
    setPhaseBoth('connecting')
    try {
      audio = openAudioContext()
      void audio.resume().catch(() => undefined)
      const session = await api.startVoiceSession(episode.id, position())
      if (abandoned()) {
        void audio.close().catch(() => undefined)
        return
      }
      setArm(`${session.arm}${session.conversational ? '' : ' (text turns only)'}`)
      const opened = await startMic(audio)
      if (abandoned()) {
        // Stopped or unmounted while the mic permission was pending: release what we took.
        opened.node.disconnect()
        opened.stream.getTracks().forEach((track) => track.stop())
        void audio.close().catch(() => undefined)
        return
      }
      ctx.current = audio
      mic.current = opened.stream
      processor.current = opened.node
      openSocket(session)
    } catch (err) {
      // Made before the first await, so every exit that did not hand it to the session
      // releases it; one that did is closed by the teardown below.
      if (audio && ctx.current !== audio) void audio.close().catch(() => undefined)
      if (abandoned()) return
      setError(err instanceof Error ? err.message : String(err))
      teardown()
      setPhaseBoth('error')
    }
  }

  const stop = () => {
    send({ type: 'close' })
    pausedByUs.current = true
    player.current?.pause()
    teardown()
  }

  const interrupt = () => {
    // The explicit floor-taking: for a listener who would rather press a button than talk
    // over the briefing. The service counts it as a barge-in like any other.
    send({ type: 'barge_in' })
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

  const running = phase !== 'idle' && phase !== 'error'

  /** The mic pill in the transport, or null when there is no transport to put it in. */
  const micPill = (content: ReactNode, options: { onClick?: () => void; disabled?: boolean; label?: string }) =>
    micSlot &&
    createPortal(
      <button
        type="button"
        className="mic"
        onClick={options.onClick}
        disabled={options.disabled}
        aria-label={options.label}
      >
        <MicGlyph />
        {content}
      </button>,
      micSlot,
    )

  if (availability.state !== 'available') {
    return (
      <div className="live" data-live-availability={availability.state}>
        {micPill('Play Live', { disabled: true })}
        <div className="row">
          {!micSlot && (
            <button type="button" disabled>
              Play Live
            </button>
          )}
          <span className="hint" role="status" data-live-unavailable-reason>
            {availability.state === 'checking' ? 'checking whether live voice is available…' : availability.reason}
          </span>
        </div>
      </div>
    )
  }

  return (
    <div className="live" data-live-availability="available">
      {!running
        ? micPill('Play Live', { onClick: start })
        : phase === 'narrating' || phase === 'paused'
          ? micPill('just ask', { onClick: interrupt, label: 'just ask (interrupt)' })
          : micPill(phase === 'connecting' ? 'connecting…' : phase === 'replying' ? 'replying…' : phase === 'resuming' ? 'resuming…' : 'listening…', {
              disabled: true,
            })}
      <div className="row">
        {running ? (
          <>
            <button type="button" onClick={stop}>
              Stop Live
            </button>
            {!micSlot && (phase === 'narrating' || phase === 'paused') && (
              <button type="button" onClick={interrupt}>
                Interrupt
              </button>
            )}
          </>
        ) : (
          !micSlot && (
            <button type="button" onClick={start}>
              Play Live
            </button>
          )
        )}
        <span className={phase === 'error' ? 'error' : 'hint'} role="status" data-live-phase={phase}>
          {/* "Just ask" only where the question can be spoken: the composed arm hears the
              interruption but takes the question typed. */}
          {phase === 'narrating' && live ? 'narrating — just ask: talk over it' : PHASE_LABEL[phase]}
          {arm && ` · ${arm}`}
        </span>
        {running && (
          <span className="hint" aria-label="mic level" data-live-mic-dbfs={micDbfs.toFixed(1)}>
            mic {'▮'.repeat(Math.min(8, Math.round(micLevel * 16)))} {micDbfs.toFixed(0)} dBFS
          </span>
        )}
      </div>
      {!running && (
        <p className="hint">Headphones recommended: the mic stays open for the whole session.</p>
      )}
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
                  <li key={index} data-live-assistant-line>
                    <strong>Motet:</strong> {line.text}
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
