"""Where everything lives, the run configuration, the STATUS line and logging.

FT_DATA (set by run.sh) is finetune/data for the real run and finetune/data-smoke for --smoke, so a smoke run
never touches the real run's files and is deleted with one `rm -r`.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

FT = Path(__file__).resolve().parent.parent          # ~/jarvis/finetune
ROOT = FT.parent                                     # ~/jarvis
DATA = Path(os.environ.get("FT_DATA", FT / "data"))
SMOKE = os.environ.get("FT_SMOKE", "0") == "1"
BASE_HF = FT / "data" / "base" / "Qwen3.5-2B"        # shared by the smoke and the real run (4.3 GB, downloaded once)
MODELS = Path.home() / "models"
LLAMA_SRC = Path.home() / ".local" / "src" / "llama.cpp"
LLAMA_SERVER = Path.home() / ".local" / "bin" / "llama-server"
LLAMA_QUANTIZE = LLAMA_SRC / "build" / "bin" / "llama-quantize"
TEACHER_GGUF = Path(os.environ.get("FT_TEACHER_GGUF", MODELS / "Qwen3.6-35B-A3B-UD-IQ4_XS.gguf"))  # override: tests
BASE_2B_GGUF = MODELS / "Qwen3.5-2B-UD-Q4_K_XL.gguf"
FOURB_GGUF = MODELS / "Qwen3.5-4B-UD-Q4_K_XL.gguf"
LLAMA_SWAP = "http://127.0.0.1:8401"

# Private ports (never 3000, 8085, 8086, 8090, 8099, 11434 and not llama-swap's 8401 / its 5800+ upstreams).
TEACHER_PORT = 8437
EVAL_PORT = 8438
FORMAT_PORT = 8439


@dataclass(frozen=True)
class RunConfig:
    train_requests: int = 3000       # turns in the training request set
    heldout_items: int = 300         # held-out items (+ section 12's 30 bench cases, eval only)
    paraphrase_frac: float = 0.55    # share of templated requests rewritten by the 35B
    teacher_parallel: int = 2        # concurrent sessions against the teacher server (-np); CPU headroom
    teacher_attempts: int = 3        # 1st thinking off, then thinking on
    epochs: int = 2
    max_steps: int = -1              # >0 overrides epochs (smoke)
    lora_r: int = 32
    lora_alpha: int = 64
    lr: float = 1e-4
    grad_accum: int = 8
    max_len: int = 10240             # the system prompt + 47 tools is 7.3k tokens; the longest traces ~9.8k
    aug_frac: float = 0.2            # tool-schema robustness augmentation
    epoch_eval_items: int = 120      # held-out first-call items scored after each epoch (checkpoint pick)
    eval_runs: int = 3
    eval_items: int = 0              # 0 = all held-out + bench; >0 = cap (smoke)
    quants: tuple[str, ...] = ("Q4_K_M", "Q5_K_M")
    save_steps: int = 40


SMOKE_CONFIG = RunConfig(train_requests=20, heldout_items=14, paraphrase_frac=0.5, teacher_parallel=2,
                         teacher_attempts=2, epochs=1, max_steps=10, grad_accum=1, epoch_eval_items=5,
                         eval_runs=1, eval_items=20, save_steps=5)


def config() -> RunConfig:
    """The run's settings; FT_EPOCHS / FT_TRAIN_REQUESTS / FT_EVAL_RUNS / FT_TEACHER_PARALLEL override them."""
    import dataclasses

    cfg = SMOKE_CONFIG if SMOKE else RunConfig()
    over = {}
    for env, key in (("FT_EPOCHS", "epochs"), ("FT_TRAIN_REQUESTS", "train_requests"), ("FT_EVAL_RUNS", "eval_runs"),
                     ("FT_TEACHER_PARALLEL", "teacher_parallel")):
        if os.environ.get(env):
            over[key] = int(os.environ[env])
    return dataclasses.replace(cfg, **over) if over else cfg


def d(*parts: str) -> Path:
    p = DATA.joinpath(*parts)
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def final_gguf(quant: str) -> Path:
    """The deliverable: ~/models/Qwen3.5-2B-jarvis-<quant>.gguf (smoke: inside the smoke data dir)."""
    if SMOKE:
        return d("export", f"Qwen3.5-2B-jarvis-{quant}.gguf")
    return MODELS / f"Qwen3.5-2B-jarvis-{quant}.gguf"


# --- logging + the one-line STATUS file -----------------------------------------------------------------------

STATUS = FT / "STATUS"


def setup_logging(name: str) -> logging.Logger:
    logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                        format="%(asctime)s %(levelname)s " + name + ": %(message)s", datefmt="%H:%M:%S")
    for noisy in ("httpx", "httpcore", "httpx2", "httpcore2", "openai", "urllib3", "filelock"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return logging.getLogger(name)


class Progress:
    """Writes `finetune/STATUS`: one line with the stage, done/total, % and an ETA from the running rate."""

    def __init__(self, stage: str, total: int, done: int = 0) -> None:
        self.stage, self.total, self.start_done = stage, max(1, total), done
        self.done = done
        self.t0 = time.monotonic()
        self.note = ""
        self.write()

    def update(self, done: int | None = None, note: str | None = None) -> None:
        if done is not None:
            self.done = done
        if note is not None:
            self.note = note
        self.write()

    def eta(self) -> str:
        rate_n = self.done - self.start_done
        if rate_n <= 0:
            return "?"
        left = (time.monotonic() - self.t0) / rate_n * (self.total - self.done)
        h, m = divmod(int(left) // 60, 60)
        return f"{h}h{m:02d}m" if h else f"{m}m"

    def write(self) -> None:
        pct = 100 * self.done / self.total
        line = (f"{time.strftime('%Y-%m-%d %H:%M')} {'[smoke] ' if SMOKE else ''}stage {self.stage}: "
                f"{self.done}/{self.total} ({pct:.0f}%), ETA {self.eta()}" + (f", {self.note}" if self.note else ""))
        write_status(line)


def write_status(line: str) -> None:
    tmp = STATUS.with_suffix(".tmp")
    tmp.write_text(line.rstrip() + "\n", encoding="utf-8")
    tmp.replace(STATUS)


def read_jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue  # a line cut by a kill mid-write; resumable stages redo it
    return out


def write_jsonl(path: Path, rows: list[dict]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    tmp.replace(path)


def append_jsonl(path: Path, row: dict) -> None:
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        fh.flush()


def dump_config() -> dict:
    return asdict(config())
