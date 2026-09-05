"""Resolve the detected GPU to a install/gpu_profiles.json entry.

On success, prints shell-sourceable KEY=VALUE lines to stdout (setup.sh does
`eval "$(python3 install/resolve_profile.py)"`). On failure, prints nothing to
stdout and a human-readable reason to stderr, exit 1 — never guesses a pin for
an architecture that hasn't actually been validated (see gpu_profiles.json).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from detect_gpu import GPUDetectionError, detect  # noqa: E402

PROFILES_PATH = Path(__file__).parent / "gpu_profiles.json"


def main() -> int:
    try:
        info = detect()
    except GPUDetectionError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    gpu = info["primary"]
    arch = gpu["sm_arch"]
    profiles = json.loads(PROFILES_PATH.read_text())
    known = sorted(k for k in profiles if not k.startswith("_"))

    if arch not in profiles:
        print(f"ERROR: no compatibility profile for {arch} ({gpu['name']}).", file=sys.stderr)
        print(f"Known architectures: {', '.join(known)}.", file=sys.stderr)
        print(f"Add a validated entry to {PROFILES_PATH} after manually installing and "
              f"passing install/smoke_test.py on this hardware — do not guess a pin.",
              file=sys.stderr)
        return 1

    profile = profiles[arch]
    main_p = profile.get("main")
    musetalk_p = profile.get("musetalk")
    if not main_p:
        print(f"ERROR: profile {arch} has no 'main' venv pin.", file=sys.stderr)
        return 1
    if not musetalk_p:
        print(f"ERROR: {arch} ({gpu['name']}) has no validated MuseTalk-venv pin yet.",
              file=sys.stderr)
        print("This is a known gap, not a bug — see the comment in gpu_profiles.json: "
              "cu118-era torch (MuseTalk's current pin) generally has no kernels for "
              "architectures newer than what it was built for, so this isn't solvable "
              "by picking a different version number alone. It needs real validation "
              "(and possibly code changes in engines/MuseTalk) before it belongs here. "
              "Stopping rather than installing something untested.", file=sys.stderr)
        return 1

    print(f"GPU_NAME={gpu['name']!r}")
    print(f"SM_ARCH={arch}")
    print(f"MAIN_TORCH={main_p['torch']}")
    print(f"MAIN_TORCHVISION={main_p['torchvision']}")
    print(f"MAIN_INDEX_URL={main_p['index_url']}")
    print(f"MUSETALK_TORCH={musetalk_p['torch']}")
    print(f"MUSETALK_TORCHVISION={musetalk_p['torchvision']}")
    print(f"MUSETALK_TORCHAUDIO={musetalk_p['torchaudio']}")
    print(f"MUSETALK_INDEX_URL={musetalk_p['index_url']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
