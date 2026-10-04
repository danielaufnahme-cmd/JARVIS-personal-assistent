#!/usr/bin/env bash
# Install the fine-tuned 2B into JARVIS (llama-swap entry + pill menu; active only if it passed the bar).
# Safe to run again (idempotent); rolls everything back on any error. See install_model.py for the details.
#   ~/jarvis/finetune/install_model.sh              # by hand (restarts jarvisd to apply and check the settings)
#   ~/jarvis/finetune/install_model.sh --no-restart # edit + check llama-swap only; jarvisd picks it up next start
set -uo pipefail
FT="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
export PATH="$HOME/.local/bin:$PATH"
exec uv run --project "$FT/.." python "$FT/install_model.py" "$@"
