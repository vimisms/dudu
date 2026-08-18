import { useEffect, useMemo, useRef, useState } from "react";
import { useAgentSocket } from "./hooks/useAgentSocket.js";
import MarkdownView from "./MarkdownView.jsx";
import "./Results.css";

const STATUS_LABEL = {
  queued: "Queued",
  running: "Running",
  done: "Done",
  error: "Failed",
  cancelled: "Cancelled",
};

function when(ts) {
  if (!ts) return "";
  try {
    return new Date(ts * 1000).toLocaleTimeString();
  } catch {
    return "";
  }
}

/**
 * The results window: an independent WS client. Shows a sidebar of all tasks
 * and, for the selected one, its 4-5 line summary plus the full rich-text
 * output. Auto-selects the newest completed task as results arrive.
 */
export default function Results() {
  const { connected, tasks } = useAgentSocket({ playAudio: false });
  const [selectedId, setSelectedId] = useState(null);
  const newestTaskRef = useRef(null);

  // Newest first for the list.
  const ordered = useMemo(
    () => [...tasks].sort((a, b) => (b.created_at ?? 0) - (a.created_at ?? 0)),
    [tasks]
  );

  // Select each newly-created task once, without fighting manual sidebar choices.
  useEffect(() => {
    const newest = ordered[0];
    if (newest && newest.id !== newestTaskRef.current) {
      newestTaskRef.current = newest.id;
      setSelectedId(newest.id);
    }
  }, [ordered]);

  // If nothing chosen yet, fall back to the newest task overall.
  const selected = useMemo(() => {
    return ordered.find((t) => t.id === selectedId) ?? ordered[0] ?? null;
  }, [ordered, selectedId]);

  return (
    <div className="results">
      <aside className="results-sidebar">
        <div className="results-sidebar-head">
          <span className={`dot ${connected ? "dot-online" : "dot-offline"}`} />
          Tasks
        </div>
        {ordered.length === 0 && <div className="results-empty">No tasks yet.</div>}
        {ordered.map((t) => (
          <button
            key={t.id}
            className={`results-item ${selected?.id === t.id ? "active" : ""}`}
            onClick={() => setSelectedId(t.id)}
          >
            <div className="results-item-top">
              <span className={`status status-${t.status}`}>{STATUS_LABEL[t.status] ?? t.status}</span>
              <span className="results-item-time">{when(t.finished_at || t.created_at)}</span>
            </div>
            <div className="results-item-instruction">{t.instruction || "(empty instruction)"}</div>
          </button>
        ))}
      </aside>

      <main className="results-main">
        {!selected && <div className="results-placeholder">Select a task to see its output.</div>}
        {selected && (
          <>
            <header className="results-header">
              <span className={`status status-${selected.status}`}>
                {STATUS_LABEL[selected.status] ?? selected.status}
              </span>
              <h1 className="results-title">{selected.instruction}</h1>
            </header>

            {selected.summary && (
              <section className="results-summary">
                <h2>Summary</h2>
                <p>{selected.summary}</p>
              </section>
            )}

            {selected.error && (
              <section className="results-error">
                <h2>Error</h2>
                <pre>{selected.error}</pre>
              </section>
            )}

            <section className="results-output">
              <div className="results-output-head">
                <h2>Live output</h2>
                {(selected.status === "running" || selected.status === "queued") && (
                  <span className="results-live"><i />{selected.phase || "Working"}</span>
                )}
              </div>
              {selected.output ? (
                <MarkdownView text={selected.output} />
              ) : selected.status === "running" || selected.status === "queued" ? (
                <div className="results-working">
                  <span className="working-pulse" />
                  {selected.phase || "Getting started"}
                </div>
              ) : null}
            </section>
          </>
        )}
      </main>
    </div>
  );
}
