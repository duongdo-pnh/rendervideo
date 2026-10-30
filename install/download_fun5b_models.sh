#!/usr/bin/env bash
# Download every model the Fun-5B engine uses into checkpoints/fun5b/ (~57GB on disk), pinned to
# the Hugging Face commits the RTX 3090 install was validated with. Safe to re-run: complete files
# are skipped, so an interrupted download resumes. Needs engines/Fun5B/.venv (for the `hf` CLI).
# Ported from temo's scripts/fun-models.sh. Called by install/setup_fun5b.sh.
set -euo pipefail
cd "$(dirname "$0")/.."

HF="${FUN5B_HF:-engines/Fun5B/.venv/bin/hf}"
[ -x "$HF" ] || { echo "missing $HF: create engines/Fun5B/.venv first (install/setup_fun5b.sh)"; exit 1; }
# hf_transfer stalled at ~95% of big files twice on this network; the default downloader resumes.
export HF_HUB_ENABLE_HF_TRANSFER=0
C="${FUN5B_CKPT_DIR:-checkpoints/fun5b}"
F="$C/wan22-fun-5b"

hf_get() {  # repo revision local_dir [hf download args...] — 3 tries; the last one without Xet
  local repo=$1 rev=$2 dir=$3; shift 3
  for try in 1 2 3; do
    if [ "$try" = 3 ]; then export HF_HUB_DISABLE_XET=1; fi
    "$HF" download "$repo" "$@" --revision "$rev" --local-dir "$dir" && return 0
    echo "download of $repo failed (try $try/3); retrying in 20s" >&2
    sleep 20
  done
  return 1
}

fetch() {  # url destination md5
  if [ -f "$2" ] && [ "$(md5sum "$2" | cut -d' ' -f1)" = "$3" ]; then return; fi
  mkdir -p "$(dirname "$2")"
  curl -L --fail --retry 3 --connect-timeout 30 -C - -o "$2.part" "$1"
  mv "$2.part" "$2"
  [ "$(md5sum "$2" | cut -d' ' -f1)" = "$3" ] || { rm -f "$2"; echo "md5 mismatch: $2"; exit 1; }
}

echo "[models] Fun-5B video model (23GB): transformer, Wan2.2 VAE, T5 encoder"
hf_get alibaba-pai/Wan2.2-Fun-5B-Control b8bc1a65ab71d054ba4636dc0dac104aa4df2686 "$F/Wan2.2-Fun-5B-Control"
echo "[models] FastWan LoRA"
hf_get Kijai/WanVideo_comfy 8260d429d19fd7a72304cad059160b95d843913f "$F/loras" \
  FastWan/Wan2_2_5B_FastWanFullAttn_lora_rank_128_bf16.safetensors
echo "[models] DWPose"
hf_get yzd-v/DWPose 1a7144101628d69ee7a3768d1ee3a094070dc388 "$F/DWPose" yolox_l.onnx dw-ll_ucoco_384.onnx
echo "[models] Big-LaMa"
fetch https://github.com/Sanster/models/releases/download/add_big_lama/big-lama.pt "$F/lama/big-lama.pt" \
  e3aa4aaa15225a33ec84f9f4bc47e500
echo "[models] Robust Video Matting"
if [ ! -f "$F/rvm/hub/PeterL1n_RobustVideoMatting_master/hubconf.py" ]; then
  mkdir -p "$F/rvm/hub"
  rm -rf "$F/rvm/hub/RobustVideoMatting-master"
  curl -L --fail --retry 3 https://github.com/PeterL1n/RobustVideoMatting/archive/refs/heads/master.tar.gz \
    | tar -xz -C "$F/rvm/hub"
  mv "$F/rvm/hub/RobustVideoMatting-master" "$F/rvm/hub/PeterL1n_RobustVideoMatting_master"
fi
fetch https://github.com/PeterL1n/RobustVideoMatting/releases/download/v1.0.0/rvm_resnet50.pth \
  "$F/rvm/hub/checkpoints/rvm_resnet50.pth" 04da1044ab32202b73a164f679824f39

# Clean background editor: FireRed-Image-Edit-1.1 (GGUF Q5_K_M, 14GB) with Qwen-Image-Edit's text
# encoder and VAE (16GB), shared through symlinks.
echo "[models] Qwen-Image-Edit text encoder + VAE (16GB)"
Q="$C/qwen-image-edit"
hf_get Qwen/Qwen-Image-Edit ac7f9318f633fc4b5778c59367c8128225f1e3de "$Q" --include "text_encoder/*" --include "vae/*"
echo "[models] FireRed-Image-Edit-1.1 (14GB)"
R="$C/firered-image-edit"
hf_get vantagewithai/FireRed-Image-Edit-1.1-GGUF 6882be7fafd594a3655d9fbcf5474ef141c34a86 "$R" \
  FireRed-Image-Edit-1.1-Q5_K_M.gguf
hf_get FireRedTeam/FireRed-Image-Edit-1.1 3bc3f2a12722fd9883eb6357500de191d56baaf5 "$R" \
  --include model_index.json --include "scheduler/*" --include "processor/*" --include "tokenizer/*" \
  --include transformer/config.json
for part in text_encoder vae; do
  [ -e "$R/$part" ] || ln -s "../qwen-image-edit/$part" "$R/$part"
done
echo "[models] ready in $C ($(du -shL "$C" | cut -f1))"
