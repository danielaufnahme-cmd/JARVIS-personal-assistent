#!/usr/bin/env bash
# Section 16: fine-tune Qwen3.5-2B into JARVIS's voice brain, end to end, resumable.
#
#   finetune/run.sh                 # the whole pipeline (re-run the same command to continue after a stop)
#   finetune/run.sh --smoke         # tiny end-to-end test (~20 requests, 10 steps, 20 eval items) in data-smoke/
#   finetune/run.sh --stage eval    # (re)run one stage: setup format requests teacher filter build train export eval report
#   finetune/run.sh --status        # the one-line status, finished stages, the last log lines
#
# Detached (survives closing the terminal):
#   systemd-run --user --unit=jarvis-finetune ~/jarvis/finetune/run.sh
#   journalctl --user -u jarvis-finetune -f        # watch        cat ~/jarvis/finetune/STATUS   # one line
#   systemctl --user stop jarvis-finetune          # stop (safe at any time; run the same command again to resume)
#
# Every tool is faked (finetune/ftlib/world.py refuses to run otherwise); nothing is sent to jarvisd; the GPU is
# only used while no fullscreen window is active and the VRAM suffices (it pauses, logging "paused: …", and polls
# every 60 s). It never uses sudo, never restarts jarvisd, and never edits the llama-swap config: the report
# prints the entry to add.
set -uo pipefail

FT="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
ROOT="$(dirname "$FT")"
PY="$FT/.venv-train/bin/python"
STAGES=(setup format requests teacher filter build train export eval report)
SMOKE=0
ONLY=""
STATUS_ONLY=0
while [ $# -gt 0 ]; do
  case "$1" in
    --smoke) SMOKE=1 ;;
    --stage) ONLY="${2:-}"; shift ;;
    --status) STATUS_ONLY=1 ;;
    -h|--help) sed -n '2,17p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done
if [ "$SMOKE" = 1 ]; then export FT_DATA="$FT/data-smoke" FT_SMOKE=1; else export FT_DATA="$FT/data" FT_SMOKE=0; fi
STATE="$FT_DATA/state"
mkdir -p "$STATE" "$FT/logs" "$FT_DATA/logs"
# CPU headroom: at most 4 BLAS/OpenMP threads per Python process (llama-servers use 8, see ftlib/guard.py).
export OMP_NUM_THREADS=4 MKL_NUM_THREADS=4 OPENBLAS_NUM_THREADS=4 RAYON_NUM_THREADS=4
export PATH="$HOME/.local/bin:$PATH" PYTHONUNBUFFERED=1 HF_HOME="$FT/data/hf" TOKENIZERS_PARALLELISM=false

if [ "$STATUS_ONLY" = 1 ]; then
  cat "$FT/STATUS" 2>/dev/null || echo "no run yet"
  echo -n "finished stages ($FT_DATA):"; for s in "${STAGES[@]}"; do [ -f "$STATE/$s.done" ] && echo -n " $s"; done; echo
  last="$(ls -t "$FT"/logs/run-*.log 2>/dev/null | head -1)"
  [ -n "$last" ] && { echo "log: $last"; grep -v "HTTP Request" "$last" | tail -5; }
  exit 0
fi
if [ -n "$ONLY" ] && [[ ! " ${STAGES[*]} " =~ " $ONLY " ]]; then echo "unknown stage: $ONLY" >&2; exit 2; fi

exec 9>"$FT_DATA/.lock"
if ! flock -n 9; then echo "another run.sh is already running on $FT_DATA" >&2; exit 1; fi

LOG="$FT/logs/run-$(date +%Y%m%d-%H%M%S)$([ "$SMOKE" = 1 ] && echo -smoke).log"
exec > >(tee -a "$LOG") 2>&1
echo "=== finetune/run.sh $(date '+%F %T') data=$FT_DATA smoke=$SMOKE stage=${ONLY:-all} log=$LOG"
cd "$ROOT" || exit 1

status() { echo "$(date '+%F %H:%M') $([ "$SMOKE" = 1 ] && echo '[smoke] ')$*" > "$FT/STATUS"; }
ledger() {  # ledger <stage> <seconds>: stage wall-clock times + the disk use before the run
  python3 - "$FT_DATA/stage_times.json" "$1" "$2" <<'EOF'
import json, os, subprocess, sys
p, stage, secs = sys.argv[1], sys.argv[2], float(sys.argv[3])
d = json.load(open(p)) if os.path.exists(p) else {"stages": {}}
if "disk_before_bytes" not in d:
    d["disk_before_bytes"] = int(subprocess.run(["df", "-B1", "--output=used", "/"], capture_output=True, text=True).stdout.split()[-1])
if stage != "-":
    d["stages"][stage] = d["stages"].get(stage, 0) + secs
json.dump(d, open(p, "w"), indent=1)
EOF
}
ledger - 0

