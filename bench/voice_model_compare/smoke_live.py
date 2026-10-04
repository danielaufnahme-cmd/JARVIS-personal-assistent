"""Silent smoke test of the model llama-swap now serves as JARVIS's voice model: one real Agent turn with FAKE
tools (the bench's guard + fakes), no TTS, no jarvisd. uv run python bench/voice_model_compare/smoke_live.py"""
import asyncio, dataclasses, json, logging, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import bench as b

logging.basicConfig(level=logging.WARNING)
b.install_process_guard()
b._import_jarvis()
cfg = b.load_config()
cat = b.LogCatcher(); logging.getLogger("jarvis").addHandler(cat)
case = next(c for c in b.CASES if c["id"] == "multi-02")
row = asyncio.run(b.run_case(case, "qwen35-4b", b.LLAMA_SWAP, cfg, cat))
print(json.dumps({k: row[k] for k in ("text", "actions", "reply", "fallback", "turn_s", "ttft_s")}, ensure_ascii=False))
