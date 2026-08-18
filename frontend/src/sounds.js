/**
 * Short UI cues, synthesised with the Web Audio API.
 *
 * No audio files: these are sub-second tones, so generating them costs nothing,
 * ships nothing, and plays with no fetch/decode latency between the event and
 * the sound. It also keeps them out of the TTS path, so a cue can play while
 * speech is already playing without the two fighting over one <audio> element.
 *
 * Each cue is a distinct shape so you can tell them apart without looking:
 *   accepted — one soft rising blip  ("heard you")
 *   done     — two-note rising chime ("finished")
 *   error    — two-note falling tone ("failed")
 *   reminder — three-note arpeggio, louder ("look at me")
 */

let ctx = null;

function audioContext() {
  if (typeof window === "undefined") return null;
  const Ctor = window.AudioContext || window.webkitAudioContext;
  if (!Ctor) return null;
  if (!ctx) ctx = new Ctor();
  // Browsers start the context suspended until a user gesture; by the time a
  // cue plays the user has clicked something, so resuming here is enough.
  if (ctx.state === "suspended") ctx.resume().catch(() => {});
  return ctx;
}

/** One shaped sine note. Gain ramps avoid the click a raw start/stop makes. */
function note(context, { freq, start, duration, peak }) {
  const osc = context.createOscillator();
  const gain = context.createGain();
  osc.type = "sine";
  osc.frequency.setValueAtTime(freq, start);

  gain.gain.setValueAtTime(0.0001, start);
  gain.gain.exponentialRampToValueAtTime(peak, start + 0.015);
  gain.gain.exponentialRampToValueAtTime(0.0001, start + duration);

  osc.connect(gain).connect(context.destination);
  osc.start(start);
  osc.stop(start + duration + 0.02);
}

const CUES = {
  //        [freq, offset, duration, peak]
  accepted: [[660, 0, 0.1, 0.12]],
  done: [
    [660, 0, 0.12, 0.15],
    [880, 0.11, 0.18, 0.15],
  ],
  error: [
    [400, 0, 0.16, 0.14],
    [300, 0.15, 0.24, 0.14],
  ],
  reminder: [
    [784, 0, 0.14, 0.22],
    [988, 0.15, 0.14, 0.22],
    [1319, 0.3, 0.34, 0.22],
  ],
};

export function playCue(name) {
  const context = audioContext();
  if (!context) return;
  const cue = CUES[name];
  if (!cue) return;
  const now = context.currentTime + 0.01;
  try {
    for (const [freq, offset, duration, peak] of cue) {
      note(context, { freq, start: now + offset, duration, peak });
    }
  } catch (err) {
    console.warn("Could not play cue", name, err);
  }
}
