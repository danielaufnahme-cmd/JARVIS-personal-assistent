"""Stage 6 (train venv): LoRA on Qwen/Qwen3.5-2B with Unsloth (bf16 LoRA: Unsloth advises against QLoRA for
Qwen3.5), loss on the assistant tokens only, packing off, 8k sequences.

    finetune/.venv-train/bin/python finetune/train.py

GPU etiquette: waits until no fullscreen window is active and enough VRAM is free; while training it checks every
step, and when a game starts it saves a checkpoint and exits with code 75 (run.sh waits, polling every 60 s, and
starts it again; it resumes from the last checkpoint). A CUDA out-of-memory also exits 75 and resumes.
After each epoch it scores the first model call of the held-out single-turn items (greedy) and keeps the best
adapter in checkpoints/best. Exit 0 = done.
"""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

os.environ.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")
os.environ.setdefault("UNSLOTH_DISABLE_STATISTICS", "1")
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
os.environ.setdefault("OMP_NUM_THREADS", "4")
os.environ.setdefault("MKL_NUM_THREADS", "4")

from ftlib import guard  # noqa: E402
from ftlib.paths import BASE_HF, SMOKE, Progress, config, d, setup_logging, write_status  # noqa: E402

log = setup_logging("train")
PAUSED = 75
# QLoRA (4-bit base) by default: with the resident 4B, the desktop and the 2.5 GB JARVIS reserve, ~4.4 GB is left, and a
# bf16 LoRA needs ~6 GB (smoke: 5.8 GB). FT_TRAIN_MODE=bf16 if the GPU has the room. The choice is kept for resumes.
NEED_BY_MODE = {"qlora": 4200, "bf16": 7000}  # 10k-token sequences
TARGETS = ["q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj", "down_proj",  # full attention + MLP
           "in_proj_qkv", "in_proj_z", "out_proj"]                                          # Gated DeltaNet layers


# --- per-epoch scoring of the first model call ------------------------------------------------------------------------

def parse_calls(text: str) -> list[tuple[str, dict]]:
    calls = []
    for m in re.finditer(r"<tool_call>\s*<function=([^>\s]+)>(.*?)</function>", text, re.S):
        args = {}
        for p in re.finditer(r"<parameter=([^>\s]+)>\n?(.*?)\n?</parameter>", m.group(2), re.S):
            v = p.group(2)
            try:
                v = json.loads(v)
            except (json.JSONDecodeError, ValueError):
                pass
            args[p.group(1)] = v
        calls.append((m.group(1), args))
    return calls


def fold(s: object) -> str:
    import unicodedata

    s = unicodedata.normalize("NFKD", str(s).lower())
    return "".join(ch for ch in s if not unicodedata.combining(ch))


def first_call_ok(label: dict, calls: list[tuple[str, dict]]) -> bool:
    """The first model call alone: the right tool (or an allowed first lookup) with the checked arguments, or no
    call when none is expected. Recipient checks need the contacts file and are skipped here."""
    expect = label.get("expect", [])
    main = [n for n in expect if n]
    also = set(label.get("also_ok", []))
    if not main:
        return not calls or (bool(label.get("lookups_ok")) and all(n in also for n, _ in calls))
    if not calls:
        return "" in expect
    name, args = calls[0]
    if name in also and name not in main:
        return True
    if name not in main:
        return False
    chk = label.get("check") or {}
    for k, want in (chk.get("eq") or {}).items():
        try:
            if isinstance(want, (int, float)) and float(args.get(k)) != float(want):
                return False
        except (TypeError, ValueError):
            return False
        if not isinstance(want, (int, float)) and fold(args.get(k)) != fold(want):
            return False
    for k, alts in (chk.get("has") or {}).items():
        if not any(fold(a) in fold(args.get(k, "")) for a in alts):
            return False
    for k, alts in (chk.get("in") or {}).items():
        if fold(args.get(k, "")) not in [fold(a) for a in alts]:
            return False
    for k in chk.get("nonempty") or []:
        if not str(args.get(k) or "").strip():
            return False
    if chk.get("any_value_has"):
        blob = fold(" ".join(str(v) for v in args.values()))
        if not any(fold(a) in blob for a in chk["any_value_has"]):
            return False
    return True


