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

echo "=== [1/6] Detecting GPU and resolving compatibility profile ==="
if ! PROFILE_OUT=$(python3 install/resolve_profile.py); then
    echo
    echo "Cannot continue without a validated profile for this GPU. See message above."
    exit 1
fi
eval "$PROFILE_OUT"
echo "GPU: $GPU_NAME ($SM_ARCH)"
echo "main venv     -> torch==$MAIN_TORCH torchvision==$MAIN_TORCHVISION ($MAIN_INDEX_URL)"
echo "musetalk venv -> torch==$MUSETALK_TORCH torchvision==$MUSETALK_TORCHVISION torchaudio==$MUSETALK_TORCHAUDIO ($MUSETALK_INDEX_URL)"

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
        echo "No terminal and no pkexec available to run: $*" >&2
        return 1
    fi
    echo >&2
    echo "Could not run automatically: $*" >&2
    echo "Open a terminal, run this yourself, then re-run setup:" >&2
    echo "  sudo $*" >&2
    return 1
}

echo
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
echo "=== [3/6] System packages (OpenCV runtime dep + build toolchain) ==="
# Check first rather than always invoking sudo — most of these are already
# present on a machine with any prior dev history.
_missing_pkgs=""
command -v ffmpeg >/dev/null 2>&1 || _missing_pkgs="$_missing_pkgs ffmpeg"
ldconfig -p 2>/dev/null | grep -q 'libGL\.so\.1' || _missing_pkgs="$_missing_pkgs libgl1"
# [6/6]'s engines/MuseTalk/download_weights.sh shells out to curl directly —
# near-universal on Ubuntu, but not guaranteed on a minimal/container base.
command -v curl >/dev/null 2>&1 || _missing_pkgs="$_missing_pkgs curl"
# insightface and friends compile a C/C++ extension during `pip install` — a
# genuinely fresh machine (unlike this dev box, which has this from unrelated
# history) may not have a compiler at all.
command -v g++ >/dev/null 2>&1 || _missing_pkgs="$_missing_pkgs build-essential"
command -v $PYTHON >/dev/null 2>&1 || _missing_pkgs="$_missing_pkgs python3.10"
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
fi

echo
echo "=== [4/6] Main venv (LatentSync) — $ROOT/venv ==="
$PYTHON -m venv venv --upgrade-deps
venv/bin/pip install "torch==$MAIN_TORCH" "torchvision==$MAIN_TORCHVISION" --index-url "$MAIN_INDEX_URL"
# requirements.txt also pins torch/torchvision/index-url itself (for the one GPU this
# repo has always run on) — strip those 3 lines, already satisfied above from the
# resolved profile, and install the rest normally.
grep -vE '^(torch==|torchvision==|--extra-index-url)' requirements.txt > /tmp/rendervideo_main_reqs.txt
venv/bin/pip install -r /tmp/rendervideo_main_reqs.txt
rm -f /tmp/rendervideo_main_reqs.txt

echo
echo "=== [5/6] MuseTalk venv — $ROOT/engines/MuseTalk/.venv ==="
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
engines/MuseTalk/.venv/bin/pip install --no-build-isolation chumpy
engines/MuseTalk/.venv/bin/mim install mmengine
engines/MuseTalk/.venv/bin/mim install "mmcv==2.0.1"
engines/MuseTalk/.venv/bin/mim install "mmdet==3.1.0"
engines/MuseTalk/.venv/bin/mim install "mmpose==1.1.0"

echo
echo "=== [6/6] Checkpoints ==="
# NOT `-U` — requirements.txt already pins huggingface_hub==0.36.0 (needed for
# tokenizers/transformers compat, both requiring huggingface-hub<1.0). `-U`
# force-upgrades past that pin to whatever's newest, and huggingface_hub>=1.0
# has fully removed the `huggingface-cli` command (renamed to `hf`) — every
# `huggingface-cli download` call below fails immediately after.
venv/bin/pip install "huggingface_hub[cli]==0.36.0"
venv/bin/huggingface-cli download ByteDance/LatentSync-1.6 whisper/tiny.pt --local-dir checkpoints
venv/bin/huggingface-cli download ByteDance/LatentSync-1.6 latentsync_unet.pt --local-dir checkpoints
# engines/MuseTalk/download_weights.sh expects `pip`/`huggingface-cli` on PATH to
# resolve to the venv it should install into — prepend the MuseTalk venv's bin/.
(cd engines/MuseTalk && PATH="$ROOT/engines/MuseTalk/.venv/bin:$PATH" bash download_weights.sh)

echo
echo "=== Smoke-testing both venvs ==="
venv/bin/python install/smoke_test.py --label main --expect-torch "$MAIN_TORCH"
engines/MuseTalk/.venv/bin/python install/smoke_test.py --label musetalk --expect-torch "$MUSETALK_TORCH"

echo
echo "Setup complete for $GPU_NAME ($SM_ARCH)."
