"""The continuous audio loop:

    mic --> [always-on] wake-word detector
              |-- "wake_up"     --> SLEEPING -> IDLE
              |-- "go_to_sleep" --> * -> SLEEPING
    (while IDLE) wake_up already puts us in LISTENING for the next utterance
              --> VAD-gated recording --> Whisper STT --> LangGraph agent
              --> Piper TTS --> push audio+state over the WebSocket --> IDLE

Runs as a single background asyncio task (see main.py lifespan), completely
decoupled from any WebSocket client -- the mic keeps listening and the agent
keeps working even if no frontend is currently connected; state/audio events
just queue up for whoever connects next via ConnectionManager.
"""
from __future__ import annotations

import asyncio
import collections
from typing import AsyncIterator

import numpy as np
import sounddevice as sd
import webrtcvad
from loguru import logger

from config import settings
from state import AgentState
from tasks import task_manager
from voice.stt import transcribe
from voice.wake_word import SAMPLE_RATE, make_wake_detector
from ws_manager import manager

FRAME_SAMPLES = 1280  # 80ms @ 16kHz, matches openwakeword's expected chunk size
VAD_FRAME_MS = 30  # webrtcvad requires 10/20/30ms frames
VAD_FRAME_SAMPLES = int(SAMPLE_RATE * VAD_FRAME_MS / 1000)
SILENCE_MS_TO_STOP = int(settings.silence_ms_to_stop)  # trailing silence that ends an utterance
MAX_UTTERANCE_S = int(settings.max_utterance_s)  # VAD-ended capture
MAX_HOLD_S = int(settings.max_hold_s)            # hold-to-talk (you end it yourself)
LEADIN_MS = int(settings.listen_leadin_s * 1000)  # grace period to start talking after wake
MIN_SPEECH_RMS = 250  # ignore captures quieter than this (silence / mic noise)
MIC_GAIN = max(1.0, float(settings.mic_gain))  # amplify a quiet mic before wake/STT

# Set by the frontend "Stop" button (routed through main.ws_endpoint) to abort
# the current turn: skip the spoken reply and drop back to sleeping.
_stop_event = asyncio.Event()
_mic_enabled = asyncio.Event()
if settings.mic_enabled_on_start:
    _mic_enabled.set()

# Depth of the live capture buffer, in 80ms frames. ~2s: enough to absorb a
# scheduling hiccup, short enough that stale audio can't accumulate into lag.
MIC_QUEUE_FRAMES = 25

# Set by _mic_frames() so drain_mic() can flush stale audio after blocking work,
# and so set_microphone_enabled() can stop/start the device directly.
_mic_queue: "asyncio.Queue[np.ndarray] | None" = None
_mic_stream = None


def request_stop() -> None:
    _stop_event.set()


def set_microphone_enabled(enabled: bool) -> None:
    """Turn capture on/off, stopping the input stream outright while off.

    The stream is controlled from here rather than from inside _mic_frames()
    because the generator only reaches its own checks when someone asks it for
    the next frame -- and while muted nobody does. Driving stop/start from the
    toggle means the device is released the instant you mute, not whenever the
    consumer next happens to wake up.
    """
    if enabled:
        drain_mic()  # whatever sat in the buffer from before the pause is stale
        # Muting sets _stop_event to break anyone out of a blocked read. Clear it
        # here or the next capture aborts instantly, spinning the loop.
        _stop_event.clear()
        _mic_enabled.set()
        if _mic_stream is not None and not _mic_stream.active:
            _mic_stream.start()
    else:
        _mic_enabled.clear()
        if _mic_stream is not None and _mic_stream.active:
            _mic_stream.stop()
        # Anyone blocked awaiting a frame would wait forever now that capture
        # has stopped. Push a sentinel so they unblock and notice the mute.
        _stop_event.set()
        if _mic_queue is not None:
            try:
                _mic_queue.put_nowait(np.empty(0, dtype=np.int16))
            except asyncio.QueueFull:
                pass
    logger.info("Microphone {}", "enabled" if enabled else "disabled")


def is_microphone_enabled() -> bool:
    return _mic_enabled.is_set()


class MicrophoneUnavailable(RuntimeError):
    """Raised when the default input device cannot be opened."""


