"""Stage 7 (train venv): merge the best LoRA into Qwen3.5-2B, convert to GGUF with llama.cpp's convert_hf_to_gguf.py,
quantize to Q4_K_M and Q5_K_M.

    finetune/.venv-train/bin/python finetune/export.py

The merged model gets the chat template of the served Qwen3.5-2B GGUF (the one the training data was rendered with),
and the result is checked to carry exactly that template. Output: ~/models/Qwen3.5-2B-jarvis-Q4_K_M.gguf and
…-Q5_K_M.gguf (smoke: inside data-smoke/export/). The 16-bit intermediates are deleted by the cleanup stage.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("UNSLOTH_DISABLE_STATISTICS", "1")

from ftlib.paths import BASE_HF, LLAMA_QUANTIZE, LLAMA_SRC, Progress, config, d, final_gguf, setup_logging  # noqa: E402

log = setup_logging("export")


def gguf_template(path: Path) -> str:
    sys.path.insert(0, str(LLAMA_SRC / "gguf-py"))
    from gguf import GGUFReader  # type: ignore[import-not-found]

    r = GGUFReader(str(path))
    f = r.fields["tokenizer.chat_template"]
    return bytes(f.parts[f.data[0]]).decode("utf-8")


def merge_on_cpu(adapter: Path, out: Path) -> None:
    """W' = W + (alpha / r) * B @ A, straight on the base checkpoint's tensors (fp32 math, stored as bf16), so every
    original tensor name is kept (including the MTP layer that transformers' save drops) and the adapter always goes
    into the ORIGINAL bf16 weights, also after a QLoRA run. On the CPU: seconds for a 2B, and no VRAM."""
    import torch
    from safetensors import safe_open
    from safetensors.torch import save_file

    conf = json.loads((adapter / "adapter_config.json").read_text())
    if conf.get("use_dora") or conf.get("use_rslora"):
        raise SystemExit("DoRA/rsLoRA adapters are not handled by this merge")
    scale = conf["lora_alpha"] / conf["r"]
    lora = safe_open(str(adapter / "adapter_model.safetensors"), "pt")
    lkeys = set(lora.keys())
    out.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.name + ".part")
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir()
    used = 0
    for src in sorted(BASE_HF.glob("*.safetensors")):
        base = safe_open(str(src), "pt")
        tensors = {}
        for k in base.keys():
            w = base.get_tensor(k)
            stem = "base_model.model." + k.removesuffix(".weight")
            a_key, b_key = stem + ".lora_A.weight", stem + ".lora_B.weight"
            if k.endswith(".weight") and a_key in lkeys:
                delta = lora.get_tensor(b_key).float() @ lora.get_tensor(a_key).float()
                w = (w.float() + scale * delta).to(w.dtype)
                used += 2
            tensors[k] = w.contiguous()
        save_file(tensors, str(tmp / src.name), metadata={"format": "pt"})
    if used != len(lkeys):
        raise SystemExit(f"only {used} of {len(lkeys)} LoRA tensors matched a base weight; refusing to export")
    for f in BASE_HF.iterdir():
        if f.is_file() and f.suffix != ".safetensors" and f.name != "README.md":
            shutil.copy(f, tmp / f.name)
    shutil.rmtree(out, ignore_errors=True)
    tmp.rename(out)
    log.info("merged %d LoRA tensors into the bf16 base (scale %.2f)", used, scale)


def main() -> int:
    cfg = config()
    best = d("checkpoints", "best")
    merged = d("export", "merged")
    bf16 = d("export", "Qwen3.5-2B-jarvis-bf16.gguf")
    template = d("format", "chat_template.jinja").read_text(encoding="utf-8")
    prog = Progress("8/10 export", 2 + len(cfg.quants))
    if not (merged / "config.json").is_file():
        from ftlib import guard

        # the CPU merge holds the 4.4 GB of bf16 weights (+ one fp32 tensor at a time): keep the RAM reserve free
        guard.wait_resources(0, "merging the adapter", ram_mb=6000, gpu=False)
        merge_on_cpu(best, merged)
    (merged / "chat_template.jinja").write_text(template, encoding="utf-8")
    tc = merged / "tokenizer_config.json"
    if tc.is_file():
        conf = json.loads(tc.read_text(encoding="utf-8"))
        conf["chat_template"] = template
        tc.write_text(json.dumps(conf, ensure_ascii=False, indent=2), encoding="utf-8")
    prog.update(1)
    if not bf16.is_file():
        env = dict(os.environ, PYTHONPATH=str(LLAMA_SRC / "gguf-py"))
        cmd = ["nice", "-n", "10", sys.executable, str(LLAMA_SRC / "convert_hf_to_gguf.py"), str(merged),
               "--outtype", "bf16", "--outfile", str(bf16)]
        log.info("converting: %s", " ".join(cmd))
        subprocess.run(cmd, check=True, env=env)
    got = gguf_template(bf16)
    if got != template:
        raise SystemExit("the converted GGUF's chat template differs from the served 2B's; refusing to continue")
    log.info("bf16 GGUF ok, chat template identical to the served Qwen3.5-2B")
    prog.update(2)
    for i, q in enumerate(cfg.quants):
        out = final_gguf(q)
        if not out.is_file():
            tmp = out.with_suffix(".part")
            subprocess.run(["nice", "-n", "10", str(LLAMA_QUANTIZE), str(bf16), str(tmp), q, os.environ.get("FT_LLAMA_THREADS", "8")], check=True,
                           stdout=subprocess.DEVNULL)
            tmp.replace(out)
        log.info("%s: %s (%.2f GB)", q, out, out.stat().st_size / 1e9)
        prog.update(3 + i)
    shutil.copy(best / "picked.json", d("export", "picked.json"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
