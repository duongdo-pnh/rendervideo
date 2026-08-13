"""Shared TTL garbage collection for MuseTalk avatar cache (results/v15/avatars/<avatar_id>/).

Each avatar build is 7-19 GB and never gets removed on its own — see the Avatar Cache tab in
web_ui.py, which surfaced 46.8 GB of caches whose source video had since been overwritten.
This adds a general safety net on top of that: ANY avatar not used for a render in
DEFAULT_MAX_AGE_DAYS gets swept, whether or not its source video still matches.

"Used" is tracked with a marker file's mtime, touched by both call sites that load an avatar:
musetalk_render_server.py (batch queue) and musetalk_stream_api.py (live streaming). Avatars
built before this file existed have no marker yet; last_used() falls back to avator_info.json's
mtime (effectively "last built") so they age out normally instead of comparing against 0.

_test*-prefixed avatar_ids are dev fixtures (see web_ui.py's Avatar Cache tab) and are never
swept automatically.
"""
import shutil
import time
from pathlib import Path

MARKER_NAME = ".last_used"
DEFAULT_MAX_AGE_DAYS = 3


def touch(avatar_dir):
    """Record that avatar_dir was just used for a render. Call on every cache HIT and MISS/build."""
    try:
        Path(avatar_dir, MARKER_NAME).touch()
    except OSError:
        pass  # best-effort; a missed touch just means this avatar ages out a bit early


def last_used(avatar_dir):
    """Seconds-since-epoch of the last recorded use, or None if avatar_dir has no info file at all."""
    avatar_dir = Path(avatar_dir)
    marker = avatar_dir / MARKER_NAME
    if marker.is_file():
        return marker.stat().st_mtime
    info = avatar_dir / "avator_info.json"
    if info.is_file():
        return info.stat().st_mtime
    return None


def sweep(root, max_age_days=DEFAULT_MAX_AGE_DAYS, now=None):
    """Delete avatar dirs under root unused for more than max_age_days. Returns removed [(avatar_id, bytes)].

    now: inject for tests; defaults to time.time() at call time (kept out of the signature default
    so importing this module doesn't freeze a stale timestamp).
    """
    root = Path(root)
    if not root.is_dir():
        return []
    if now is None:
        now = time.time()
    cutoff = now - max_age_days * 86400
    removed = []
    for d in sorted(root.iterdir()):
        if not d.is_dir() or d.name.startswith("_test"):
            continue
        if not (d / "avator_info.json").is_file():
            continue
        used = last_used(d)
        if used is not None and used >= cutoff:
            continue
        size = sum(f.stat().st_size for f in d.rglob("*") if f.is_file())
        shutil.rmtree(d, ignore_errors=True)
        removed.append((d.name, size))
    return removed