def _describe_input_devices() -> str:
    """Human-readable list of usable input devices, for the error message."""
    try:
        devices = sd.query_devices()
    except Exception:  # noqa: BLE001
        return "  (could not enumerate audio devices)"
    names = [
        f"  [{i}] {d['name']}" for i, d in enumerate(devices) if d.get("max_input_channels", 0) > 0
    ]
    return "\n".join(names) if names else "  (no input devices found)"


def drain_mic() -> int:
    """Throw away buffered microphone audio so the next read is LIVE.

    This is the fix for "I'm speaking and nothing happens". The capture callback
    keeps enqueuing frames the whole time Whisper is transcribing (1-2s) and
    while the agent starts up. Those frames are stale by the time anyone reads
    them, and because the consumer then works through the backlog before
    reaching live audio, the delay does not recover -- every utterance pushes
    the pipeline further behind real time, up to the queue's full depth.

    Call this after any blocking step. Dropping old audio is always correct
    here: nobody wants the assistant reacting to what they said ten seconds ago.
    """
    if _mic_queue is None:
        return 0
    dropped = 0
    while True:
        try:
            _mic_queue.get_nowait()
            dropped += 1
        except asyncio.QueueEmpty:
            break
    if dropped:
        logger.debug("Dropped {} stale mic frame(s) ({:.1f}s)", dropped, dropped * FRAME_SAMPLES / SAMPLE_RATE)
    return dropped


async def _mic_frames() -> AsyncIterator[np.ndarray]:
    """Yields int16 mono frames of FRAME_SAMPLES from the default input device."""
    global _mic_queue, _mic_stream
    loop = asyncio.get_event_loop()
    # Was maxsize=200 -- SIXTEEN SECONDS of audio. On a real-time path a deep
    # buffer is not a safety margin, it is a latency budget the pipeline will
    # happily spend. Bounded to ~2s: past that, old audio is worthless and
    # drop-oldest keeps us near live.
    queue: asyncio.Queue[np.ndarray] = asyncio.Queue(maxsize=MIC_QUEUE_FRAMES)
    _mic_queue = queue

    def _enqueue(frame: np.ndarray) -> None:
        # Real-time audio: if the consumer stalls (agent/STT busy), drop the
        # oldest frame rather than raising QueueFull and spamming errors.
        if queue.full():
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
        try:
            queue.put_nowait(frame)
        except asyncio.QueueFull:
            pass

    def _callback(indata, frames, time_info, status):  # sounddevice callback thread
        if status:
            logger.warning("Audio input status: {}", status)
        mono = indata[:, 0].copy()
        if MIC_GAIN != 1.0:
            mono = np.clip(mono.astype(np.float32) * MIC_GAIN, -32768, 32767).astype(np.int16)
        loop.call_soon_threadsafe(_enqueue, mono)

    try:
        stream = sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=1,
            dtype="int16",
            blocksize=FRAME_SAMPLES,
            callback=_callback,
        )
    except Exception as exc:  # noqa: BLE001 - PortAudioError and friends
        raise MicrophoneUnavailable(
            f"Could not open the default microphone at {SAMPLE_RATE}Hz mono: {exc}\n"
            f"Input devices seen by sounddevice:\n{_describe_input_devices()}\n"
            "Check Windows Settings > Privacy > Microphone, and that a default "
            "recording device is set."
        ) from exc

    _mic_stream = stream
    # Muted at startup? Release the device immediately -- don't hold an open
    # input stream just because the generator hasn't been asked for a frame yet.
    if not _mic_enabled.is_set() and stream.active:
        stream.stop()

    with stream:
        while True:
            if not _mic_enabled.is_set():
                # set_microphone_enabled() already stopped the device; just wait.
                await _mic_enabled.wait()
                drain_mic()  # discard anything captured before the pause
            yield await queue.get()


