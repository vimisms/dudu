import { useCallback, useEffect, useRef, useState } from "react";
import { playCue } from "../sounds.js";

const WS_URL = "ws://127.0.0.1:8756/ws";
const RECONNECT_DELAY_MS = 1500;

/**
 * Owns the WebSocket connection to the FastAPI backend. Event-driven: the
 * backend pushes {type:"state"|"transcript"|"audio"|"error"|"task"|"snapshot"}
 * messages as the voice loop / task manager progress, so the UI never polls.
 *
 * Both the main avatar window and the results window use this hook -- each is
 * an independent WS client that receives a snapshot on connect plus live task
 * events, so they stay in sync without any cross-window IPC.
 *
 * @param {object} opts
 * @param {(task:object)=>void} [opts.onTaskStart] called when a task is accepted.
 * @param {(task:object)=>void} [opts.onTaskComplete] called when a task flips to "done".
 * @param {boolean} [opts.playAudio] whether this window should play TTS audio (default true).
 */
export function useAgentSocket({ onTaskStart, onTaskComplete, playAudio = true } = {}) {
  const [connected, setConnected] = useState(false);
  const [agentState, setAgentState] = useState("sleeping");
  const [stateDetail, setStateDetail] = useState("");
  const [transcript, setTranscript] = useState([]); // [{role, text}]
  const [tasks, setTasks] = useState([]); // [{id, instruction, status, output, summary, ...}]
  const [muted, setMuted] = useState(false);
  // Mic starts OFF and is corrected by the backend's snapshot on connect. It is
  // deliberately NOT a local-only guess: the backend owns whether the device is
  // actually open, and a toggle that lies about that is worse than no toggle.
  const [micOn, setMicOn] = useState(false);
  const [voiceMode, setVoiceMode] = useState("push_to_talk");
  const [lastError, setLastError] = useState(null);
  const [lastReminder, setLastReminder] = useState(null);

  const wsRef = useRef(null);
  const audioRef = useRef(null);
  const reconnectTimer = useRef(null);
  const onTaskStartRef = useRef(onTaskStart);
  const onTaskCompleteRef = useRef(onTaskComplete);
  const mutedRef = useRef(muted);
  const seenStartedRef = useRef(new Set());
  const seenDoneRef = useRef(new Set()); // task ids we've already fired complete for

  useEffect(() => { onTaskStartRef.current = onTaskStart; }, [onTaskStart]);
  useEffect(() => { onTaskCompleteRef.current = onTaskComplete; }, [onTaskComplete]);
  useEffect(() => { mutedRef.current = muted; }, [muted]);

  const doPlayAudio = useCallback((base64Wav) => {
    if (!playAudio || mutedRef.current) return;
    const src = `data:audio/wav;base64,${base64Wav}`;
    if (!audioRef.current) audioRef.current = new Audio();
    audioRef.current.muted = mutedRef.current;
    audioRef.current.src = src;
    audioRef.current.play().catch((err) => console.warn("Autoplay blocked:", err));
  }, [playAudio]);

  const upsertTask = useCallback((task) => {
    setTasks((prev) => {
      const idx = prev.findIndex((t) => t.id === task.id);
      if (idx === -1) return [...prev, task];
      const next = prev.slice();
      next[idx] = task;
      return next;
    });
    if ((task.status === "queued" || task.status === "running") && !seenStartedRef.current.has(task.id)) {
      seenStartedRef.current.add(task.id);
      onTaskStartRef.current?.(task);
    }
    if (task.status === "done" && !seenDoneRef.current.has(task.id)) {
      seenDoneRef.current.add(task.id);
      onTaskCompleteRef.current?.(task);
    }
  }, []);

  useEffect(() => {
    let cancelled = false;

    function connect() {
      const ws = new WebSocket(WS_URL);
      wsRef.current = ws;

      ws.onopen = () => !cancelled && setConnected(true);
      ws.onclose = () => {
        if (cancelled) return;
        setConnected(false);
        reconnectTimer.current = setTimeout(connect, RECONNECT_DELAY_MS);
      };
      ws.onerror = () => ws.close();

      ws.onmessage = (event) => {
        let msg;
        try { msg = JSON.parse(event.data); } catch { return; }
        switch (msg.type) {
          case "state":
            setAgentState(msg.state);
            setStateDetail(msg.detail ?? "");
            break;
          case "transcript":
            setTranscript((prev) => [...prev.slice(-49), { role: msg.role, text: msg.text }]);
            break;
          case "audio":
            doPlayAudio(msg.data);
            break;
          case "task":
            upsertTask(msg.task);
            break;
          case "sound":
            // Only the window that owns audio playback chimes -- otherwise the
            // main window and the results window would double every cue.
            if (playAudio && !mutedRef.current) playCue(msg.name);
            break;
          case "reminder":
            setLastReminder({ text: msg.text, at: Date.now() });
            break;
          case "mic":
            setMicOn(!!msg.on);
            break;
          case "snapshot":
            setTasks(msg.tasks ?? []);
            setMuted(!!msg.muted);
            setMicOn(!!msg.mic);
            if (msg.voice_mode) setVoiceMode(msg.voice_mode);
            (msg.tasks ?? []).forEach((t) => seenStartedRef.current.add(t.id));
            (msg.tasks ?? []).forEach((t) => { if (t.status === "done") seenDoneRef.current.add(t.id); });
            break;
          case "error":
            setLastError(msg.message);
            break;
          default:
            break;
        }
      };
    }

    connect();
    return () => {
      cancelled = true;
      clearTimeout(reconnectTimer.current);
      wsRef.current?.close();
    };
  }, [doPlayAudio, upsertTask]);

  const send = useCallback((obj) => {
    const ws = wsRef.current;
    if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(obj));
  }, []);

  const clearError = useCallback(() => setLastError(null), []);
  const sendPttStart = useCallback(() => send({ type: "ptt_start" }), [send]);
  const sendPttStop = useCallback(() => send({ type: "ptt_stop" }), [send]);
  const sendCommand = useCallback((text) => send({ type: "command", text }), [send]);
  const sendMicToggle = useCallback((on) => {
    setMicOn(on);  // optimistic; the backend's "mic" broadcast confirms it
    send({ type: "mic_toggle", on });
  }, [send]);
  const sendCancel = useCallback((id) => send({ type: "cancel_task", id }), [send]);
  const sendStop = useCallback(() => send({ type: "stop" }), [send]);
  const sendClearTasks = useCallback(() => {
    send({ type: "clear_tasks" });
    setTasks((prev) => prev.filter((t) => t.status === "queued" || t.status === "running"));
  }, [send]);
  const sendMute = useCallback((on) => {
    setMuted(on);
    if (audioRef.current) audioRef.current.muted = on;
    send({ type: "mute", on });
  }, [send]);

  return {
    connected, agentState, stateDetail, transcript, tasks, muted, micOn, voiceMode,
    lastError, clearError, lastReminder,
    clearReminder: () => setLastReminder(null),
    sendCommand, sendMicToggle, sendCancel, sendStop, sendClearTasks, sendMute,
    sendPttStart, sendPttStop,
  };
}
