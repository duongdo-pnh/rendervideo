"""Smoke-test a venv's torch install: CUDA actually initializes and can allocate.

Run with the TARGET venv's own python interpreter, e.g.:
    venv/bin/python install/smoke_test.py --label main --expect-torch 2.7.1
    engines/MuseTalk/.venv/bin/python install/smoke_test.py --label musetalk --expect-torch 2.0.1

This is deliberately the last step of setup.sh for each venv. It is the exact check
that would have caught the 2026-08-14 incident -- a leaked PYTORCH_CUDA_ALLOC_CONF env
var made the MuseTalk venv's torch build fail torch.cuda.init() outright -- before it
silently failed ~35 real render jobs overnight.
"""
from __future__ import annotations

import argparse
import sys


def run(label: str, expect_torch: str | None) -> int:
    label = label or "venv"
    print(f"[smoke_test:{label}] python {sys.version.split()[0]} ({sys.executable})")

    try:
        import torch
    except Exception as e:
        print(f"[smoke_test:{label}] FAIL: cannot import torch: {type(e).__name__}: {e}", file=sys.stderr)
        return 1

    print(f"[smoke_test:{label}] torch {torch.__version__}")
    if expect_torch and not torch.__version__.startswith(expect_torch):
        print(f"[smoke_test:{label}] FAIL: expected torch {expect_torch}, got {torch.__version__}",
              file=sys.stderr)
        return 1

    if not torch.cuda.is_available():
        print(f"[smoke_test:{label}] FAIL: torch.cuda.is_available() is False", file=sys.stderr)
        return 1

    try:
        device_name = torch.cuda.get_device_name(0)
        x = torch.zeros(4, device="cuda")
        total = (x + 1).sum().item()
    except Exception as e:
        print(f"[smoke_test:{label}] FAIL: CUDA init/alloc raised {type(e).__name__}: {e}", file=sys.stderr)
        return 1

    print(f"[smoke_test:{label}] OK: CUDA initialized, device={device_name}, test tensor sum={total}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--label", default="", help="Human label for this venv (main/musetalk) — report-only.")
    ap.add_argument("--expect-torch", default=None,
                     help="Fail unless torch.__version__ starts with this (e.g. 2.7.1).")
    args = ap.parse_args()
    return run(args.label, args.expect_torch)


if __name__ == "__main__":
    raise SystemExit(main())
