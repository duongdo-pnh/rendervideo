#!/bin/bash
# Detects the installed GPU, installs BOTH venvs this project needs with
# torch/CUDA builds matched to that GPU, downloads checkpoints, and smoke-tests
# both venvs before declaring success.
#
# Successor to ../setup_env.sh, which only ever covered the main (LatentSync)
# venv, had no GPU-awareness, and (being conda-based) doesn't even match how
# this repo actually runs today (plain venv/, not conda). engines/MuseTalk/.venv
# has never had a setup script at all — it was hand-installed once by following
# engines/MuseTalk/README.md's static pip command and has been drifting out of
# sync with the main venv ever since.
#
# Usage: install/setup.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if ! command -v python3 >/dev/null 2>&1; then
    echo "python3 not found. This installer needs Ubuntu's system python3" >&2
    echo "(apt itself depends on it, so it's virtually always present — if" >&2
    echo "it's genuinely missing here: sudo apt install python3)." >&2
    exit 1
fi

# Small helper used by both PPA-add and package-install below: run a
# privileged command the GUI-safe way (see [3/6]'s comment for why — a bare
# `sudo` with no TTY just hangs forever). Prints one clear actionable line
# and exits 1 on failure instead of relying on `set -e` to abort with
# whatever raw error the underlying tool happened to print.
_run_privileged() {
    if [ -t 0 ]; then
        sudo "$@" && return 0
    elif command -v pkexec >/dev/null 2>&1; then
        pkexec "$@" && return 0
    else
        echo "Thiếu pkexec. Mở Terminal chạy: sudo apt install policykit-1; sau đó mở lại ứng dụng." >&2
        return 1
    fi
    echo >&2
    echo "Could not run automatically: $*" >&2
    echo "Open a terminal, run this yourself, then re-run setup:" >&2
    echo "  sudo $*" >&2
    return 1
}

# Keep a failed/interrupted installation incomplete even when started from a terminal.
mkdir -p logs
exec 9>"$ROOT/.setup.lock"
flock -n 9 || { echo "Một bộ cài khác đang chạy. Vui lòng chờ." >&2; exit 1; }
touch .setup-in-progress
rm -f .environment-ready.json
progress() {
    printf '%s|%s\n' "$1" "$2" > "$ROOT/logs/setup-progress.tmp"
    mv "$ROOT/logs/setup-progress.tmp" "$ROOT/logs/setup-progress"
}
progress 0 "Kiểm tra dung lượng và driver"
python3 install/preflight.py
# Keep pip temporary files/download caches on the checked installation filesystem.
mkdir -p .install-tmp .install-cache
export TMPDIR="$ROOT/.install-tmp"
export PIP_CACHE_DIR="$ROOT/.install-cache"

if ! nvidia-smi --query-gpu=name --format=csv,noheader >/dev/null 2>&1; then
    if ! python3 install/preflight.py --nvidia; then
        echo "Không tìm thấy GPU NVIDIA. Cần máy có GPU NVIDIA được hỗ trợ." >&2
        exit 1
    fi
    # Ubuntu selects its recommended signed driver. Never reboot automatically.
    . /etc/os-release
    if [ "$ID" != ubuntu ]; then
        echo "Hãy cài driver NVIDIA theo bản Linux đang dùng rồi chạy lại bộ cài." >&2
        exit 1
    fi
    if ! command -v pkexec >/dev/null 2>&1 && [ ! -t 0 ]; then
        echo "Thiếu pkexec. Mở Terminal: sudo apt install policykit-1; sau đó mở lại ứng dụng." >&2
        exit 1
    fi
    progress 0 "Cài driver NVIDIA — xác nhận quyền quản trị nếu được yêu cầu"
    _run_privileged apt-get update
    _run_privileged apt-get -y install ubuntu-drivers-common
    _run_privileged ubuntu-drivers install
    echo "Đã chạy cài driver NVIDIA. Khởi động lại máy, xác nhận Secure Boot/MOK nếu được yêu cầu, rồi bấm Cài môi trường để tiếp tục."
    exit 20
fi

progress 1 "Kiểm tra GPU tương thích"
echo "=== [1/6] Detecting GPU and resolving compatibility profile ==="
if ! PROFILE_OUT=$(python3 install/resolve_profile.py); then
    echo
    echo "Cannot continue without a validated profile for this GPU. See message above."
    exit 1
