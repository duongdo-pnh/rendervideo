//! Byscom Render desktop shell.
//!
//! This app does NOT render its own UI in the webview — backend/app.py already
//! serves the built frontend (frontend/dist) with SPA fallback at 127.0.0.1:8000.
//! All this shell does is:
//!   1. ensure_source(): extract the bundled Python source tree to
//!      ~/.local/share/byscom-render if missing or older than this build
//!      (venv/, checkpoints/, uploads/, jobs.db etc. are left untouched —
//!      only source .py/.xlsx/config files ship in the bundle).
//!   2. Spawn `venv/bin/python -m backend.run` from that directory. backend/app.py's
//!      own startup hook (backend/workers.py) takes it from there — it spawns
//!      queue_worker.py + tts_worker.py itself.
//!   3. Poll 127.0.0.1:8000 until it answers, then point the window at it.
//!
//! The spawned backend is intentionally NOT tracked/killed on window close —
//! renders already in flight must survive closing the window (see
//! backend/workers.py's docstring). A plain std::process::Command child on Linux
//! is reparented to init and keeps running once this process exits; we never
//! call .kill() on it.
//!
//! That persistence means a later app launch's ensure_source() can refresh the
//! source tree while an old backend.run from a previous launch is still up and
//! serving on BACKEND_PORT — step 3's reconnect check would just find it already
//! answering and never spawn a fresh one. kill_stale_backend() closes that gap:
//! it SIGTERMs a backend.run that predates the refresh (tracked via
//! backend/run.py's own PID file) before the reconnect check runs.

mod install_progress;

use std::net::TcpStream;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::time::{Duration, Instant};

use std::sync::atomic::{AtomicBool, Ordering};
use tauri::Manager;

static STARTING: AtomicBool = AtomicBool::new(false);

const BACKEND_HOST: &str = "127.0.0.1";
const BACKEND_PORT: u16 = 8000;
const BUNDLED_VERSION_MARKER: &str = ".bundled-version";
const BACKEND_PID_FILE: &str = ".backend_run.pid";
const STARTUP_TIMEOUT: Duration = Duration::from_secs(120);

fn install_dir() -> PathBuf {
    // Matches env_path.py / job_files.py's convention for the packaged app.
    dirs::data_dir()
        .expect("no XDG data dir")
        .join("byscom-render")
}

fn log_line(msg: &str) {
    println!("[byscom-render-desktop] {msg}");
}

/// Extract the bundled source tarball over `install_dir` if it's missing or the
/// bundle is newer than what's already installed there. Never touches venv/,
/// checkpoints/, uploads/, jobs.db, or anything else not present in the tarball.
fn ensure_source(app: &tauri::AppHandle, target: &Path) -> Result<(), String> {
    let resource_path = app
        .path()
        .resolve(
            "resources/rendervideo-src.tar.gz",
            tauri::path::BaseDirectory::Resource,
        )
        .map_err(|e| format!("resolve bundled source: {e}"))?;

    let marker = target.join(BUNDLED_VERSION_MARKER);
    let bundled_version = app.package_info().version.to_string();
    let installed_version = std::fs::read_to_string(&marker).unwrap_or_default();

    if installed_version.trim() == bundled_version && target.join("backend").is_dir() {
        log_line(&format!(
            "source up to date (v{bundled_version}), skipping extract"
        ));
        return Ok(());
    }

    log_line(&format!(
        "extracting bundled source v{bundled_version} -> {}",
        target.display()
    ));
    std::fs::create_dir_all(target).map_err(|e| format!("mkdir {}: {e}", target.display()))?;

    let status = Command::new("tar")
        .arg("-xzf")
        .arg(&resource_path)
        .arg("-C")
        .arg(target)
        .status()
        .map_err(|e| format!("spawn tar: {e}"))?;
    if !status.success() {
        return Err(format!("tar extract exited with {status}"));
    }

    std::fs::write(&marker, &bundled_version).map_err(|e| format!("write marker: {e}"))?;
    Ok(())
}

fn spawn_backend(install_dir: &Path) -> Result<(), String> {
    let python = install_dir.join("venv").join("bin").join("python");
    if !python.is_file() {
        return Err(format!(
            "Không tìm thấy {} — hãy chạy install/setup.sh trước khi mở app.",
            python.display()
        ));
    }

    let logs_dir = install_dir.join("logs");
    std::fs::create_dir_all(&logs_dir).map_err(|e| format!("mkdir logs: {e}"))?;
    let log_path = logs_dir.join("backend.log");
    let log_file = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(&log_path)
        .map_err(|e| format!("open {}: {e}", log_path.display()))?;
    let log_file_err = log_file
        .try_clone()
        .map_err(|e| format!("clone log handle: {e}"))?;

    log_line(&format!(
        "spawning backend.run (log: {})",
        log_path.display()
    ));
    Command::new(python)
        .arg("-m")
        .arg("backend.run")
        .current_dir(install_dir)
        .stdout(Stdio::from(log_file))
        .stderr(Stdio::from(log_file_err))
        .stdin(Stdio::null())
        .spawn()
        .map_err(|e| format!("spawn backend.run: {e}"))?;
    // Deliberately not tracking this child: it (and the workers it spawns via
    // backend/workers.py) must keep running after this process exits.
    Ok(())
}

