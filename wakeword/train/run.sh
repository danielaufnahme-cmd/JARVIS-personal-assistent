#!/usr/bin/env bash
# Run training pipeline stages in the separate training venv.
#   wakeword/train/run.sh all      (or single stages: generate extras piper_voices speechbank augment features train export)
# First time: train/download.sh (venv, espeak-ng, ACAV100M 17 GB, voices, corpora), then train/gen_testset.py.
set -euo pipefail
WW="$(cd "$(dirname "$0")/.." && pwd)"
export PATH="$WW/data/tools/espeak-ng/bin:$PATH"      # espeak-ng built from source (no sudo)
export HF_HOME="$WW/data/hf"
export PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True
exec nice -n 10 "$WW/.venv-train/bin/python" "$WW/train/pipeline.py" "$WW/train/jarvis.train.yaml" "$@"
