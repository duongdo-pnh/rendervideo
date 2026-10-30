#!/usr/bin/env bash
# Installs the optional Fun-5B engine on this machine: VideoX-Fun (pinned), engines/Fun5B/.venv and
# .venv-edit (exact package sets from install/fun5b-*requirements.lock) and ~57GB of models.
#
# Started in the background by the app itself (fun5b_setup.auto_start(), at backend start-up) when
# an update brought the Fun-5B code but the machine has no engine yet; also runnable by hand:
#   install/setup_fun5b.sh
# Re-runnable: finished parts are skipped, interrupted model downloads resume. Every venv /
# checkout is built under a *.partial name and renamed only once complete, so the app never sees
# a half-installed engine (fun5b_adapter.missing_files() decides availability). No sudo: missing
# system tools are reported, not installed (install/setup.sh owns apt).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
mkdir -p logs
exec 9>"$ROOT/.fun5b-setup.lock"
flock -n 9 || { echo "Fun-5B setup is already running."; exit 0; }

PROGRESS="$ROOT/logs/fun5b-setup-progress"
RESULT="$ROOT/logs/fun5b-setup-result"
TOTAL=7
LABEL="Bắt đầu"
rm -f "$RESULT"
progress() {
    LABEL="$2"
    printf '%s|%s|%s\n' "$1" "$TOTAL" "$2" > "$PROGRESS.tmp"
    mv "$PROGRESS.tmp" "$PROGRESS"
    echo
    echo "=== [$1/$TOTAL] $2 ($(date '+%F %T')) ==="
}
result() { printf '%s|%s\n' "$1" "$2" > "$RESULT.tmp"; mv "$RESULT.tmp" "$RESULT"; }
trap 'result failed "Lỗi ở bước: $LABEL — xem logs/fun5b-setup.log"' ERR

MAIN_PY="$ROOT/venv/bin/python"
[ -x "$MAIN_PY" ] || { result failed "Chưa có môi trường chính (venv/) — chạy cài đặt app trước"; exit 1; }
PY_VERSION=3.11.15
TORCH_INDEX=https://download.pytorch.org/whl/cu128
ENGINE="$ROOT/engines/Fun5B"
OWN_UV_CACHE="$ROOT/.install-cache/uv"
export UV_CACHE_DIR="${UV_CACHE_DIR:-$OWN_UV_CACHE}"
export TMPDIR="$ROOT/.install-tmp/fun5b"
mkdir -p "$TMPDIR" "$ENGINE"

progress 1 "Kiểm tra GPU, dung lượng và công cụ"
set +e
REASON="$("$MAIN_PY" "$ROOT/fun5b_setup.py" preflight)"
rc=$?
set -e
if [ $rc -eq 3 ]; then result skipped "$REASON"; echo "skipped: $REASON"; exit 0; fi
if [ $rc -ne 0 ]; then result failed "$REASON"; echo "$REASON" >&2; exit 1; fi
echo "$REASON"

progress 2 "Chuẩn bị uv và Python $PY_VERSION"
UV="$(command -v uv || true)"
if [ -z "$UV" ] && [ -x "$HOME/.local/bin/uv" ]; then UV="$HOME/.local/bin/uv"; fi
if [ -z "$UV" ]; then  # fleet machines: a pinned uv inside the app's main venv
    [ -x "$ROOT/venv/bin/uv" ] || "$ROOT/venv/bin/pip" install "uv==0.13.0"
    UV="$ROOT/venv/bin/uv"
fi
"$UV" --version
"$UV" python install "$PY_VERSION"

progress 3 "Tải mã nguồn VideoX-Fun"
read -r VX_REPO VX_COMMIT < <("$MAIN_PY" -c "import json; s=json.load(open('install/fun5b-source.json')); print(s['repo'], s['commit'])")
VX="$ROOT/engines/VideoX-Fun"
if [ "$(git -C "$VX" rev-parse HEAD 2>/dev/null || true)" = "$VX_COMMIT" ]; then
    echo "already at $VX_COMMIT — skipping"
else
    rm -rf "$VX.partial"
    git clone --filter=blob:none --no-checkout "$VX_REPO" "$VX.partial"
    git -C "$VX.partial" checkout -q "$VX_COMMIT"
    rm -rf "$VX"
    mv "$VX.partial" "$VX"
fi

build_venv() {  # destination lock-file smoke-import
    local dest=$1 lock=$2 smoke=$3
    if [ -x "$dest/bin/python" ] && "$dest/bin/python" -c "$smoke" >/dev/null 2>&1; then
        echo "$dest already installed — skipping"
        return
    fi
    rm -rf "$dest.partial"
    "$UV" venv --relocatable --python "$PY_VERSION" "$dest.partial"
    # --no-deps: the lock is the complete, exact package set of the validated install (it has
    # onnxruntime-gpu but not the CPU onnxruntime VideoX-Fun's requirements would pull in).
    "$UV" pip install --python "$dest.partial/bin/python" --no-deps \
        --index-strategy unsafe-best-match --extra-index-url "$TORCH_INDEX" -r "$lock"
    "$dest.partial/bin/python" -c "$smoke"
    rm -rf "$dest"
    mv "$dest.partial" "$dest"
}

progress 4 "Cài môi trường Fun-5B (PyTorch, ~9 GB)"
build_venv "$ENGINE/.venv" install/fun5b-requirements.lock \
    "import torch, torchvision, sageattention, rtmlib, onnxruntime, decord, diffusers, cv2"

progress 5 "Cài môi trường vẽ nền FireRed"
build_venv "$ENGINE/.venv-edit" install/fun5b-edit-requirements.lock "import torch, diffusers, gguf, transformers"

progress 6 "Tải model Fun-5B và FireRed (~57 GB)"
FUN5B_HF="$ENGINE/.venv/bin/hf" bash install/download_fun5b_models.sh

progress 7 "Kiểm tra cài đặt"
"$MAIN_PY" "$ROOT/fun5b_setup.py" verify
"$ENGINE/.venv/bin/python" -c "import torch; assert torch.cuda.is_available(), 'CUDA không dùng được trong engines/Fun5B/.venv'; print('cuda', torch.version.cuda, torch.cuda.get_device_name(0))"

# The uv cache (torch wheels etc.) is only needed to resume an interrupted install.
rm -rf "$OWN_UV_CACHE" "$TMPDIR"
result ok "Đã cài xong Fun-5B"
echo "Fun-5B setup complete."
