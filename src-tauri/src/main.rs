#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]
use serde_json::{json, Value};
use std::{collections::HashMap, io::{BufRead, BufReader, Write}, process::{Child, ChildStdin, Command, Stdio}, sync::{Arc, Mutex, atomic::{AtomicBool, Ordering}}, time::{SystemTime, UNIX_EPOCH}};
use tauri::{Emitter, Manager};

struct Controller { stdin: Mutex<Option<ChildStdin>>, child: Mutex<Child>, first_snapshot: Arc<Mutex<bool>>, actions: Arc<Mutex<HashMap<String, String>>>, allow_close: AtomicBool }
const ACTIONS: &[&str] = &["app.snapshot", "app.preferences", "app.diagnostics", "account.importLegacy", "account.connect", "account.submitCode", "account.select", "account.usage", "account.remove", "account.update", "account.credentials", "session.start", "session.cancel", "session.reconnect", "session.restart", "session.stop", "session.freeMemory", "session.runtimeSave", "session.open", "model.list", "download.list", "node.catalog", "node.versions", "workflow.list", "legacy.list", "legacy.import", "download.add", "download.cancel", "download.resume", "cache.prepare", "cache.cancel", "cache.clear", "workflow.check", "workflow.import", "node.list", "node.install", "node.update", "node.remove", "drive.copy", "drive.share", "history.list", "history.export", "shell.openExternal", "shell.ready", "shell.close"];
fn authorization_url(value: &str) -> Result<String, String> {
    let url = url::Url::parse(value).map_err(|_| "Invalid Google authorization link. Reconnect the account.")?;
    if value.len() > 16384 || url.scheme() != "https" || ![Some("accounts.google.com"), Some("sdk.cloud.google.com")].contains(&url.host_str()) || !url.username().is_empty() || url.password().is_some() || url.port().is_some() {
        return Err("Only Google authorization links can be opened. Reconnect the account.".into());
    }
    Ok(url.to_string())
}
fn comfy_url(value: &str) -> Result<String, String> {
    let url = url::Url::parse(value).map_err(|_| "Invalid owned ComfyUI URL. Reconnect the session.")?;
    if url.scheme() != "http" || url.host_str() != Some("127.0.0.1") || url.port().unwrap_or(0) < 1024 || [Some(18188), Some(18189)].contains(&url.port()) || !url.username().is_empty() || url.password().is_some() || url.path() != "/" || url.query().is_some() || url.fragment().is_some() { return Err("Invalid owned ComfyUI URL. Reconnect the session.".into()); }
    Ok(url.to_string())
}
fn open_browser(url: &str) -> Result<(), String> {
    Command::new("rundll32.exe").args(["url.dll,FileProtocolHandler", url]).spawn().map_err(|_| "Could not open the browser. Reconnect the account.")?;
    Ok(())
}
#[tauri::command]
fn controller_request(app: tauri::AppHandle, window: tauri::WebviewWindow, state: tauri::State<Controller>, request: Value) -> Result<(), String> {
    if window.label() != "main" { return Err("Request blocked. Use the local application.".into()); }
    let origin = window.url().map_err(|_| "Could not verify application origin.")?;
    let packaged = (origin.scheme() == "tauri" && origin.host_str() == Some("localhost")) || (["http", "https"].contains(&origin.scheme()) && origin.host_str() == Some("tauri.localhost") && origin.port().is_none());
    let development = cfg!(debug_assertions) && origin.scheme() == "http" && origin.host_str() == Some("127.0.0.1") && origin.port() == Some(1420);
    if !packaged && !development { return Err("Request blocked. Use the local application.".into()); }
    let action = request.get("action").and_then(Value::as_str).ok_or("Invalid request. Reopen the app.")?;
    let id = request.get("id").and_then(Value::as_str).ok_or("Invalid request ID.")?;
    let params = request.get("params").ok_or("Missing request parameters.")?;
    if !ACTIONS.contains(&action) || !params.is_object() || request.to_string().len() > 256 * 1024 || id.len() > 128 { return Err("Action unavailable. Check the fields and try again.".into()); }
    if action == "shell.openExternal" {
        let url = authorization_url(params.get("url").and_then(Value::as_str).unwrap_or(""))?;
        open_browser(&url)?;
        app.emit("controller-message", json!({"id": id, "ok": true, "result": {}})).map_err(|_| "Could not confirm browser launch.")?;
        return Ok(());
    }
    if action == "shell.close" {
        state.allow_close.store(true, Ordering::SeqCst);
        app.emit("controller-message", json!({"id": id, "ok": true, "result": {}})).map_err(|_| "Could not close the window. Try again.")?;
        window.close().map_err(|_| "Could not close the window. Try again.")?;
        return Ok(());
    }
    if action == "shell.ready" {
        if *state.first_snapshot.lock().map_err(|_| "Controller unavailable.")? {
            if let Some(path) = std::env::var_os("ECC_BENCHMARK_READY_FILE") {
                let now = SystemTime::now().duration_since(UNIX_EPOCH).map_err(|_| "Clock unavailable.")?.as_millis();
                std::fs::write(path, json!({"ready_at": now, "shell": "tauri"}).to_string()).map_err(|_| "Could not save benchmark marker.")?;
            }
        }
        app.emit("controller-message", json!({"id": id, "ok": true, "result": {}})).map_err(|_| "Controller unavailable.")?;
        return Ok(());
    }
    state.actions.lock().map_err(|_| "Controller unavailable.")?.insert(id.into(), action.into());
    let mut pipe = state.stdin.lock().map_err(|_| "The controller is unavailable. Reopen the app.")?;
    let stdin = pipe.as_mut().ok_or("The controller stopped. Reopen the app to reconnect safely.")?;
    writeln!(stdin, "{}", request).and_then(|_| stdin.flush()).map_err(|_| "The controller stopped. Reopen the app to reconnect safely.".to_owned())
}
fn spawn_controller(app: &mut tauri::App) -> Result<(), Box<dyn std::error::Error>> {
    let mut command = if cfg!(debug_assertions) {
        let mut command = Command::new(std::env::var_os("EASY_COMFY_PYTHON").unwrap_or("python".into()));
        command.arg("-u").arg(std::path::Path::new(env!("CARGO_MANIFEST_DIR")).parent().ok_or("Project directory unavailable")?.join("controller/main.py")); command
    } else {
        let executable = std::env::current_exe()?.parent().ok_or("Application directory unavailable")?.join("easy-comfy-controller.exe");
        Command::new(executable)
    };
    #[cfg(windows)] { use std::os::windows::process::CommandExt; command.creation_flags(0x08000000); }
    let mut child = command.stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::null()).spawn()?;
    let stdin = child.stdin.take().ok_or("Controller stdin unavailable")?;
    let stdout = child.stdout.take().ok_or("Controller stdout unavailable")?;
    let first_snapshot = Arc::new(Mutex::new(false));
    let actions = Arc::new(Mutex::new(HashMap::<String, String>::new()));
    app.manage(Controller { stdin: Mutex::new(Some(stdin)), child: Mutex::new(child), first_snapshot: first_snapshot.clone(), actions: actions.clone(), allow_close: AtomicBool::new(false) });
    let handle = app.handle().clone();
    std::thread::spawn(move || {
        for line in BufReader::new(stdout).lines().map_while(Result::ok) {
            if line.len() > 1024 * 1024 { continue; }
            if let Ok(mut value) = serde_json::from_str::<Value>(&line) {
                let action = value.get("id").and_then(Value::as_str).and_then(|id| actions.lock().ok()?.remove(id));
                // A successful snapshot is recognized by its public shape, never saved to the marker.
                if value.get("ok") == Some(&Value::Bool(true)) && value.get("result").and_then(|v| v.get("session")).is_some() {
                    if let Ok(mut ready) = first_snapshot.lock() { *ready = true; }
                }
                if action.as_deref() == Some("session.open") && value.get("ok") == Some(&Value::Bool(true)) {
                    let result = comfy_url(value["result"]["url"].as_str().unwrap_or("")).and_then(|url| open_browser(&url));
                    if let Err(message) = result { value["ok"] = Value::Bool(false); value["error"] = json!({"code": "native_action_failed", "message": message}); }
                }
                if matches!(action.as_deref(), Some("app.diagnostics" | "history.export")) && value.get("ok") == Some(&Value::Bool(true)) {
                    use tauri_plugin_dialog::DialogExt;
                    let filename = if action.as_deref() == Some("history.export") { "easy-comfy-colab-history.json" } else { "easy-comfy-diagnostic.json" };
                    let path = handle.dialog().file().add_filter("JSON", &["json"]).set_file_name(filename).blocking_save_file();
                    if let Some(path) = path {
                        let saved = path.into_path().map_err(|_| ()).and_then(|path| std::fs::write(path, serde_json::to_string_pretty(&value["result"]).unwrap_or_default()).map_err(|_| ()));
                        if saved.is_err() { value["ok"] = Value::Bool(false); value["error"] = json!({"code": "diagnostic_save_failed", "message": "Choose a writable location and export the file again."}); }
                        else { value["result"] = json!({"saved": true}); }
                    } else { value["result"] = json!({"saved": false}); }
                }
                let _ = handle.emit("controller-message", value);
            }
        }
        let _ = handle.emit("controller-message", json!({"event": "controller.unavailable", "data": {"message": "The native controller stopped. Reopen the app to reconnect safely."}}));
    });
    Ok(())
}
fn main() {
    tauri::Builder::default()
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| { if let Some(window) = app.get_webview_window("main") { let _ = window.set_focus(); } }))
        .plugin(tauri_plugin_dialog::init())
        .invoke_handler(tauri::generate_handler![controller_request])
        .on_window_event(|window, event| { if let tauri::WindowEvent::CloseRequested { api, .. } = event { if let Some(state) = window.app_handle().try_state::<Controller>() { if !state.allow_close.load(Ordering::SeqCst) && state.first_snapshot.lock().map(|ready| *ready).unwrap_or(false) { api.prevent_close(); let _ = window.emit("controller-message", json!({"event": "app.close-request", "data": {}})); } } } })
        .setup(spawn_controller)
        .build(tauri::generate_context!()).expect("Could not prepare the app. Reinstall and try again.")
        .run(|app, event| { if let tauri::RunEvent::Exit = event { if let Some(state) = app.try_state::<Controller>() { if let Ok(mut pipe) = state.stdin.lock() { pipe.take(); } if let Ok(mut child) = state.child.lock() { let _ = child.try_wait(); } } } });
}
#[cfg(test)] mod tests {
    use super::*;
    #[test] fn rejects_non_google_and_unsafe_links() {
        assert!(authorization_url("https://accounts.google.com/o/oauth2/auth").is_ok());
        assert!(authorization_url("https://sdk.cloud.google.com/auth").is_ok());
        for value in ["http://accounts.google.com/", "https://accounts.google.com.evil.test/", "https://user:secret@accounts.google.com/", "file:///C:/secret", "https://accounts.google.com:444/"] { assert!(authorization_url(value).is_err()); }
    }
    #[test] fn rejects_legacy_and_remote_comfy_urls() {
        assert!(comfy_url("http://127.0.0.1:28188/").is_ok());
        for value in ["http://127.0.0.1:18188/", "http://127.0.0.1:18189/", "http://evil.test:28188/", "http://127.0.0.1:28188/secret", "http://127.0.0.1:28188/?url=evil"] { assert!(comfy_url(value).is_err()); }
    }
}