async def _record_utterance(frames: AsyncIterator[np.ndarray]) -> np.ndarray:
    """Records one full instruction. Waits (up to LEADIN_MS) for you to start
    talking, then keeps recording until it hears SILENCE_MS_TO_STOP of trailing
    silence -- so natural mid-sentence pauses don't cut you off. Returns empty
    if you never started speaking within the lead-in window."""
    vad = webrtcvad.Vad(2)  # 0-3, higher = more aggressive filtering of non-speech
    collected: list[np.ndarray] = []
    silence_ms = 0
    total_ms = 0.0
    speech_started = False

    leftover = np.empty(0, dtype=np.int16)
    async for frame in frames:
        collected.append(frame)
        total_ms += len(frame) / SAMPLE_RATE * 1000

        buf = np.concatenate([leftover, frame])
        n_full = len(buf) // VAD_FRAME_SAMPLES
        for i in range(n_full):
            chunk = buf[i * VAD_FRAME_SAMPLES : (i + 1) * VAD_FRAME_SAMPLES]
            if vad.is_speech(chunk.tobytes(), SAMPLE_RATE):
                speech_started = True
                silence_ms = 0
            else:
                silence_ms += VAD_FRAME_MS
        leftover = buf[n_full * VAD_FRAME_SAMPLES :]

        if _stop_event.is_set():
            break
        if not speech_started:
            # Still waiting for you to begin -- give up only after the lead-in.
            if total_ms >= LEADIN_MS:
                return np.empty(0, dtype=np.int16)
            continue
        if silence_ms >= SILENCE_MS_TO_STOP or total_ms >= MAX_UTTERANCE_S * 1000:
            break

    return np.concatenate(collected) if speech_started and collected else np.empty(0, dtype=np.int16)


VOICE_MODE = settings.voice_mode.lower()
PUSH_TO_TALK = VOICE_MODE == "push_to_talk"
HOLD_TO_TALK = VOICE_MODE == "hold_to_talk"

# Set while the user physically holds the Talk button / spacebar.
_ptt_held = asyncio.Event()

# A hold shorter than this is a mis-click (or the button losing the pointer),
# not speech. Reported explicitly rather than silently dropped.
MIN_HOLD_SECONDS = 0.35

# Loudness floor for held audio. Much lower than MIN_SPEECH_RMS: pressing the
# button is already an unambiguous intent signal, so this only needs to catch a
# genuinely dead input, not to second-guess a quiet microphone.
MIN_HELD_SPEECH_RMS = 40


def start_ptt() -> None:
    """Talk button pressed: open the mic and begin recording."""
    set_microphone_enabled(True)
    _ptt_held.set()


def stop_ptt() -> None:
    """Talk button released. Only clears the flag -- the capture loop closes the
    recording and shuts the device down, so we never stop the stream out from
    under a read that's still in flight."""
    _ptt_held.clear()


async def _record_while_held(mic: AsyncIterator[np.ndarray]) -> np.ndarray:
    """Record for exactly as long as the button is held.

    No VAD end-pointing here, deliberately. Releasing the button is an explicit
    "I'm done", which is strictly better information than inferring it from
    silence -- it's instant, it can't clip you mid-pause, and it can't be fooled
    by background noise.
    """
    collected: list[np.ndarray] = []
    seconds = 0.0
    async for frame in mic:
        if frame.size:  # skip the mute sentinel
            collected.append(frame)
            seconds += len(frame) / SAMPLE_RATE
        if not _ptt_held.is_set():
            break
        if seconds >= MAX_HOLD_S:
            logger.warning(
                "Hit the {}s hold cap -- recording stopped and your sentence was cut off. "
                "Raise MAX_HOLD_S in .env if you need to dictate for longer.",
                MAX_HOLD_S,
            )
            break
    return np.concatenate(collected) if collected else np.empty(0, dtype=np.int16)


