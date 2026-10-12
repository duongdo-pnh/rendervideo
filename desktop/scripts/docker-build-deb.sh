#!/bin/bash
# Builds the .deb inside an Ubuntu 22.04 container so the resulting glibc
# requirement works on both 22.04 and 24.04 (built-on-24.04 binaries fail to
# even start on 22.04). Run from the repo root:
#   desktop/scripts/docker-build-deb.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

IMAGE_TAG="byscom-render-desktop-builder:ubuntu22"

echo "=== [1/3] Building frontend ==="
( cd frontend && npm install && npm run build )

echo "=== [2/3] Bundling Python source ==="
desktop/scripts/bundle-source.sh

echo "=== [3/3] Building .deb inside Ubuntu 22.04 container ==="
docker build -t "$IMAGE_TAG" -f desktop/docker/Dockerfile.ubuntu22 desktop/docker

docker run --rm \
    -v "$ROOT":/work \
    -w /work/desktop \
    "$IMAGE_TAG" \
    tauri build

echo
echo "Done. .deb output under desktop/src-tauri/target/release/bundle/deb/"
find desktop/src-tauri/target/release/bundle/deb -name '*.deb' 2>/dev/null
