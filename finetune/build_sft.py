"""Stage 5 (train venv): render the training examples into token ids + labels.

    finetune/.venv-train/bin/python finetune/build_sft.py

- The exact llama-server rendering (ftlib/render.py; checked byte for byte in stage 1), thinking off.
- Loss only on the assistant tokens of the example's own turn (its tool calls and its final reply, each up to and
  including <|im_end|>); system prompt, tools, history, user text and tool results are masked.
- Tool-schema robustness: in `aug_frac` of the examples the tools the example doesn't use are partly dropped, the
  list is shuffled, and some descriptions are replaced by the teacher's paraphrases (tool_paraphrases.json).
- Sequences longer than `max_len` are dropped (none expected: the prompt alone is ~5.6k tokens).
Outputs: sft_tokens.pt, heldout_prompt_tokens.pt, build_stats.json.
"""

from __future__ import annotations

import copy
import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ftlib.paths import Progress, config, d, read_jsonl, setup_logging  # noqa: E402

log = setup_logging("build")


def augment(tools: list[dict], used: set[str], paraphrases: dict[str, list[str]], rng: random.Random) -> list[dict]:
    keep = [t for t in tools if t["function"]["name"] in used]
    other = [t for t in tools if t["function"]["name"] not in used]
    drop = rng.uniform(0.1, 0.5)
    other = [t for t in other if rng.random() > drop]
    out = copy.deepcopy(keep + other)
    rng.shuffle(out)
    for t in out:
        alts = paraphrases.get(t["function"]["name"])
        if alts and rng.random() < 0.5:
            t["function"]["description"] = rng.choice(alts)
    return out


def main() -> int:
    import torch

    from ftlib.render import assistant_spans, render, tokenize_with_labels, tokenizer

    cfg = config()
    rng = random.Random(1616)
    tools = json.loads(d("format", "tools.json").read_text(encoding="utf-8"))
    tp_path = d("tool_paraphrases.json")
    paraphrases = json.loads(tp_path.read_text(encoding="utf-8")) if tp_path.is_file() else {}
    rows = read_jsonl(d("sft.jsonl"))
    prog = Progress("6/10 render + tokenize", len(rows))
    out, stats = [], {"examples": 0, "augmented": 0, "dropped_long": 0, "prefix_mismatch": 0, "label_tokens": 0,
                      "tokens": 0, "max_len": 0}
    for i, ex in enumerate(rows):
        msgs = ex["messages"]
        aug = rng.random() < cfg.aug_frac
        tl = augment(tools, set(ex["used_tools"]), paraphrases, rng) if aug else tools
        full = render(msgs, tl, False)
        prompt = render(msgs[:-1], tl, True)
        if not full.startswith(prompt):
            stats["prefix_mismatch"] += 1
            continue
        ids, labels = tokenize_with_labels(full, assistant_spans(full, 0))
        n_lab = sum(x != -100 for x in labels)
        if n_lab == 0:
            stats["prefix_mismatch"] += 1
            continue
        if len(ids) > cfg.max_len:
            stats["dropped_long"] += 1
            continue
        out.append({"id": ex["id"], "category": ex["category"], "input_ids": torch.tensor(ids, dtype=torch.int32),
                    "labels": torch.tensor(labels, dtype=torch.int32)})
        stats["examples"] += 1
        stats["augmented"] += aug
        stats["label_tokens"] += n_lab
        stats["tokens"] += len(ids)
        stats["max_len"] = max(stats["max_len"], len(ids))
        if i == 0:
            tok = tokenizer()
            shown = tok.decode([t for t, lab in zip(ids, labels, strict=True) if lab != -100])
            log.info("example %s trains on: %r", ex["id"], shown[:400])
        if i % 50 == 0:
            prog.update(i + 1)
    torch.save(out, d("sft_tokens.pt"))
    prompts = []
    for p in read_jsonl(d("heldout_prompts.jsonl")):
        text = render(p["messages"], tools, True)
        prompts.append({"id": p["id"], "category": p["category"], "label": p["label"],
                        "input_ids": tokenizer()(text, add_special_tokens=False)["input_ids"]})
    torch.save(prompts, d("heldout_prompt_tokens.pt"))
    stats["heldout_prompts"] = len(prompts)
    stats["mean_len"] = round(stats["tokens"] / max(1, stats["examples"]))
    d("build_stats.json").write_text(json.dumps(stats, indent=1), encoding="utf-8")
    prog.update(len(rows))
    log.info("built %s", json.dumps(stats))
    return 0 if stats["examples"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