/// If a backend.run from BEFORE the last ensure_source() refresh is still listening
/// on BACKEND_PORT, kill it — otherwise the reconnect check below just finds it
/// already up and never spawns a fresh one, so it keeps serving whatever code was
/// in memory when IT started, forever (closing the window never kills it — see this
/// module's docstring). Mirrors backend/workers.py's _kill_stale_worker(), one level
/// up: that one only re-checks staleness for queue_worker.py/tts_worker.py at
/// backend.run's OWN startup, which never happens if backend.run itself is the
/// stale one still running.
fn kill_stale_backend(target: &Path) {
    let pid_time = match target.join(BACKEND_PID_FILE).metadata().and_then(|m| m.modified()) {
        Ok(t) => t,
        Err(_) => return, // no previous backend recorded — nothing to do
    };
    let marker_time = match target.join(BUNDLED_VERSION_MARKER).metadata().and_then(|m| m.modified()) {
        Ok(t) => t,
        Err(_) => return,
    };
    if marker_time <= pid_time {
        return; // backend started at/after the last source refresh — not stale
    }
    let pid: u32 = match std::fs::read_to_string(target.join(BACKEND_PID_FILE))
        .ok()
        .and_then(|s| s.trim().parse().ok())
    {
        Some(p) => p,
        None => return,
    };
    log_line(&format!(
        "backend.run pid={pid} predates the current source — sending SIGTERM"
    ));
    let _ = Command::new("kill").arg("-TERM").arg(pid.to_string()).status();
    let deadline = Instant::now() + Duration::from_secs(5);
    while Instant::now() < deadline {
        let alive = Command::new("kill")
            .arg("-0")
            .arg(pid.to_string())
            .status()
            .map(|s| s.success())
            .unwrap_or(false);
        if !alive {
            break;
        }
        std::thread::sleep(Duration::from_millis(300));
    }
}

fn wait_for_backend() -> bool {
    let deadline = Instant::now() + STARTUP_TIMEOUT;
    while Instant::now() < deadline {
        if TcpStream::connect((BACKEND_HOST, BACKEND_PORT)).is_ok() {
            return true;
        }
        std::thread::sleep(Duration::from_millis(300));
    }
    false
}

#[tauri::command]
fn get_install_progress() -> Result<install_progress::InstallProgress, String> {
    install_progress::snapshot(&install_dir())
}

/// Invoked after the loading page is ready, so first-run status cannot be lost.
#[tauri::command]
async fn start_environment(app: tauri::AppHandle, install: bool) -> Result<String, String> {
    if STARTING.swap(true, Ordering::SeqCst) {
        return Err("Đang cài đặt hoặc khởi động, vui lòng chờ.".into());
    }
    let result = tauri::async_runtime::spawn_blocking(move || {
        let target = install_dir();
        if install {
            std::fs::create_dir_all(target.join("logs")).map_err(|e| e.to_string())?;
            std::fs::write(
                target.join("logs/setup-progress"),
                "0|Đang chuẩn bị bộ cài…",
            )
            .map_err(|e| e.to_string())?;
            std::fs::write(target.join("logs/setup.log"), "").map_err(|e| e.to_string())?;
        }
        ensure_source(&app, &target)?;
        kill_stale_backend(&target);
        let incomplete = target.join(".setup-in-progress");
        let missing = incomplete.exists()
            || !Command::new("python3")
                .arg("install/environment.py")
                .current_dir(&target)
                .status()
                .map(|status| status.success())
                .unwrap_or(false);
        if missing && !install {
            return Ok("missing".into());
        }
        if install {
            std::fs::write(&incomplete, "installing").map_err(|e| e.to_string())?;
            let logs = target.join("logs");
            std::fs::create_dir_all(&logs).map_err(|e| e.to_string())?;
            let log_path = logs.join("setup.log");
            let log = std::fs::File::create(&log_path).map_err(|e| e.to_string())?;
            let err_log = log.try_clone().map_err(|e| e.to_string())?;
            let status = Command::new("bash")
                .arg("install/setup.sh")
                .current_dir(&target)
                .env("PYTHONUNBUFFERED", "1")
                .stdin(Stdio::null())
                .stdout(Stdio::from(log))
                .stderr(Stdio::from(err_log))
                .status()
                .map_err(|e| format!("Không chạy được bộ cài: {e}"))?;
            if status.code() == Some(20) {
                return Ok("reboot".into());
            }
            if !status.success() {
                let output = std::fs::read_to_string(&log_path).unwrap_or_default();
                let tail: Vec<_> = output.lines().rev().take(15).collect();
                return Err(format!(
                    "Cài môi trường thất bại. Chi tiết: {}\n{}",
                    log_path.display(),
                    tail.into_iter().rev().collect::<Vec<_>>().join("\n")
                ));
            }
            if incomplete.exists() {
                std::fs::remove_file(&incomplete).map_err(|e| e.to_string())?;
            }
        }
        if TcpStream::connect((BACKEND_HOST, BACKEND_PORT)).is_err() {
            spawn_backend(&target)?;
        }
        if !wait_for_backend() {
            return Err(format!(
                "Backend không phản hồi sau 120s. Xem {}",
                target.join("logs/backend.log").display()
            ));
        }
        if let Some(window) = app.get_webview_window("main") {
            window
                .navigate(
                    format!("http://{BACKEND_HOST}:{BACKEND_PORT}/")
                        .parse()
                        .unwrap(),
                )
                .map_err(|e| e.to_string())?;
        }
        Ok("ready".into())
    })
    .await
    .map_err(|e| e.to_string())
    .and_then(|result| result);
    STARTING.store(false, Ordering::SeqCst);
    result
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .invoke_handler(tauri::generate_handler![
            start_environment,
            get_install_progress
        ])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
