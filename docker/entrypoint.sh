#!/bin/sh
# Container entrypoint: dependency installation happens during image build;
# this script performs runtime initialization (data dir, basic checks)
# and then starts the web server.
set -eu

PORT="${PORT:-8000}"
export DATA_DIR="${DATA_DIR:-/data}"

mkdir -p "$DATA_DIR"

echo "[entrypoint] data dir: $DATA_DIR"
echo "[entrypoint] listening on 0.0.0.0:$PORT"

exec "$@"
