#!/usr/bin/env bash
# Start (or resume) the unattended fine-tune as the user unit `jarvis-finetune`, inside kernel limits, so that even a
# leak or a spike in the run can never freeze the PC (2026-09-26: a 9-minute freeze from memory thrashing):
#   MemoryHigh = RAM - 5 GB  (above it the kernel throttles and reclaims the unit's own memory)
#   MemoryMax  = RAM - 3 GB  (hard ceiling: the unit is OOM-killed before the desktop suffers)
#   MemorySwapMax = 1 GB, CPUQuota = 75 % of all threads, CPUWeight = 20, IOWeight = 20 (or a low I/O priority when
#   the io controller isn't delegated to the user manager), Nice = 10.
# The pipeline's own guards (finetune/ftlib/guard.py) pause it long before these limits are reached.
#
#   ~/jarvis/finetune/start.sh              # overnight mode: JARVIS off while it trains, back on at the end
#   ~/jarvis/finetune/start.sh --with-jarvis # JARVIS keeps running (slower: QLoRA, ~20 h, no automatic install)
#   ~/jarvis/finetune/start.sh --print      # only show the systemd-run command
# FT_* settings in the environment (FT_EPOCHS, FT_RAM_RESERVE_MB, …) are passed on to the unit.
set -uo pipefail
FT="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
UNIT="${FT_UNIT:-jarvis-finetune}"
SCRIPT="$FT/overnight.sh"
PRINT=0
PASS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --with-jarvis) SCRIPT="$FT/run.sh" ;;
    --print) PRINT=1 ;;
    --) shift; PASS=("$@"); break ;;   # the rest goes to the script (tests: -- --dry-run)
    -h|--help) sed -n '2,15p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done

state="$(systemctl --user is-active "$UNIT" 2>/dev/null)"
if [ "$state" = "active" ] || [ "$state" = "activating" ] || [ "$state" = "deactivating" ]; then
  echo "The training is already running (unit $UNIT is $state)."
  echo "Progress: cat ~/jarvis/finetune/STATUS    Stop: systemctl --user stop $UNIT"
  exit 0
fi
systemctl --user reset-failed "$UNIT" 2>/dev/null  # a failed earlier unit would block the name

total_mb=$(awk '/^MemTotal:/ {print int($2/1024)}' /proc/meminfo)
threads=$(nproc)
high=$(( total_mb - 5120 )); max=$(( total_mb - 3072 )); quota=$(( threads * 75 ))
cg="/sys/fs/cgroup/user.slice/user-$(id -u).slice/user@$(id -u).service/cgroup.subtree_control"
ctl="$(cat "$cg" 2>/dev/null)"
props=(-p Nice=10)
notes=()
if [[ " $ctl " == *" memory "* ]]; then
  props+=(-p "MemoryHigh=${high}M" -p "MemoryMax=${max}M" -p "MemorySwapMax=1G")
else
  notes+=("the memory controller isn't delegated to the user manager: no MemoryHigh/Max (the RAM guard still runs)")
fi
if [[ " $ctl " == *" cpu "* ]]; then
  props+=(-p "CPUQuota=${quota}%" -p CPUWeight=20)
else
  notes+=("the cpu controller isn't delegated: no CPUQuota/CPUWeight (Nice=10 and the thread caps still apply)")
fi
if [[ " $ctl " == *" io "* ]]; then
  props+=(-p IOWeight=20)
else
  props+=(-p IOSchedulingClass=best-effort -p IOSchedulingPriority=7)
  notes+=("the io controller isn't delegated to the user manager: IOWeight is replaced by the lowest best-effort I/O priority")
fi
envs=()
while IFS='=' read -r k _; do
  [[ "$k" == FT_* ]] && envs+=(-E "$k")
done < <(env)

cmd=(systemd-run --user "--unit=$UNIT" --description="JARVIS fine-tune (finetune/start.sh)" "${props[@]}" "${envs[@]}" "$SCRIPT" "${PASS[@]}")
echo "RAM ${total_mb} MiB, ${threads} threads -> MemoryHigh ${high}M, MemoryMax ${max}M, CPUQuota ${quota}%"
for n in "${notes[@]}"; do echo "note: $n"; done
echo "\$ ${cmd[*]}"
[ "$PRINT" = 1 ] && exit 0
if "${cmd[@]}"; then
  echo
  echo "Started. Progress: cat ~/jarvis/finetune/STATUS"
  echo "Log:  journalctl --user -u $UNIT -f      Stop: systemctl --user stop $UNIT      Resume: $FT/start.sh"
else
  echo "systemd-run failed" >&2
  exit 1
fi
