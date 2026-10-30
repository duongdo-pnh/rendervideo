"""Auto-starts the background daemons the desktop app needs, so the packaged
app is genuinely zero-terminal-steps: queue_worker.py (render queue),
tts_worker.py (TTS queue), and akali_bridge.py (akali queue bridge — see
doc_api/APi_RENDER_QUEUE.md). akali_bridge.py no-ops immediately if
AKALI_URL/AKALI_WORKER_KEY aren't set (see its bridge_enabled()), so starting
it unconditionally is fine even before the "Kết nối akali" tab is configured.

musetalk_render_server.py is deliberately NOT started here — it already
auto-starts itself, lazily, the first time a render job actually needs it
(see musetalk_adapter.py's _ensure_server(), which has its own cross-process
lock). Duplicating that here would pin ~5.5GB of VRAM before it's needed.

All three scripts already refuse to run a second instance (flock-based lock,
see their own _acquire_worker_lock()) — so spawning them here is always safe
to attempt, whether or not one is already running (manually, or from a
previous launch of this app): the duplicate just detects the lock and exits
within milliseconds.

Each subprocess gets an EXPLICIT, minimal environment built from scratch
rather than inheriting this process's via `{**os.environ}`. That blind-
inheritance pattern is exactly what let a stray env var meant for the main
venv leak into a MuseTalk subprocess and silently crash ~35 production
render jobs overnight — building the env from a short allowlist here means
whatever Tauri/the shell happens to export on THIS process never reaches
these workers unless it's explicitly named below.

Workers are intentionally fire-and-forget: once spawned they keep running
even after this backend process exits (they're meant to run 24/7, independent
of whether the desktop window happens to be open — closing the app should not
abort an in-progress render). start_new_session=True detaches them from this
process's session so they never receive a signal sent to it.

That persistence has a cost: a worker holds whatever code was in memory the
moment it started, forever — Python never hot-reloads an already-imported
module. After ensure_source() refreshes the source tree for an app update,
an already-running worker keeps executing the OLD code indefinitely; the
naive "spawn if the lock is free" check above just detects the lock is
already held and backs off, leaving the stale process in charge. See
_kill_stale_worker() below.
"""
import os
import signal
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MAIN_PYTHON = ROOT / "venv" / "bin" / "python"
MUSETALK_PYTHON = ROOT / "engines" / "MuseTalk" / ".venv" / "bin" / "python"
LOGS_DIR = ROOT / "logs"

_PASSTHROUGH_ENV_VARS = ("HOME", "LANG", "LC_ALL", "TZ", "USER")

_WORKER_SCRIPTS = ("queue_worker.py", "tts_worker.py", "akali_bridge.py")


def _clean_env() -> dict:
    env = {k: os.environ[k] for k in _PASSTHROUGH_ENV_VARS if k in os.environ}
    env["PATH"] = f"{MAIN_PYTHON.parent}:{os.environ.get('PATH', '/usr/bin:/bin')}"
    return env


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _kill_stale_worker(script: str) -> None:
    """If `script`'s currently-running instance (flock PID recorded in its
    lock file, written by its own _acquire_worker_lock()) acquired that lock
    before the app's source was last refreshed, it's running code from
    before the update — SIGTERM it so the spawn below can take over the now-
    free lock with fresh code.

    Staleness is judged against .bundled-version (written by the Tauri
    shell's ensure_source() on every source refresh — see desktop/src-tauri/
    src/lib.rs), NOT script's own mtime: tar preserves original mtimes on
    extract, so an unmodified file like queue_worker.py keeps an old mtime
    even in a freshly-extracted tree, while the actual change was in a
    module it imports (google_drive_upload.py, say) — comparing the whole
    tree's last-refreshed marker instead of one file catches that case. In a
    dev checkout (no .bundled-version, never extracted from a bundle) this
    is always a no-op, which is correct — nothing to compare against.
    """
    lock_path = ROOT / f".{script[:-3]}.lock"
    version_marker = ROOT / ".bundled-version"
    try:
        pid = int(lock_path.read_text().strip())
        stale = version_marker.stat().st_mtime > lock_path.stat().st_mtime
    except (FileNotFoundError, ValueError, OSError):
        return
    if not stale or not _pid_alive(pid):
        return
    print(f"[workers] {script} pid={pid} predates the current source — sending SIGTERM")
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    for _ in range(10):
        time.sleep(0.5)
        if not _pid_alive(pid):
            break


