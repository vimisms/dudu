// Minimal shell: all real logic lives in the Python FastAPI backend. Tauri's
// job here is just to host the transparent, always-on-top avatar window and
// (optionally) spawn/manage the backend process alongside the app.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

fn main() {
    tauri::Builder::default()
        .setup(|_app| {
            // Hook point: if you want Tauri to launch `python backend/main.py`
            // itself (instead of running it separately during dev), use the
            // `tauri-plugin-shell` sidecar API here.
            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("error while running DuDu");
}
