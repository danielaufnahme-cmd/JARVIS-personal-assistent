#!/usr/bin/env bash
# The fast, unattended fine-tune: JARVIS is switched off so the whole GPU trains (bf16 LoRA), then the result is
# installed and JARVIS is started again, ALWAYS (finished, failed or stopped).
#
#   ~/jarvis/finetune/start.sh    # start or resume (runs this script in the unit jarvis-finetune, with limits)
#   cat ~/jarvis/finetune/STATUS                                                  # progress
#   systemctl --user stop jarvis-finetune                                         # stop (JARVIS comes back)
#
# 1. stops jarvisd and unloads every llama-swap model;  2. runs finetune/run.sh with FT_TRAIN_MODE=bf16 (resumable);
# 3. on success runs finetune/install_model.sh;  4. on ANY exit starts jarvisd again and writes finetune/RESULT.txt
# if the run didn't get that far. If you start jarvisd yourself mid-run, the pipeline's VRAM reserve check pauses the
# GPU work ("paused: keeping VRAM for JARVIS") until you stop jarvisd again or the run is restarted.
#
#   --dry-run   test the stop/start handling only: no pipeline, no model unload (FT_DRY_SLEEP=<s> waits that long,
#               FT_DRY_FAIL=1 exits with an error, FT_DRY_INSTALL=1 runs install_model.sh --no-services)
#   FT_JARVISD_UNIT=<unit> (tests: a stand-in for jarvisd)
set -uo pipefail
FT="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
export PATH="$HOME/.local/bin:$PATH"
UNIT="${FT_JARVISD_UNIT:-jarvisd}"
# JARVIS is off during this run, so its 2.5 GB VRAM reserve shrinks to 1.5 GB for the desktop; the teacher uses the
# difference to keep more expert layers on the GPU (less RAM). Starting jarvisd mid-run pauses the GPU work.
export FT_VRAM_RESERVE_MB="${FT_VRAM_RESERVE_MB:-1500}"
SWAP="${FT_LLAMA_SWAP_URL:-http://127.0.0.1:8401}"
RESULT="${FT_RESULT:-$FT/RESULT.txt}"
DRY=0
[ "${1:-}" = "--dry-run" ] && DRY=1
mkdir -p "$FT/logs"
LOG="$FT/logs/overnight-$(date +%Y%m%d-%H%M%S)$([ $DRY = 1 ] && echo -dry).log"
# No `exec > >(tee …)`: systemctl stop would kill tee before the trap's messages are written.
say() { local l; l="$(date '+%F %T') overnight: $*"; echo "$l"; echo "$l" >> "$LOG"; }

CHILD=""
PHASE="starting"
DONE=0
restore() {
  local rc=$?
  [ "$DONE" = 1 ] && return
  DONE=1
  trap - EXIT INT TERM
  if [ -n "$CHILD" ] && kill -0 "$CHILD" 2>/dev/null; then
    say "stopping the pipeline (it checkpoints first)…"
    kill -TERM -- "-$CHILD" 2>/dev/null || kill -TERM "$CHILD" 2>/dev/null
    for _ in $(seq 60); do kill -0 "$CHILD" 2>/dev/null || break; sleep 1; done
    kill -KILL -- "-$CHILD" 2>/dev/null
  fi
  if [ "$PHASE" != "installed" ]; then
    local status; status="$(cat "$FT/STATUS" 2>/dev/null)"
    case "$PHASE" in
      stopped) echo "The training was stopped before it finished ($status). JARVIS was started again with its old settings. To continue where it stopped, run ~/jarvis/finetune/start.sh again" > "$RESULT" ;;
      failed)  echo "The training stopped with an error ($status). JARVIS was started again with its old settings, nothing was installed. The log is $LOG. Running ~/jarvis/finetune/start.sh again retries from the failed step" > "$RESULT" ;;
      install-failed) : ;;  # install_model.sh wrote RESULT.txt itself
      *) echo "The training run ended early ($PHASE, exit $rc; $status). JARVIS was started again. Log: $LOG" > "$RESULT" ;;
    esac
  fi
  if systemctl --user is-active --quiet "$UNIT"; then
    say "$UNIT is running"
  else
    say "starting $UNIT again"
    systemctl --user start "$UNIT" && say "$UNIT started" || say "could not start $UNIT: run 'systemctl --user start $UNIT'"
  fi
  say "finished ($PHASE); $RESULT"
  # Always exit 0: the outcome is in RESULT.txt, and a transient unit that ends "failed" would stay loaded and
  # block the next `systemd-run --unit=jarvis-finetune` (the resume command).
  exit 0
}
trap restore EXIT
trap 'PHASE=stopped; exit 143' INT TERM

say "log $LOG"
say "stopping $UNIT and unloading the models, so the training gets the GPU"
systemctl --user stop "$UNIT" || say "warning: couldn't stop $UNIT"
if [ $DRY = 1 ]; then
  say "dry run: would POST $SWAP/api/models/unload"
else
  curl -s -m 60 -X POST "$SWAP/api/models/unload" >/dev/null && say "llama-swap: all models unloaded" \
    || say "warning: couldn't reach llama-swap to unload"
fi

PHASE="training"
if [ $DRY = 1 ]; then
  say "dry run: skipping the pipeline (sleep ${FT_DRY_SLEEP:-2} s)"
  setsid sleep "${FT_DRY_SLEEP:-2}" & CHILD=$!
  wait "$CHILD"; rc=$?
  [ "${FT_DRY_FAIL:-0}" = 1 ] && rc=1
else
  # Its own process group, so a stop can signal the whole pipeline; run in the background so the trap fires at once.
  FT_TRAIN_MODE=bf16 setsid "$FT/run.sh" & CHILD=$!
  wait "$CHILD"; rc=$?
fi
CHILD=""
if [ $rc -ne 0 ]; then
  PHASE="failed"
  say "the pipeline failed (exit $rc)"
  exit $rc
fi

PHASE="installing"
say "pipeline done; installing the model"
if [ $DRY = 1 ]; then
  if [ "${FT_DRY_INSTALL:-0}" = 1 ]; then "$FT/install_model.sh" --no-services; rc=$?; else rc=0; fi
else
  "$FT/install_model.sh"; rc=$?
fi
if [ $rc -eq 0 ]; then PHASE="installed"; else PHASE="install-failed"; fi
say "install exit $rc"
cat "$RESULT" 2>/dev/null
exit 0
