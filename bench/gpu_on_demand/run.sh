#!/usr/bin/env bash
# Section 20 bench: a private llama-swap on bench ports, measure.py, then everything private is stopped.
set -u
cd "$(dirname "$0")/../.."
mkdir -p "${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/jarvis-slots-bench"
~/.local/bin/llama-swap -config bench/gpu_on_demand/swap-bench.yaml -listen 127.0.0.1:18434 \
  > bench/gpu_on_demand/swap-bench.log 2>&1 &
SWAP=$!
cleanup() {
  kill "$SWAP" 2>/dev/null; sleep 1
  pkill -f -- "--port 185[0-9][0-9] " 2>/dev/null
  rm -rf "${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/jarvis-slots-bench"
}
trap cleanup EXIT
for _ in $(seq 50); do curl -sf 127.0.0.1:18434/running >/dev/null && break; sleep 0.1; done
uv run python bench/gpu_on_demand/measure.py "$@"