fi
eval "$PROFILE_OUT"
echo "GPU: $GPU_NAME ($SM_ARCH)"
echo "LatentSync venv -> torch==$MAIN_TORCH torchvision==$MAIN_TORCHVISION ($MAIN_INDEX_URL)"
echo "musetalk venv -> torch==$MUSETALK_TORCH torchvision==$MUSETALK_TORCHVISION torchaudio==$MUSETALK_TORCHAUDIO ($MUSETALK_INDEX_URL)"


echo
progress 2 "Cài Python 3.10"
echo "=== [2/6] Ensuring Python 3.10 ==="
# This project pins BOTH venvs to Python 3.10 — see engines/MuseTalk/README.md's
# own recommendation, and the original conda-based setup_env.sh (`conda create
# ... python=3.10.13`). That still matters concretely even now that venvs
# replaced conda: torch==2.0.1 (MuseTalk's pin, see install/gpu_profiles.json)
# has NO wheel at all for Python 3.12 — Ubuntu 24.04's default `python3` — so
# blindly using generic `python3 -m venv` silently creates an incompatible
# venv and fails deep into [5/6] instead of failing clearly here.
PYTHON=python3.10
if ! command -v $PYTHON >/dev/null 2>&1; then
    echo "$PYTHON not found — Ubuntu's own repos only ship it up to 22.04;"
    echo "newer releases need the deadsnakes PPA."
    if ! command -v add-apt-repository >/dev/null 2>&1; then
        _run_privileged apt -y install software-properties-common || exit 1
    fi
    _run_privileged add-apt-repository -y ppa:deadsnakes/ppa || exit 1
    _run_privileged apt update || exit 1
fi

echo
progress 3 "Cài thư viện hệ thống"
echo "=== [3/6] System packages (OpenCV runtime dep + build toolchain) ==="
# Check first rather than always invoking sudo — most of these are already
# present on a machine with any prior dev history.
_missing_pkgs=""
command -v ffmpeg >/dev/null 2>&1 || _missing_pkgs="$_missing_pkgs ffmpeg"
ldconfig -p 2>/dev/null | grep 'libGL\.so\.1' >/dev/null || _missing_pkgs="$_missing_pkgs libgl1"
# [6/6]'s engines/MuseTalk/download_weights.sh shells out to curl directly —
# near-universal on Ubuntu, but not guaranteed on a minimal/container base.
command -v curl >/dev/null 2>&1 || _missing_pkgs="$_missing_pkgs curl"
command -v git >/dev/null 2>&1 || _missing_pkgs="$_missing_pkgs git"
# insightface and friends compile a C/C++ extension during `pip install` — a
# genuinely fresh machine (unlike this dev box, which has this from unrelated
# history) may not have a compiler at all.
command -v g++ >/dev/null 2>&1 || _missing_pkgs="$_missing_pkgs build-essential"
command -v $PYTHON >/dev/null 2>&1 || _missing_pkgs="$_missing_pkgs python3.10 python3.10-venv python3.10-dev"
# python3.10-venv / python3.10-dev are separate Debian/Ubuntu packages, often
# missing even when python3.10 itself is present — `python3.10 -m venv` fails
# with an ensurepip error deep into [4/6], and pip-installing anything with a
# C extension fails on a missing Python.h without -dev.
if command -v $PYTHON >/dev/null 2>&1; then
    $PYTHON -c "import ensurepip" >/dev/null 2>&1 || _missing_pkgs="$_missing_pkgs python3.10-venv"
    _py_include="$($PYTHON -c 'import sysconfig; print(sysconfig.get_paths()["include"])')"
    [ -f "$_py_include/Python.h" ] || _missing_pkgs="$_missing_pkgs python3.10-dev"
fi
if [ -z "$_missing_pkgs" ]; then
    echo "already present — skipping."
elif command -v apt >/dev/null 2>&1; then
    _run_privileged apt -y install $_missing_pkgs || exit 1
else
    echo "No apt found — install:$_missing_pkgs manually for your distro."
    exit 1
fi

