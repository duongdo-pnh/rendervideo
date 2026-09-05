"""Detect the installed NVIDIA GPU and map it to an architecture family (sm_XX).

Used by setup.sh to look up install/gpu_profiles.json before installing either venv.
Prints a single JSON object to stdout so shell callers can parse it with jq/python,
and can also be imported directly (see detect() / sm_arch()).

Deliberately does NOT guess a compatible torch/CUDA build here — that decision lives
in gpu_profiles.json, hand-curated per architecture, so a new/untested GPU generation
fails loudly (see setup.sh) instead of silently picking something that might not work
(see the incident this install/ directory exists to prevent: 2026-08-14, an env var
tuned for one venv's torch build crashed the other venv's incompatible torch build
across ~35 real render jobs before it was caught).
"""
from __future__ import annotations

import json
import subprocess
import sys

NVIDIA_SMI_QUERY = "name,compute_cap,memory.total,driver_version"


class GPUDetectionError(RuntimeError):
    pass


def _run_nvidia_smi() -> str:
    try:
        out = subprocess.run(
            ["nvidia-smi", f"--query-gpu={NVIDIA_SMI_QUERY}", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=15,
        )
    except FileNotFoundError:
        raise GPUDetectionError(
            "nvidia-smi not found — no NVIDIA driver installed, or not on PATH."
        ) from None
    except subprocess.TimeoutExpired:
        raise GPUDetectionError("nvidia-smi timed out after 15s.") from None
    if out.returncode != 0:
        raise GPUDetectionError(f"nvidia-smi failed (rc={out.returncode}): {out.stderr.strip()}")
    if not out.stdout.strip():
        raise GPUDetectionError("nvidia-smi returned no GPUs.")
    return out.stdout


def sm_arch(compute_cap: str) -> str:
    """'8.6' -> 'sm_86', '12.0' -> 'sm_120'. Mirrors nvcc's own naming convention."""
    major, _, minor = compute_cap.strip().partition(".")
    return f"sm_{major}{minor or '0'}"


# Human-readable family name per architecture generation — informational only,
# gpu_profiles.json is keyed on the sm_XX string itself, not this label.
_FAMILY_BY_MAJOR = {
    "6": "Pascal",
    "7": "Volta/Turing",
    "8": "Ampere/Ada",
    "9": "Hopper",
    "10": "Blackwell",
    "12": "Blackwell",
}


def detect() -> dict:
    """Return info for every visible GPU plus which one setup.sh should key off of
    (the first — this codebase's render/GPU-lock logic already assumes a single GPU)."""
    rows = []
    for line in _run_nvidia_smi().strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) != 4:
            continue
        name, compute_cap, mem_total, driver = parts
        arch = sm_arch(compute_cap)
        major = compute_cap.strip().split(".")[0]
        rows.append({
            "name": name,
            "compute_cap": compute_cap,
            "sm_arch": arch,
            "family": _FAMILY_BY_MAJOR.get(major, "unknown"),
            "memory_total_mib": int(mem_total) if mem_total.isdigit() else None,
            "driver_version": driver,
        })
    if not rows:
        raise GPUDetectionError("Could not parse any GPU from nvidia-smi output.")
    return {"gpus": rows, "primary": rows[0]}


def main() -> int:
    try:
        info = detect()
    except GPUDetectionError as e:
        print(json.dumps({"error": str(e)}), file=sys.stdout)
        print(f"[detect_gpu] {e}", file=sys.stderr)
        return 1
    print(json.dumps(info, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