async def _hold_to_talk_loop(mic: AsyncIterator[np.ndarray]) -> None:
    """Wait for the button, record while held, transcribe on release."""
    while True:
        await _ptt_held.wait()
        drain_mic()
        await manager.send_state(AgentState.LISTENING, detail="recording — release to send")

        # Bounded: _record_while_held only notices the release when the NEXT
        # frame arrives, so a device that stops delivering (unplugged headset,
        # driver stall) would otherwise hang here forever with the mic flagged
        # open and no way to release it from the UI.
        try:
            audio = await asyncio.wait_for(_record_while_held(mic), timeout=MAX_HOLD_S + 5)
        except asyncio.TimeoutError:
            logger.error("Recording timed out -- the microphone stopped delivering audio")
            audio = np.empty(0, dtype=np.int16)
            await manager.send_error("The microphone stopped responding. Try toggling it off and on.")

        # Close the device the moment the button comes up: in this mode the mic
        # is open only for the duration of the hold, nothing else.
        set_microphone_enabled(False)
        await manager.send_mic(False)

        rms = float(np.sqrt(np.mean(audio.astype(np.float32) ** 2))) if audio.size else 0.0
        seconds = len(audio) / SAMPLE_RATE if audio.size else 0.0
        # Always log both numbers: "no speech captured" on its own can't
        # distinguish a quiet microphone from a hold that ended instantly.
        logger.info("Talk released: {:.2f}s captured, rms {:.0f}", seconds, rms)

        if seconds < MIN_HOLD_SECONDS:
            # Far too short to be speech -- almost always the button losing the
            # pointer rather than the user actually letting go.
            logger.warning(
                "Hold lasted only {:.2f}s -- treating as a mis-click, not speech.", seconds
            )
            await manager.send_error("That press was too brief — hold the button while you speak.")
            await manager.send_state(AgentState.SLEEPING, detail=sleeping_detail())
            continue

        # Deliberately far more permissive than the wake-word path's threshold.
        # There, a gate stops Whisper hallucinating words out of room tone. Here
        # the user physically held a button to speak, so there is no phantom
        # trigger to guard against -- rejecting their audio because their mic
        # runs quiet is the worse failure. Let Whisper decide.
        if audio.size == 0 or rms < MIN_HELD_SPEECH_RMS:
            logger.info("Captured audio looks like silence (rms {:.0f})", rms)
            await manager.send_error(
                f"Didn't hear anything (level {rms:.0f}). Check the right input device is "
                "selected, or raise MIC_GAIN in .env."
            )
            await manager.send_state(AgentState.SLEEPING, detail=sleeping_detail())
            continue

        await manager.send_state(AgentState.THINKING, detail="transcribing")
        text = await transcribe(audio)
        drain_mic()

        if text:
            await task_manager.submit(text, source="voice")
        else:
            await manager.send_error("Didn't catch that — try holding Talk a moment longer.")
        await manager.send_state(AgentState.SLEEPING, detail=sleeping_detail())

# Spoken phrases that turn the mic back off in push-to-talk mode, so you don't
# have to reach for the UI to stop it listening.
_STOP_LISTENING_PHRASES = ("go to sleep", "stop listening", "dudu stop")


def sleeping_detail() -> str:
    """What the avatar should say it's doing while idle."""
    if HOLD_TO_TALK:
        return "hold Talk to speak"
    if not is_microphone_enabled():
        return "microphone off"
    return "listening — just speak" if PUSH_TO_TALK else "waiting for wake word"


async def _push_to_talk_loop(mic: AsyncIterator[np.ndarray]) -> None:
    """Mic on == actively taking dictation. No wake word.

    The wake-word design requires EVERY utterance to begin with "Dudu"; anything
    else is transcribed, found not to match, and thrown away -- which looks
    exactly like "I spoke, it said listening, then went back to sleep". When the
    user has explicitly unmuted, the unmute IS the activation signal, the same
    way tapping the mic button in Claude or Gemini is. So here we just capture,
    transcribe and hand straight to the agent.
    """
    await manager.send_state(AgentState.LISTENING, detail="listening — just speak")

    while is_microphone_enabled():
        audio = await _record_utterance(mic)

        rms = float(np.sqrt(np.mean(audio.astype(np.float32) ** 2))) if audio.size else 0.0
        if audio.size == 0 or rms < MIN_SPEECH_RMS:
            continue  # silence or the lead-in expiring: keep listening
        if _stop_event.is_set():
            _stop_event.clear()
            continue

        await manager.send_state(AgentState.THINKING, detail="transcribing")
        text = await transcribe(audio)
        drain_mic()  # Whisper just blocked us; resume from live audio

        if not text:
            await manager.send_state(AgentState.LISTENING, detail="listening — just speak")
            continue

        if any(p in text.lower() for p in _STOP_LISTENING_PHRASES):
            logger.info("Spoken stop phrase heard -- muting")
            set_microphone_enabled(False)
            await manager.send_mic(False)
            return

        await task_manager.submit(text, source="voice")
        drain_mic()
        # Straight back to listening -- the agent runs in the background, so you
        # can queue another instruction without waiting for the answer.
        await manager.send_state(AgentState.LISTENING, detail="listening — just speak")


