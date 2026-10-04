#!/usr/bin/env bash
# Exit 0 only when JARVIS is idle: no session, not speaking/thinking, no wake/turn in the journal for $1 s (def 90).
quiet=${1:-90}
snap=$(~/jarvis/bin/jarvisctl status 2>/dev/null) || { echo "jarvisd unreachable"; exit 1; }
python3 - "$snap" <<'PY' || exit 1
import json, sys
s = json.loads(sys.argv[1])
st = s.get("state", {})
busy = st.get("session") or st.get("mode") not in ("idle", None) or (s.get("computer") or {}).get("active")
print("state:", st, "computer:", (s.get("computer") or {}).get("active"))
sys.exit(1 if busy else 0)
PY
recent=$(journalctl --user -u jarvisd --since "-${quiet}s" --no-pager -o cat 2>/dev/null | grep -E "wake|heard|addressed|session|reply|filler" | tail -3)
if [ -n "$recent" ]; then echo "recent activity:"; echo "$recent"; exit 1; fi
nvidia-smi --query-gpu=memory.used,utilization.gpu --format=csv,noheader
echo idle
