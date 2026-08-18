// Opens (or focuses) the DuDu results window. Under Tauri this spawns a real
// second OS window that loads the same bundle at #/results; in a plain browser
// (dev/build preview) it falls back to a popup, and callers can additionally
// render an in-app overlay. Returns true if a native/popup window was used.
export async function openResultsWindow() {
  const isTauri = typeof window !== "undefined" && !!window.__TAURI_INTERNALS__;

  if (isTauri) {
    try {
      const { WebviewWindow } = await import("@tauri-apps/api/webviewWindow");
      const existing = await WebviewWindow.getByLabel("results");
      if (existing) {
        await existing.show();
        await existing.setFocus();
        return true;
      }
      const win = new WebviewWindow("results", {
        url: "index.html#/results",
        title: "DuDu — Results",
        width: 760,
        height: 680,
        resizable: true,
        decorations: true,
        transparent: false,
        alwaysOnTop: false,
      });
      win.once("tauri://error", (e) => console.error("results window error", e));
      return true;
    } catch (err) {
      console.error("Failed to open Tauri results window:", err);
      return false;
    }
  }

  // Browser fallback: try a popup pointed at the same route.
  try {
    const popup = window.open(`${location.origin}/#/results`, "dudu-results", "width=760,height=680");
    if (popup) return true;
  } catch (err) {
    console.warn("Popup blocked, using in-app overlay:", err);
  }
  return false;
}