def epoch_eval(model, tokenizer, prompts: list[dict], n: int) -> dict | None:  # noqa: ANN001
    import torch
    from unsloth import FastLanguageModel

    FastLanguageModel.for_inference(model)
    ok = 0
    rows = []
    t0 = time.monotonic()
    try:
        for i, p in enumerate(prompts[:n]):
            if guard.blocked(0, running=True):
                return None  # a game started: the caller checkpoints, exits, and scores after the resume
            ids = torch.tensor([p["input_ids"]], device="cuda")
            with torch.no_grad():
                out = model.generate(input_ids=ids, attention_mask=torch.ones_like(ids), max_new_tokens=160,
                                     do_sample=False, pad_token_id=tokenizer.eos_token_id)
            text = tokenizer.decode(out[0, ids.shape[1]:], skip_special_tokens=False)
            text = text.split("<|im_end|>")[0]
            good = first_call_ok(p["label"], parse_calls(text))
            ok += good
            rows.append({"id": p["id"], "ok": good, "out": text[:300]})
    finally:
        FastLanguageModel.for_training(model)
    n_done = len(rows)
    return {"first_call_acc": round(ok / max(1, n_done), 4), "items": n_done, "s": round(time.monotonic() - t0),
            "fails": [r for r in rows if not r["ok"]][:40]}


# --- training ------------------------------------------------------------------------------------------------------------

def last_checkpoint(out: Path) -> Path | None:
    cps = sorted(out.glob("checkpoint-*"), key=lambda p: int(p.name.split("-")[1]))
    for cp in reversed(cps):
        if (cp / "trainer_state.json").is_file():
            return cp
    return None


def train_mode() -> str:
    p = d("train_mode.json")
    if p.is_file():
        return json.loads(p.read_text())["mode"]
    mode = os.environ.get("FT_TRAIN_MODE", "qlora")
    p.write_text(json.dumps({"mode": mode}))
    return mode


def main() -> int:
    mode = train_mode()
    need = int(os.environ.get("FT_TRAIN_NEED_MB", NEED_BY_MODE[mode]))
    ram_need = int(os.environ.get("FT_TRAIN_RAM_MB", {"qlora": 4500, "bf16": 5500}[mode]))
    after = d("ram_paused")
    guard.wait_resources(need, f"training ({mode})", ram_mb=ram_need, after_ram_pause=after.is_file())
    after.unlink(missing_ok=True)
    t0 = time.monotonic()
    try:
        return _main()
    finally:
        # training time across pause/resume sessions (the report sums it)
        p = d("train_time.json")
        spent = json.loads(p.read_text()) if p.is_file() else {"sessions": []}
        spent["sessions"].append(round(time.monotonic() - t0))
        spent["total_s"] = sum(spent["sessions"])
        p.write_text(json.dumps(spent))