def _spawn(script: str) -> None:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOGS_DIR / f"{script[:-3]}.log"
    log = open(log_path, "a", buffering=1)
    subprocess.Popen(
        [str(MAIN_PYTHON), "-u", script],
        cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
        env=_clean_env(), start_new_session=True, close_fds=True,
    )
    print(f"[workers] spawned {script} (log: {log_path})")


def _ensure_pkg_resources(python: Path) -> None:
    """Pin setuptools<81 in `python`'s venv if pkg_resources is missing.

    install/setup.sh pins this at install time (setuptools>=81 dropped pkg_resources,
    which librosa imports, so every LatentSync render dies with "No module named
    'pkg_resources'"), but that pin only landed on 2026-10-12. Venvs created before
    then still carry setuptools>=81, and a .deb upgrade only refreshes the source —
    it never re-runs setup.sh — so those installs stay broken. Repair them here, once:
    after the pin, the import check passes and this is a ~1s no-op on every start.
    """
    if not python.is_file():
        return
    try:
        check = subprocess.run([str(python), "-c", "import pkg_resources"],
                               capture_output=True, env=_clean_env(), timeout=60)
    except (OSError, subprocess.TimeoutExpired) as e:
        print(f"[workers] pkg_resources check failed for {python}: {e}")
        return
    if check.returncode == 0:
        return
    print(f"[workers] pkg_resources missing in {python} — installing setuptools<81")
    try:
        fix = subprocess.run([str(python), "-m", "pip", "install", "setuptools<81"],
                             capture_output=True, text=True, env=_clean_env(), timeout=300)
    except (OSError, subprocess.TimeoutExpired) as e:
        print(f"[workers] setuptools<81 install failed for {python}: {e}")
        return
    if fix.returncode == 0:
        print(f"[workers] setuptools<81 installed in {python}")
    else:
        print(f"[workers] setuptools<81 install failed for {python} "
              f"(exit {fix.returncode}): {fix.stderr.strip()[-500:]}")


def start_all() -> None:
    if not MAIN_PYTHON.is_file():
        print(f"[workers] {MAIN_PYTHON} not found — skipping auto-start (not installed?)")
        return

    # Before any worker can start a render on a venv that can't import librosa.
    for python in (MAIN_PYTHON, MUSETALK_PYTHON):
        _ensure_pkg_resources(python)

    for script in _WORKER_SCRIPTS:
        _kill_stale_worker(script)
        _spawn(script)

    # An update ships fun5b/ but not its venvs/models: install them in the background when
    # this machine can run Fun-5B (no-op once installed, or on GPUs under ~20 GB).
    import fun5b_setup
    fun5b_setup.auto_start()


def spawn_akali_bridge() -> None:
    """Gọi từ backend/akali.py ngay sau khi lưu AKALI_URL/AKALI_WORKER_KEY thành
    công, để không bắt người dùng đóng/mở lại app mới thấy bridge chạy — trước
    bản vá này, akali_bridge.py tự thoát ngay lúc backend khởi động nếu .env
    lúc đó còn trống, và không có gì khởi động lại nó khi config được lưu sau
    đó (xem doc_api/APi_RENDER_QUEUE.md mục 10).

    An toàn gọi bất cứ lúc nào, kể cả khi bridge đã chạy — flock trong
    akali_bridge._acquire_worker_lock() tự phát hiện và thoát ngay nếu trùng,
    giống hệt logic start_all() ở trên. Không gọi _kill_stale_worker() ở đây:
    đây không phải lúc source vừa được refresh, chỉ đơn giản là "đảm bảo nó
    đang chạy" với đúng .env vừa lưu.
    """
    if not MAIN_PYTHON.is_file():
        return
    _spawn("akali_bridge.py")
