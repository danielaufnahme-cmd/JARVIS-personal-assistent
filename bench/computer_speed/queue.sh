#!/usr/bin/env bash
# Section 23: run benchmark configs one after another, each only once nobody is using the GPU or JARVIS
# (bench.py checks; exit 3 = busy -> wait and retry). Usage: queue.sh "<config>:<runs>" ...
cd "$(dirname "$0")/../.." || exit 1
for item in "$@"; do
  cfg=${item%%:*}; runs=${item##*:}
  while true; do
    if [ -n "$(curl -s 127.0.0.1:11434/api/ps | python3 -c 'import json,sys; print(" ".join(m["name"] for m in json.load(sys.stdin)["models"]))')" ] && [[ $cfg == *ollama* ]]; then
      echo "$(date +%T) $cfg: Ollama is in use, waiting"; sleep 60; continue
    fi
    uv run --with websockets bench/computer_speed/bench.py --config "$cfg" --runs "$runs" 2>&1 \
      | grep -E --line-buffered "^run|NOT|pausing|Traceback|Error|loaded in|saved"
    rc=${PIPESTATUS[0]}
    [ "$rc" = 3 ] && { sleep 60; continue; }
    echo "$(date +%T) $cfg finished rc=$rc"; break
  done
done
echo "$(date +%T) queue done"
