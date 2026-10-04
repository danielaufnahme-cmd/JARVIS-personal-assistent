"""Shared code for the section 16 fine-tuning pipeline (finetune/run.sh). Runs in the main jarvis venv
(`uv run`) except `train.py` / `render.py`, which run in finetune/.venv-train and only import `paths` and `guard`."""