stage_setup() {
  # The training venv (pinned; see requirements-train.txt), the base weights and llama-quantize. Idempotent.
  if [ ! -x "$PY" ] || ! "$PY" -c "import unsloth, fla" >/dev/null 2>&1; then
    uv venv "$FT/.venv-train" --python 3.12 && nice -n 10 uv pip install --python "$PY" -r "$FT/requirements-train.txt" || return 1
  fi
  if [ ! -f "$FT/data/base/Qwen3.5-2B/model.safetensors.index.json" ]; then
    nice -n 10 "$FT/.venv-train/bin/hf" download Qwen/Qwen3.5-2B --local-dir "$FT/data/base/Qwen3.5-2B" || return 1
  fi
  if [ ! -x "$HOME/.local/src/llama.cpp/build/bin/llama-quantize" ]; then
    nice -n 10 cmake --build "$HOME/.local/src/llama.cpp/build" --target llama-quantize -j 8 || return 1
  fi
  for f in "$HOME/models/Qwen3.6-35B-A3B-UD-IQ4_XS.gguf" "$HOME/models/Qwen3.5-2B-UD-Q4_K_XL.gguf" \
           "$HOME/models/Qwen3.5-4B-UD-Q4_K_XL.gguf" "$HOME/.local/bin/llama-server"; do
    [ -e "$f" ] || { echo "missing: $f"; return 1; }
  done
  avail=$(df -B1G --output=avail / | tail -1 | tr -d ' ')
  [ "$avail" -ge 25 ] || { echo "only ${avail} GB free on /; the run needs ~20 GB"; return 1; }
}
stage_format()   { nice -n 10 uv run finetune/format_dump.py && nice -n 10 "$PY" finetune/format_check.py; }
stage_requests() { nice -n 10 uv run finetune/make_requests.py; }
stage_teacher()  { nice -n 10 uv run finetune/teacher.py; }
stage_filter()   { nice -n 10 uv run finetune/filter.py; }
stage_build()    { nice -n 10 "$PY" finetune/build_sft.py; }
stage_train()    { nice -n 10 "$PY" finetune/train.py; }
stage_export()   { nice -n 10 "$PY" finetune/export.py; }
stage_eval()     { nice -n 10 uv run finetune/evaluate.py; }
stage_report()   { nice -n 10 uv run finetune/report.py; }

n=0
for s in "${STAGES[@]}"; do
  n=$((n + 1))
  if [ -n "$ONLY" ] && [ "$s" != "$ONLY" ]; then continue; fi
  if [ -z "$ONLY" ] && [ -f "$STATE/$s.done" ]; then echo "--- stage $n/${#STAGES[@]} $s: already done"; continue; fi
  echo "--- stage $n/${#STAGES[@]} $s: start $(date '+%T')"
  status "stage $n/${#STAGES[@]} $s: running"
  t0=$(date +%s)
  unset FT_AFTER_PAUSE
  while true; do
    # "Pause new work": nothing starts while RAM headroom is below the reserve (no GPU needed for this check).
    python3 "$FT/ftlib/guard.py" wait --no-gpu --what "stage $s" ${FT_AFTER_PAUSE:+--after-pause}
    "stage_$s"; rc=$?
    [ $rc -eq 75 ] || break
    # 75 = the stage paused itself (a game, VRAM for JARVIS, or RAM: it stopped its llama-server / checkpointed the
    # trainer). Wait, then run it again; every stage resumes from its own saved progress.
    echo "--- stage $s paused; it continues when the GPU/RAM are free again ($(date '+%T'))"
    status "stage $n/${#STAGES[@]} $s: paused (game running / keeping VRAM for JARVIS / keeping RAM free); waiting"
    export FT_AFTER_PAUSE=1
    sleep 30
  done
  ledger "$s" $(( $(date +%s) - t0 ))
  if [ $rc -ne 0 ]; then
    status "stage $n/${#STAGES[@]} $s: FAILED (exit $rc), see $LOG; run the same command again to retry"
    echo "--- stage $s FAILED (exit $rc)"
    exit $rc
  fi
  touch "$STATE/$s.done"
  echo "--- stage $n/${#STAGES[@]} $s: done in $(( $(date +%s) - t0 )) s"
done
if [ -z "$ONLY" ] || [ "$ONLY" = report ]; then
  status "all stages done; report: $([ "$SMOKE" = 1 ] && echo "$FT_DATA/finetune_2b.md" || echo "$ROOT/docs/finetune_2b.md")"
fi
echo "=== finished $(date '+%F %T')"