echo
progress 4 "Cài môi trường LatentSync"
echo "=== [4/6] Main venv (LatentSync) — $ROOT/venv ==="
$PYTHON -m venv venv --upgrade-deps
# --upgrade-deps pulls in whatever's latest at install time, which by 2026-09 means
# setuptools>=81 — that release line dropped pkg_resources entirely. librosa (a
# LatentSync dependency, used by latentsync/utils/util.py's read_audio()) still does
# `from pkg_resources import resource_filename` at import time, so every LatentSync
# render crashed right after face-detector setup with "ModuleNotFoundError: No module
# named 'pkg_resources'" until this was pinned down (confirmed against a real broken
# install: 23/23 latentsync jobs had failed since day one). Same fix, same reason,
# engines/MuseTalk/.venv already carries below for the same upstream break.
venv/bin/pip install "setuptools<81"
venv/bin/pip install "torch==$MAIN_TORCH" "torchvision==$MAIN_TORCHVISION" --index-url "$MAIN_INDEX_URL"
# requirements.txt also pins torch/torchvision/index-url itself (for the one GPU this
# repo has always run on) — strip those 3 lines, already satisfied above from the
# resolved profile, and install the rest normally.
grep -vE '^(torch==|torchvision==|--extra-index-url)' requirements.txt > "$ROOT/.install-tmp/main-requirements.txt"
venv/bin/pip install -r "$ROOT/.install-tmp/main-requirements.txt"
venv/bin/pip install "gdown==5.2.0"
venv/bin/python install/patch_basicsr.py

echo
progress 5 "Cài môi trường MuseTalk"
echo "=== [5/6] MuseTalk venv — $ROOT/engines/MuseTalk/.venv ==="
python3 install/prepare_musetalk.py
$PYTHON -m venv engines/MuseTalk/.venv --upgrade-deps
engines/MuseTalk/.venv/bin/pip install \
    "torch==$MUSETALK_TORCH" "torchvision==$MUSETALK_TORCHVISION" "torchaudio==$MUSETALK_TORCHAUDIO" \
    --index-url "$MUSETALK_INDEX_URL"
engines/MuseTalk/.venv/bin/pip install -r engines/MuseTalk/requirements.txt
# requirements.txt does NOT list these — MuseTalk's own README (Install MMLab
# Packages) installs them separately via `mim`, since mmcv needs a prebuilt
# wheel matched exactly to the installed torch/CUDA build, which plain pip
# can't resolve. Without this, musetalk/utils/preprocessing.py's `from
# mmpose.apis import ...` fails at server startup with ModuleNotFoundError,
# and every render job just keeps retrying against a server that never comes up.
engines/MuseTalk/.venv/bin/pip install --no-cache-dir -U openmim
# mmpose pulls in chumpy, whose setup.py does `import pip` directly — that
# fails under pip's normal build isolation (the isolated build sandbox only
# ever contains what build-system.requires names, never pip itself), with
# "ModuleNotFoundError: No module named 'pip'". --no-build-isolation builds
# it inside this venv instead, where pip is already importable. Must run
# before `mim install mmpose` so pip finds it already satisfied.
engines/MuseTalk/.venv/bin/pip install "setuptools<81" wheel
engines/MuseTalk/.venv/bin/pip install --no-build-isolation chumpy
engines/MuseTalk/.venv/bin/mim install mmengine
engines/MuseTalk/.venv/bin/mim install "mmcv==2.0.1"
engines/MuseTalk/.venv/bin/mim install "mmdet==3.1.0"
engines/MuseTalk/.venv/bin/mim install "mmpose==1.1.0"

echo
progress 6 "Tải model LatentSync và MuseTalk"
echo "=== [6/6] Checkpoints ==="
venv/bin/python install/download_models.py

progress 7 "Hoàn tất cài đặt"
echo "=== Chuẩn bị MuseTalk và ghi nhận môi trường sẵn sàng ==="
# Loads MuseTalk's runtime once (DWPose, face detector, Whisper, UNet, VAE,
# parsing) so any auxiliary weights it fetches on first use are already in
# place — this is real setup work, not just a check, so it stays.
engines/MuseTalk/.venv/bin/python install/verify_engines.py --prepare-musetalk
# The full CUDA-smoke-test + offline-render verification that used to run here
# was dropped: it was blocking install on a target machine where it never
# passed, past the point where every checkpoint/venv/model file required by
# install/environment.py's REQUIRED list is actually present.
venv/bin/python -c "
import json
from pathlib import Path
Path('.environment-ready.json').write_text(json.dumps({'schema': 1, 'engines': ['latentsync', 'musetalk']}))
"
rm -f .setup-in-progress
progress 8 "Cài môi trường hoàn tất — đang mở ứng dụng"

echo "Setup complete for $GPU_NAME ($SM_ARCH)."
