"""Automatic install of the optional Fun-5B engine on machines that got its code through an update.

An app update ships fun5b/ but not its ~57GB of models and two venvs (bundle-source.sh never
carries engines/ or checkpoints/). backend/workers.start_all() calls auto_start() on every backend
start-up: when the engine is missing and this machine can run it, install/setup_fun5b.sh starts in
the background (detached, survives the window closing, resumes on the next start if interrupted).
The UI reads status(); POST /api/engines/fun5b/install calls start() to retry by hand.

Also the setup script's own checks:  venv/bin/python fun5b_setup.py preflight|verify
"""
import fcntl
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import fun5b_adapter

ROOT = Path(__file__).resolve().parent
SCRIPT = ROOT / "install" / "setup_fun5b.sh"
LOCK = ROOT / ".fun5b-setup.lock"
LOG = ROOT / "logs" / "fun5b-setup.log"
PROGRESS = ROOT / "logs" / "fun5b-setup-progress"
RESULT = ROOT / "logs" / "fun5b-setup-result"
MODELS_BYTES = 57.3e9  # checkpoints/fun5b of the validated install
VENVS_BYTES = 9.5e9  # engines/Fun5B/.venv (8.5GB) + .venv-edit (0.3GB) + VideoX-Fun
WORK_BYTES = 8e9  # uv cache + temp files while installing (deleted at the end)
REQUIRED_TOOLS = ("git", "curl", "tar", "md5sum", "gcc")  # gcc: Triton compiles its launchers


def installed():
    """Engine and FireRed both complete (the setup script's goal)."""
    return not fun5b_adapter.missing_files() and fun5b_adapter.plate_available()


def running():
    """The setup script holds LOCK for its whole run."""
    try:
        with open(LOCK, "a+") as f:
            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
            return False
    except OSError:
        return False


def _dir_bytes(path):
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.lstat(os.path.join(root, name)).st_size
            except OSError:
                pass
    return total


def models_bytes():
    ckpt = fun5b_adapter.CKPT_DIR
    return _dir_bytes(ckpt.resolve()) if ckpt.exists() else 0


def bytes_needed():
    """Free disk the remaining install needs (what is already there is not counted again)."""
    need = max(0.0, MODELS_BYTES - models_bytes()) + WORK_BYTES
    if not fun5b_adapter.PYTHON.is_file():
        need += VENVS_BYTES
    return need


def preflight():
    """(code, reason): 0 = go, 3 = this machine cannot run Fun-5B (do not install), 1 = fix and retry."""
    import gpu_info

    total = gpu_info.gpu_total_gib()
    if total <= 0:
        return 3, "Không tìm thấy GPU NVIDIA — Fun-5B không chạy được trên máy này."
    if total < fun5b_adapter.MIN_VRAM_GIB:
        name = gpu_info.current_gpu_name() or "GPU"
        return 3, f"{name} chỉ có {total:.0f} GB VRAM, Fun-5B cần ~{fun5b_adapter.MIN_VRAM_GIB} GB — bỏ qua cài đặt."
    missing = [t for t in REQUIRED_TOOLS if not shutil.which(t)]
    if missing:
        return 1, f"Thiếu công cụ hệ thống: {', '.join(missing)} (sudo apt install git curl build-essential)."
    free = shutil.disk_usage(ROOT).free
    need = bytes_needed()
    if free < need:
        return 1, f"Không đủ dung lượng ổ đĩa: còn {free / 1e9:.0f} GB, cần ~{need / 1e9:.0f} GB cho Fun-5B."
    return 0, f"GPU {total:.0f} GB, ổ đĩa còn {free / 1e9:.0f} GB (cần ~{need / 1e9:.0f} GB)."


def _read(path):
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def status():
    """What the UI shows: state is installed | running | failed | skipped | not_installed."""
    step, total, label = 0, 7, ""
    parts = _read(PROGRESS).split("|", 2)
    if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit():
        step, total, label = int(parts[0]), int(parts[1]), parts[2]
    result, _, message = _read(RESULT).partition("|")
    if running():
        state = "running"
    elif installed():
        state, message = "installed", ""
    elif result in ("failed", "skipped"):
        state = result
    else:
        state = "not_installed"
    info = {"state": state, "step": step, "total": total, "label": label, "message": message or None,
            "downloaded_gb": None, "models_gb": round(MODELS_BYTES / 1e9, 1)}
    if state == "running" and step >= 6:  # the model download: show how far it got
        info["downloaded_gb"] = round(models_bytes() / 1e9, 1)
    return info


def _env():
    env = {k: os.environ[k] for k in ("HOME", "LANG", "LC_ALL", "TZ", "USER") if k in os.environ}
    env["PATH"] = f"{ROOT / 'venv' / 'bin'}:{os.environ.get('PATH', '/usr/bin:/bin')}"
    return env


def start():
    """Start install/setup_fun5b.sh detached (no-op while it already runs). Returns status()."""
    if not running():
        LOG.parent.mkdir(parents=True, exist_ok=True)
        RESULT.unlink(missing_ok=True)
        with open(LOG, "a") as log:
            log.write(f"\n===== Fun-5B setup started {time.strftime('%F %T')} =====\n")
            log.flush()
            subprocess.Popen(["bash", str(SCRIPT)], cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
                             stdin=subprocess.DEVNULL, start_new_session=True, close_fds=True, env=_env())
        for _ in range(20):  # until the script took its lock, so status() says "running"
            if running():
                break
            time.sleep(0.1)
    return status()


def auto_start():
    """Backend start-up hook: install Fun-5B in the background when it is missing and this machine
    can run it. Never raises: a failed check must not stop the app from starting."""
    try:
        if installed() or running():
            return
        code, reason = preflight()
        if code == 3:  # never going to run here: remember why, do not download 57GB
            RESULT.parent.mkdir(parents=True, exist_ok=True)
            RESULT.write_text(f"skipped|{reason}\n", encoding="utf-8")
            return
        print(f"[fun5b-setup] engine missing — installing in the background ({reason})", flush=True)
        start()
    except Exception as e:  # noqa: BLE001
        print(f"[fun5b-setup] auto start failed: {e}", flush=True)


def verify():
    """Setup script's final check: every file the adapter needs, FireRed included."""
    missing = fun5b_adapter.missing_files()
    if not fun5b_adapter.plate_available():
        missing.append("FireRed (checkpoints/fun5b/firered-image-edit, engines/Fun5B/.venv-edit)")
    if missing:
        print("Fun-5B vẫn thiếu:\n  - " + "\n  - ".join(missing))
        return 1
    print("Fun-5B: đủ file.")
    return 0


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "preflight":
        code, reason = preflight()
        print(reason)
        sys.exit(code)
    if cmd == "verify":
        sys.exit(verify())
    print(__doc__)
    sys.exit(2)
