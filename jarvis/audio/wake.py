"""Wake word: openWakeWord on 80 ms frames, with a threshold, a refractory period and score logging."""

from __future__ import annotations

import inspect
import re
import logging
import time
import warnings
from pathlib import Path

import numpy as np

from ..config import REPO_ROOT

log = logging.getLogger(__name__)

WAKEWORD_DIR = REPO_ROOT / "wakeword"


def resolve_model(name: str) -> Path:
    """`[wake] model`: a path to an .onnx, `wakeword/<name>.onnx`, or a model bundled with openWakeWord."""
    candidates = [Path(name).expanduser()]
    if not name.endswith(".onnx"):
        candidates.append(WAKEWORD_DIR / f"{name}.onnx")
    candidates.append(REPO_ROOT / name)
    for c in candidates:
        if c.is_file():
            return c.resolve()
    import openwakeword

    bundled = Path(openwakeword.__file__).parent / "resources" / "models"
    hits = sorted(bundled.glob(f"{name}_v*.onnx")) or sorted(bundled.glob(f"{name}.onnx"))
    if hits:
        return hits[-1]
    raise FileNotFoundError(f"wake model {name!r} not found (tried {', '.join(map(str, candidates))}, {bundled})")


def parse_models(spec: str, default_threshold: float) -> list[tuple[str, float]]:
    """`[wake] model`: one name, or several with their own thresholds, e.g. "jarvis:0.06, hey_jarvis:0.5".

    Running several models shares openWakeWord's feature extraction, so a second model is nearly free. The
    trained single-word "jarvis" model is best on a bare "Jarvis"; the bundled hey_jarvis on "Hey Jarvis".
    """
    models = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        name, sep, thr = part.rpartition(":")
        try:
            models.append((name.strip(), float(thr)) if sep else (part, default_threshold))
        except ValueError:  # a ":" that isn't a threshold, e.g. in a path
            models.append((part, default_threshold))
    if not models:
        raise ValueError("[wake] model is empty")
    return models


def _load_oww(paths: list[Path]):  # noqa: ANN202 - openwakeword has no types
    from openwakeword.model import Model

    params = inspect.signature(Model.__init__).parameters
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message=".*CUDAExecutionProvider.*")  # it asks for CUDA; CPU is right
        if "wakeword_models" in params:  # openWakeWord >= 0.5
            return Model(wakeword_models=[str(p) for p in paths], inference_framework="onnx")
        return Model(wakeword_model_paths=[str(p) for p in paths])


class WakeWord:
    def __init__(self, model: str, threshold: float = 0.5, refractory_s: float = 2.0, log_min_score: float = 0.3,
                 scorer=None) -> None:  # noqa: ANN001 - scorer: frame -> score (or {model: score}), for tests
        self.models = parse_models(model, threshold)
        self.threshold = min(t for _, t in self.models)
        self.refractory_s = refractory_s
        self.log_min_score = log_min_score
        self.name = "+".join(n for n, _ in self.models)
        self._last_fire = -1e9
        self._oww = None
        self._thresholds: dict[str, float] = {}
        if scorer is None:
            paths = [resolve_model(n) for n, _ in self.models]
            self._oww = _load_oww(paths)
            # openWakeWord keys its models by file stem, in load order.
            self._thresholds = dict(zip(self._oww.models, (t for _, t in self.models), strict=True))
            for (n, t), p in zip(self.models, paths, strict=True):
                log.info("wake word model %s (%s), threshold %.2f", n, p, t)
            scorer = self._score_oww
        self._scorer = scorer
        self.last_score = 0.0

    def _score_oww(self, frame: np.ndarray) -> dict[str, float]:
        assert self._oww is not None
        scores = self._oww.predict(frame)
        return {k: float(scores.get(k, 0.0)) for k in self._thresholds}

    def process(self, frame: np.ndarray, now: float | None = None, scale: float = 1.0) -> bool:
        """Feed one 80 ms int16 frame. True once per detection (then quiet for `refractory_s`). `scale` multiplies
        every threshold (below 1 while JARVIS is speaking)."""
        raw = self._scorer(frame)
        scores = raw if isinstance(raw, dict) else {self.name: float(raw)}
        thresholds = self._thresholds or {self.name: self.threshold}
        fired = {k: v for k, v in scores.items() if v >= thresholds.get(k, self.threshold) * scale}
        self.last_score = max(fired.values()) if fired else max(scores.values(), default=0.0)
        now = time.monotonic() if now is None else now
        for k, v in scores.items():
            if v >= self.log_min_score:
                log.debug("wake score %s %.3f", k, v)
        if not fired:
            return False
        if now - self._last_fire < self.refractory_s:
            return False
        self._last_fire = now
        which = max(fired, key=fired.get)
        log.info("wake word %s detected (score %.3f)", which, fired[which])
        return True

    def reset(self) -> None:
        if self._oww is not None:
            self._oww.reset()


# --- second-stage verification ---------------------------------------------------------------------------

_WORD = re.compile(r"[a-z]+")


def wake_match(text: str, word: str = "jarvis") -> float:
    """How well a transcript contains the wake word: the best fuzzy ratio (0-100) of any word, or two adjacent
    words run together ("jar vis"), against `word`.

    Word-level on purpose: a substring match (partial_ratio) scores "jars" 86 and "Travis" 80. Here "jarvis" /
    "hey jarvis" score 100, "jervis" / "jarves" / "marvis" 83, "hey javis" 91, while "jars" 80, "Travis" 67,
    "service" / "nervous" / "customer service" 46 and ordinary dialogue < 50.
    """
    from rapidfuzz import fuzz

    words = _WORD.findall(text.lower())
    candidates = words + [a + b for a, b in zip(words, words[1:], strict=False)]
    return max((fuzz.ratio(w, word) for w in candidates), default=0.0)


def wake_verified(text: str, min_ratio: float = 82.0) -> tuple[bool, float]:
    score = wake_match(text)
    return score >= min_ratio, score