async def voice_loop() -> None:
    detector = make_wake_detector()
    state = AgentState.SLEEPING
    await manager.send_state(state, detail=sleeping_detail())

    mic = _mic_frames()
    dead_mic_rounds = 0

    while True:
        try:
            if HOLD_TO_TALK:
                await manager.send_state(AgentState.SLEEPING, detail="hold Talk to speak")
                await _hold_to_talk_loop(mic)
                continue

            if PUSH_TO_TALK:
                if not is_microphone_enabled():
                    await manager.send_state(AgentState.SLEEPING, detail="microphone off")
                    await _mic_enabled.wait()
                    drain_mic()
                await _push_to_talk_loop(mic)
                await manager.send_state(AgentState.SLEEPING, detail=sleeping_detail())
                continue

            if state == AgentState.SLEEPING:
                # Block here until a wake word fires.
                heard_anything = False
                async for label in detector.listen(mic):
                    heard_anything = True
                    if label == "wake_up":
                        state = AgentState.IDLE
                        await manager.send_state(state)
                        break
                    # ignore "go_to_sleep" while already sleeping

                if not heard_anything:
                    # The detector's `async for` over the mic ended without
                    # producing anything, which means the mic generator itself
                    # is finished (device unplugged, stream closed). Previously
                    # this spun a tight loop at 100% CPU forever. Back off and
                    # rebuild the stream so replugging a headset recovers.
                    dead_mic_rounds += 1
                    if dead_mic_rounds >= 3:
                        logger.error("Microphone stream ended; rebuilding it in 5s")
                        await manager.send_error(
                            "Lost the microphone. Retrying — you can still type instructions."
                        )
                        await asyncio.sleep(5)
                        mic = _mic_frames()
                        dead_mic_rounds = 0
                    else:
                        await asyncio.sleep(0.5)
                else:
                    dead_mic_rounds = 0
                continue

            # AWAKE: a wake word fired. If the wake utterance already carried the
            # instruction in the same breath ("Dudu find the SQL view"), run it
            # directly; otherwise fall back to recording the next utterance.
            _stop_event.clear()

            pending = getattr(detector, "pending_command", "")
            if pending:
                setattr(detector, "pending_command", "")
                if _matches(pending, "go to sleep"):
                    state = AgentState.SLEEPING
                    await manager.send_state(state, detail=sleeping_detail())
                    continue
                await task_manager.submit(pending, source="voice")
                state = AgentState.SLEEPING
                await manager.send_state(state, detail=sleeping_detail())
                # Submitting spawns the agent; whatever the mic captured during
                # that is stale. Start the next wake cycle from live audio.
                drain_mic()
                continue

            state = AgentState.LISTENING
            await manager.send_state(state)
            drain_mic()  # the wake word itself is already consumed -- record from NOW
            audio = await _record_utterance(mic)

            # Energy gate: ignore silence / faint mic noise so a quiet or absent
            # mic can't trigger phantom turns (Whisper hallucinates on silence).
            rms = float(np.sqrt(np.mean(audio.astype(np.float32) ** 2))) if audio.size else 0.0
            if audio.size == 0 or rms < MIN_SPEECH_RMS or _stop_event.is_set():
                state = AgentState.SLEEPING
                await manager.send_state(state, detail=sleeping_detail())
                continue

            state = AgentState.THINKING
            await manager.send_state(state)

            text = await transcribe(audio)
            if not text or _stop_event.is_set():
                state = AgentState.SLEEPING
                await manager.send_state(state, detail=sleeping_detail())
                continue

            if _matches(text, "go to sleep"):
                state = AgentState.SLEEPING
                await manager.send_state(state, detail=sleeping_detail())
                continue

            # Hand the utterance to the concurrent task manager (it owns the
            # user transcript, agent run, 4-5 line summary, TTS and avatar
            # state). We return to sleeping right away so the mic can catch the
            # next wake word while the task runs in the background.
            await task_manager.submit(text, source="voice")

            state = AgentState.SLEEPING
            await manager.send_state(state, detail=sleeping_detail())
            drain_mic()  # transcription + submit took time; resume from live audio

        except asyncio.CancelledError:
            raise
        except MicrophoneUnavailable as exc:
            # Not recoverable by retrying quickly, and worth telling the user
            # about once rather than burying it in the log: the app is still
            # fully usable by typing, it just can't hear them.
            logger.error("{}", exc)
            await manager.send_error(
                "No microphone available — voice is off, but you can still type instructions."
            )
            state = AgentState.SLEEPING
            await manager.send_state(state, detail="no microphone")
            await asyncio.sleep(30)
            mic = _mic_frames()
        except Exception:  # noqa: BLE001 - never let the background task die silently
            logger.exception("voice_loop iteration failed, recovering to sleeping")
            state = AgentState.SLEEPING
            await manager.send_state(state, detail=sleeping_detail())
            await asyncio.sleep(1)


def _matches(text: str, phrase: str) -> bool:
    return phrase in text.lower()
