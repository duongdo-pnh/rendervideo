#!/bin/bash
# Packs the repo's Python source (tracked + untracked-but-not-gitignored files)
# into desktop/src-tauri/resources/rendervideo-src.tar.gz, which the Tauri app
# extracts over ~/.local/share/byscom-render on every version bump (see
# ensure_source() in desktop/src-tauri/src/lib.rs).
#
# Deliberately excludes:
#   - desktop/ itself (the packaging shell has no business re-bundling itself)
#   - frontend/ SOURCE — only frontend/dist (the built static assets) ships;
#     backend/app.py serves that directly, nobody needs the React source on
#     the target machine.
#   - engines/ — doesn't exist in this repo at all; MuseTalk's ~34GB venv +
#     checkpoints live only at the install location, managed by install/setup.sh,
#     never touched by this bundle.
#
# Run from the repo root: desktop/scripts/bundle-source.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

if [ ! -d frontend/dist ]; then
    echo "frontend/dist missing — run 'npm run build' in frontend/ first." >&2
    exit 1
fi

OUT_DIR="desktop/src-tauri/resources"
OUT_FILE="$OUT_DIR/rendervideo-src.tar.gz"
mkdir -p "$OUT_DIR"

WORKLIST="$(mktemp)"
trap 'rm -f "$WORKLIST"' EXIT

{
    git ls-files -z
    git ls-files -z --others --exclude-standard
} | tr '\0' '\n' \
    | grep -v '^desktop/' \
    | grep -v '^frontend/' \
    | grep -vE '^(venv|venv\.backup|engines|checkpoints)/' \
    > "$WORKLIST"

# frontend/dist only, not frontend source.
( cd frontend/dist && find . -type f ) | sed 's#^\./#frontend/dist/#' >> "$WORKLIST"

echo "Bundling $(wc -l < "$WORKLIST") files -> $OUT_FILE"
tar -czf "$OUT_FILE" --files-from="$WORKLIST"
echo "Done: $(du -h "$OUT_FILE" | cut -f1)"