def _main() -> int:
    cfg = config()
    import torch

    # CPU headroom: 4 threads for the trainer (the desktop keeps the rest; the unit's CPUQuota caps it too)
    torch.set_num_threads(int(os.environ.get("FT_TORCH_THREADS", "4")))
    mode = train_mode()
    # Cap our allocations so JARVIS always keeps its reserve: budget = what is free now - the reserve - ~0.5 GB for
    # our CUDA context (not counted by the allocator cap).
    free = guard.free_vram_mb() or 0
    total = torch.cuda.get_device_properties(0).total_memory / 2**20
    budget = free - guard.RESERVE_MB - 500
    torch.cuda.set_per_process_memory_fraction(max(0.05, budget / total))
    log.info("mode %s; %d MiB free, the JARVIS reserve %d MiB -> allocator cap %d MiB", mode, free, guard.RESERVE_MB,
             budget)
    from transformers import Trainer, TrainerCallback, TrainingArguments
    from unsloth import FastLanguageModel

    out_dir = d("checkpoints", "x").parent
    best_dir = out_dir / "best"
    evals_path = d("epoch_evals.json")
    evals = json.loads(evals_path.read_text()) if evals_path.is_file() else {}
    rows = torch.load(d("sft_tokens.pt"))
    prompts = torch.load(d("heldout_prompt_tokens.pt"))
    log.info("%d training examples, %d held-out prompts", len(rows), len(prompts))

    model, tokenizer = FastLanguageModel.from_pretrained(model_name=str(BASE_HF), max_seq_length=cfg.max_len,
                                                         load_in_4bit=mode == "qlora", load_in_16bit=mode == "bf16",
                                                         full_finetuning=False, dtype=torch.bfloat16)
    names = {n.rsplit(".", 1)[-1] for n, _ in model.named_modules()}
    targets = [t for t in TARGETS if t in names]
    log.info("LoRA targets present: %s", targets)
    model = FastLanguageModel.get_peft_model(model, r=cfg.lora_r, lora_alpha=cfg.lora_alpha, lora_dropout=0.0,
                                             target_modules=targets, bias="none",
                                             use_gradient_checkpointing="unsloth", random_state=3407,
                                             # never adapt the vision tower (text-only use)
                                             exclude_modules=r".*visual.*")
    tok = getattr(tokenizer, "tokenizer", tokenizer)

    class DS(torch.utils.data.Dataset):
        def __len__(self) -> int:
            return len(rows)

        def __getitem__(self, i: int) -> dict:
            return {"input_ids": rows[i]["input_ids"].long(), "labels": rows[i]["labels"].long()}

    def collate(batch: list[dict]) -> dict:
        assert len(batch) == 1
        ids = batch[0]["input_ids"].unsqueeze(0)
        return {"input_ids": ids, "labels": batch[0]["labels"].unsqueeze(0), "attention_mask": torch.ones_like(ids)}

    steps_per_epoch = math.ceil(len(rows) / cfg.grad_accum)
    total_steps = cfg.max_steps if cfg.max_steps > 0 else steps_per_epoch * cfg.epochs
    prog = Progress("7/10 training", total_steps)
    state = {"paused": None, "t0": time.monotonic(), "losses": [], "term": False}

    import signal

    def on_term(signum, frame):  # noqa: ANN001, ANN202 - systemctl stop / Ctrl-C: checkpoint at the next step, then exit
        state["term"] = True

    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGINT, on_term)

    class Guard(TrainerCallback):
        def _check(self, st, control, at_step: bool):  # noqa: ANN001, ANN202
            """GPU + RAM, every micro-step (~5 s): a soft RAM shortage waits here (no new work), anything harder
            checkpoints and exits (75; run.sh waits for the resources and resumes)."""
            if state["term"]:
                why = "stopped by the user"
            else:
                why = guard.blocked(0, running=True)
                t_soft = time.monotonic()
                while why and guard.RAM_WORDS in why and guard.ram_level()[0] == "soft" and not state["term"]:
                    if time.monotonic() - t_soft > 300:
                        why += "; still short after 5 min, releasing the trainer's memory"
                        break  # waiting while holding ~5 GB doesn't help: checkpoint and free it
                    log.info("paused: %s (training waits)", why)
                    guard._status_note(f"paused: {why}")
                    time.sleep(5)
                    why = guard.blocked(0, running=True)
                if why is None:
                    guard._status_note("")
            fake = os.environ.get("FT_FAKE_PAUSE_AT_STEP")  # tests the pause/resume path (one-shot)
            if at_step and fake and st.global_step == int(fake) and not d("fake_pause.done").is_file():
                d("fake_pause.done").write_text("1")
                why = "test pause (FT_FAKE_PAUSE_AT_STEP)"
            if why:
                log.info("paused: %s; saving a checkpoint at step %d and exiting", why, st.global_step)
                state["paused"] = why
                if guard.RAM_WORDS in why:
                    d("ram_paused").write_text(why)
                control.should_save = True
                control.should_training_stop = True
            return control

        def on_step_end(self, args, st, control, **kw):  # noqa: ANN001, ANN201
            prog.update(st.global_step, note=f"epoch {st.epoch:.2f}")
            return self._check(st, control, True)

        def on_substep_end(self, args, st, control, **kw):  # noqa: ANN001, ANN201
            return self._check(st, control, False)

        def on_log(self, args, st, control, logs=None, **kw):  # noqa: ANN001, ANN201
            if logs and "loss" in logs:
                state["losses"].append({"step": st.global_step, "loss": round(logs["loss"], 4),
                                        "lr": logs.get("learning_rate")})
                log.info("step %d/%d loss %.4f", st.global_step, total_steps, logs["loss"])

        def on_train_begin(self, args, st, control, model=None, **kw):  # noqa: ANN001, ANN201
            pend = d("pending_eval.json")
            if pend.is_file():
                ep = json.loads(pend.read_text())["epoch"]
                if ep not in evals:
                    log.info("scoring epoch %s, interrupted by a pause before", ep)
                    self._score(ep, st.global_step, model)
                pend.unlink(missing_ok=True)
            return control

        def on_epoch_end(self, args, st, control, model=None, **kw):  # noqa: ANN001, ANN201
            ep = str(round(st.epoch))
            if state["paused"] or ep in evals or cfg.max_steps > 0:
                return control
            if not self._score(ep, st.global_step, model):
                d("pending_eval.json").write_text(json.dumps({"epoch": ep}))
                log.info("paused: game running (during the epoch %s score); checkpointing and exiting", ep)
                state["paused"] = "game running"
                control.should_save = True
                control.should_training_stop = True
            return control

        def _score(self, ep: str, step: int, model) -> bool:  # noqa: ANN001
            write_status(f"stage 7/10 training: epoch {ep} done, scoring held-out first calls")
            res = epoch_eval(model, tok, prompts, cfg.epoch_eval_items)
            if res is None:
                return False
            res["step"] = step
            evals[ep] = res
            evals_path.write_text(json.dumps(evals, indent=1))
            log.info("epoch %s: held-out first-call accuracy %.1f %% (%d items, %d s)", ep,
                     100 * res["first_call_acc"], res["items"], res["s"])
            best = max(evals.values(), key=lambda e: (e["first_call_acc"], e["step"]))
            if best is res:
                if best_dir.exists():
                    shutil.rmtree(best_dir)
                model.save_pretrained(str(best_dir))
                tok.save_pretrained(str(best_dir))
                (best_dir / "picked.json").write_text(json.dumps({"epoch": ep, **{k: v for k, v in res.items()
                                                                                  if k != "fails"}}))
                log.info("best adapter so far: epoch %s -> %s", ep, best_dir)
            return True

    targs = TrainingArguments(
        output_dir=str(out_dir), per_device_train_batch_size=1, gradient_accumulation_steps=cfg.grad_accum,
        num_train_epochs=cfg.epochs, max_steps=cfg.max_steps, learning_rate=cfg.lr, lr_scheduler_type="cosine",
        warmup_steps=max(1, int(0.04 * total_steps)), weight_decay=0.0, logging_steps=1 if SMOKE else 5,
        save_strategy="steps", save_steps=cfg.save_steps, save_total_limit=2, bf16=True, optim="adamw_8bit",
        seed=3407, report_to="none", dataloader_num_workers=0, remove_unused_columns=False,
        max_grad_norm=1.0)
    trainer = Trainer(model=model, args=targs, train_dataset=DS(), data_collator=collate, callbacks=[Guard()])
    resume = last_checkpoint(out_dir)
    if resume:
        log.info("resuming from %s", resume)
    try:
        trainer.train(resume_from_checkpoint=str(resume) if resume else None)
    except torch.cuda.OutOfMemoryError:
        log.warning("CUDA out of memory (another program took the VRAM?); will resume from the last checkpoint")
        return PAUSED
    if state["term"]:
        return 143
    if state["paused"]:
        return PAUSED
    peak = round(torch.cuda.max_memory_allocated() / 2**20)
    if cfg.max_steps > 0 or not best_dir.exists():
        # smoke / no epoch finished an eval: score the final adapter once and keep it
        res = epoch_eval(model, tok, prompts, cfg.epoch_eval_items)
        res["step"] = trainer.state.global_step
        evals["final"] = res
        evals_path.write_text(json.dumps(evals, indent=1))
        if best_dir.exists():
            shutil.rmtree(best_dir)
        model.save_pretrained(str(best_dir))
        tok.save_pretrained(str(best_dir))
        (best_dir / "picked.json").write_text(json.dumps({"epoch": "final", "first_call_acc": res["first_call_acc"]}))
    summary = {"mode": mode, "allocator_cap_mb": budget, "examples": len(rows), "steps": trainer.state.global_step, "epochs": cfg.epochs,
               "max_steps": cfg.max_steps, "lora_r": cfg.lora_r, "lora_alpha": cfg.lora_alpha, "lr": cfg.lr,
               "grad_accum": cfg.grad_accum, "max_len": cfg.max_len, "targets": targets,
               "peak_alloc_mb": peak, "train_seconds_this_session": round(time.monotonic() - state["t0"]),
               "losses": state["losses"][-200:], "epoch_evals": {k: {kk: vv for kk, vv in v.items() if kk != "fails"}
                                                                 for k, v in evals.items()},
               "picked": json.loads((best_dir / "picked.json").read_text())}
    tt = d("train_time.json")
    before = json.loads(tt.read_text())["total_s"] if tt.is_file() else 0
    summary["train_seconds_total"] = before + summary["train_seconds_this_session"]
    d("train_summary.json").write_text(json.dumps(summary, indent=1))
    log.info("training done: %d steps, peak %d MiB allocated, best: %s", trainer.state.global_step, peak,
             summary["picked"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
