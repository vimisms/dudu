import { useCallback, useEffect, useMemo, useState } from "react";
import { useAgentSocket } from "./hooks/useAgentSocket.js";
import { openResultsWindow } from "./resultsWindow.js";
import Results from "./Results.jsx";
import idleImg from "./assets/images/idle.svg";
import listeningImg from "./assets/images/listening.svg";
import thinkingImg from "./assets/images/thinking.svg";
import talkingImg from "./assets/images/talking.svg";

// SLEEPING has no dedicated image -- it reuses the idle image, dimmed via CSS.
const IMAGE_BY_STATE = {
  sleeping: idleImg,
  idle: idleImg,
  listening: listeningImg,
  thinking: thinkingImg,
  talking: talkingImg,
};

const STATUS_LABEL = {
  queued: "Queued",
  running: "Running",
  done: "Done",
  error: "Failed",
  cancelled: "Cancelled",
};

export default function App() {
  const [draft, setDraft] = useState("");
  const [showOverlay, setShowOverlay] = useState(false);
  const [pinned, setPinned] = useState(true);

  // Open output as soon as a task is accepted so progress and text are visible.
  const showResults = useCallback(async () => {
    const opened = await openResultsWindow();
    if (!opened) setShowOverlay(true);
  }, []);

  const {
    connected, agentState, stateDetail, tasks, muted, micOn, voiceMode, lastError, clearError,
    lastReminder, clearReminder,
    sendCommand, sendMicToggle, sendCancel, sendStop, sendClearTasks, sendMute,
    sendPttStart, sendPttStop,
  } = useAgentSocket({ onTaskStart: showResults });

  const holdToTalk = voiceMode === "hold_to_talk";
  const [talking, setTalking] = useState(false);

  // Press = start recording, release = send. Guarded so a repeat keydown or a
  // release outside the button can't leave the mic stuck open.
  const beginTalk = useCallback(() => {
    setTalking((was) => {
      if (!was) sendPttStart();
      return true;
    });
  }, [sendPttStart]);

  const endTalk = useCallback(() => {
    setTalking((was) => {
      if (was) sendPttStop();
      return false;
    });
  }, [sendPttStop]);

  // Spacebar as the hotkey, but never while typing in the composer.
  useEffect(() => {
    if (!holdToTalk) return undefined;
    const isTyping = (e) => ["INPUT", "TEXTAREA"].includes(e.target?.tagName);
    const down = (e) => {
      if (e.code !== "Space" || e.repeat || isTyping(e)) return;
      e.preventDefault();
      beginTalk();
    };
    const up = (e) => {
      if (e.code !== "Space" || isTyping(e)) return;
      e.preventDefault();
      endTalk();
    };
    // Releasing outside the window would otherwise strand the mic open.
    window.addEventListener("keydown", down);
    window.addEventListener("keyup", up);
    window.addEventListener("blur", endTalk);
    return () => {
      window.removeEventListener("keydown", down);
      window.removeEventListener("keyup", up);
      window.removeEventListener("blur", endTalk);
    };
  }, [holdToTalk, beginTalk, endTalk]);

  const imgSrc = useMemo(() => IMAGE_BY_STATE[agentState] ?? IMAGE_BY_STATE.idle, [agentState]);
  const ordered = useMemo(
    () => [...tasks].sort((a, b) => (b.created_at ?? 0) - (a.created_at ?? 0)),
    [tasks]
  );
  const activeCount = tasks.filter((t) => t.status === "queued" || t.status === "running").length;

  function toggleMic() {
    sendMicToggle(!micOn);
  }

  function submitDraft() {
    const text = draft.trim();
    if (!text) return;
    showResults();
    sendCommand(text);
    setDraft("");
  }

  function onInputKeyDown(e) {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      submitDraft();
    }
  }

  async function minimizeWindow() {
    if (!window.__TAURI_INTERNALS__) return;
    const { getCurrentWindow } = await import("@tauri-apps/api/window");
    await getCurrentWindow().minimize();
  }

  async function togglePin() {
    const next = !pinned;
    setPinned(next);
    if (!window.__TAURI_INTERNALS__) return;
    const { getCurrentWindow } = await import("@tauri-apps/api/window");
    await getCurrentWindow().setAlwaysOnTop(next);
  }

  return (
    <div className="app" data-state={agentState}>
      <div className="titlebar">
        <span className="titlebar-name">DuDu</span>
        <div className="titlebar-btns">
          <button className="tbtn" title={pinned ? "Unpin (allow behind other windows)" : "Pin on top"} onClick={togglePin}>
            {pinned ? "📌" : "📍"}
          </button>
          <button className="tbtn" title="Minimize" onClick={minimizeWindow}>—</button>
        </div>
      </div>

      <div className={`avatar-frame ${agentState === "sleeping" ? "dimmed" : ""}`}>
        <img key={imgSrc} className="avatar-img" src={imgSrc} alt={`DuDu is ${agentState}`} />
      </div>

      <div className="status-row">
        <span className={`dot ${connected ? "dot-online" : "dot-offline"}`} />
        <span className="state-label">{agentState}</span>
        {activeCount > 0 && <span className="state-detail">— {activeCount} running</span>}
        {activeCount === 0 && stateDetail && <span className="state-detail">— {stateDetail}</span>}
      </div>

      {/* Positioned directly under the status row, ABOVE every conditional
          banner and the composer. Anything that can appear or disappear
          above this button shifts it mid-hold -- which is how it ended up
          sliding out from under the cursor. Only fixed-height elements sit
          above it now. */}
      {holdToTalk && (
        <button
          className={`talk-btn ${talking ? "talking" : ""}`}
          // Pointer capture routes every subsequent pointer event to THIS
          // element until release, even if the cursor wanders off it. Without
          // it we had to rely on onPointerLeave to catch drags -- and since the
          // button also changed size when pressed, the edge slid out from under
          // a perfectly still cursor and "left" fired instantly, ending the
          // recording a few milliseconds after it began.
          onPointerDown={(e) => {
            try { e.currentTarget.setPointerCapture(e.pointerId); } catch { /* older webview */ }
            beginTalk();
          }}
          onPointerUp={(e) => {
            try { e.currentTarget.releasePointerCapture(e.pointerId); } catch { /* ignore */ }
            endTalk();
          }}
          onPointerCancel={endTalk}
          onContextMenu={(e) => e.preventDefault()}
        >
          {talking ? "● Recording — release to send" : "🎙️ Hold to Talk — or hold Space"}
        </button>
      )}

      {agentState === "sleeping" && !micOn && !holdToTalk && (
        <div className="hint-banner muted-hint">
          🔇 Mic is <b>off</b> — nothing is being recorded. Type below, or tap
          <b> 🎙️ Mic</b> to start listening.
        </div>
      )}
      {agentState === "sleeping" && micOn && voiceMode !== "push_to_talk" && (
        <div className="hint-banner">
          Say <b>"Dudu"</b> + your instruction — e.g. <i>"Dudu, find the SQL view for inventory closing"</i>
        </div>
      )}
      {micOn && voiceMode === "push_to_talk" && agentState !== "thinking" && !holdToTalk && (
        <div className="hint-banner listening-hint">
          🎙️ <b>Listening</b> — just speak, no wake word needed. Say
          <b> "stop listening"</b> or tap the mic to stop.
        </div>
      )}
      {/* Hidden in hold-to-talk: "say your instruction, then pause" is wrong
          advice there (you release the button, you don't pause), and the button
          already turns red and reads "Recording". */}
      {agentState === "listening" && !holdToTalk && (
        <div className="listening-banner">🎙️ Listening — say your instruction, then pause</div>
      )}

      <div className="composer">
        <textarea
          className="composer-input"
          placeholder="Type an instruction… (Enter to send)"
          value={draft}
          rows={2}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={onInputKeyDown}
        />
        <button className="composer-send" onClick={submitDraft} disabled={!draft.trim()}>
          Send
        </button>
      </div>


      <div className="controls">
        {!holdToTalk && (
          <button className={`chip ${micOn ? "on" : "off"}`} onClick={toggleMic}>
            {micOn ? "🎙️ Mic" : "🔇 Mic"}
          </button>
        )}
        <button className={`chip ${muted ? "off" : "on"}`} onClick={() => sendMute(!muted)}>
          {muted ? "🔈 Muted" : "🔊 Sound"}
        </button>
        <button className="chip stop" onClick={sendStop} disabled={activeCount === 0}>
          ■ Stop
        </button>
      </div>

      {!connected && (
        <div className="error-banner">
          Backend not reachable on 127.0.0.1:8756 — is it running?
        </div>
      )}
      {lastReminder && (
        <div className="reminder-banner" role="button" title="Dismiss" onClick={clearReminder}>
          ⏰ <b>Reminder:</b> {lastReminder.text} <span className="error-dismiss">✕</span>
        </div>
      )}
      {lastError && (
        <div className="error-banner" role="button" title="Dismiss" onClick={clearError}>
          {lastError} <span className="error-dismiss">✕</span>
        </div>
      )}

      <div className="tasklist">
        <div className="tasklist-head">
          <span>Tasks{tasks.length > 0 ? ` (${tasks.length})` : ""}</span>
          {tasks.some((t) => t.status === "done" || t.status === "error" || t.status === "cancelled") && (
            <button className="tasklist-clear" onClick={sendClearTasks}>Clear finished</button>
          )}
        </div>
        {ordered.length === 0 && <div className="tasklist-empty">No tasks yet — send one above.</div>}
        {ordered.map((t) => (
          <div key={t.id} className={`task task-${t.status}`}>
            <div className="task-row">
              <span className={`status status-${t.status}`}>{STATUS_LABEL[t.status] ?? t.status}</span>
              {(t.status === "queued" || t.status === "running") && (
                <button className="task-cancel" title="Cancel" onClick={() => sendCancel(t.id)}>✕</button>
              )}
              {t.status === "done" && (
                <button className="task-view" onClick={showResults}>View ↗</button>
              )}
            </div>
            <div className="task-instruction">{t.instruction || "(empty)"}</div>
            {t.status === "running" && <div className="task-phase">{t.phase || "Working"}</div>}
            {t.status === "done" && t.summary && <div className="task-summary">{t.summary}</div>}
            {t.status === "error" && t.error && <div className="task-error">{t.error}</div>}
          </div>
        ))}
      </div>

      {showOverlay && (
        <div className="overlay">
          <button className="overlay-close" onClick={() => setShowOverlay(false)}>✕ Close</button>
          <Results />
        </div>
      )}
    </div>
  );
}
