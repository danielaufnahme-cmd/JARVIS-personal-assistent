"""Section 13: the "addressed to JARVIS?" check on the real fast model — verdicts and latency.

    uv run scripts/bench_addressed.py             # [llm] fast_model through llama-swap, budget from [manners]
    uv run scripts/bench_addressed.py --runs 3 --budget-ms 1000

Each utterance runs through `AddressCheck.decide()` exactly as jarvisd does (rules first, then the classifier);
the table shows the decision, who made it (rule / llm / fallback) and the classifier's time. The model is warmed
up first (a load is not what the 300 ms budget is about).
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from jarvis.addressed import AddressCheck, AddressClassifier  # noqa: E402
from jarvis.config import load_config  # noqa: E402
from jarvis.llm import LLM  # noqa: E402

TIME_CONTEXT = [("What's the time?", "It's 11:23, sir.")]
# (text, kind, seconds of speech, context, expected accept)
CASES = [
    ("So JARVIS is pretty good but I want you to change that thing. So when he's talking and I interrupted with his "
     "name, he should stop speaking and listen to me.", "followup", 26.5, [], False),
    ("What I was talking about and then JARVIS kind of answered the question that I was asking you to fix. And you "
     "make it so that it actually only wakes up when I'm talking about him and not unnecessarily.", "followup", 17.0,
     [], False),
    ("Jarvis, what's the time", "wake", 1.5, [], True),
    ("Hey Jarvis, set a timer for ten minutes", "wake", 2.0, [], True),
    ("What's the time?", "wake", 1.4, [], True),
    ("and in Tokyo?", "followup", 1.0, TIME_CONTEXT, True),
    ("What's the weather like tomorrow?", "followup", 2.0, TIME_CONTEXT, True),
    ("Go full screen.", "wake", 1.2, [], True),
    ("okay so the function should return early if the list is empty and then we log it", "followup", 5.5,
     TIME_CONTEXT, False),
    ("yeah I'll call you back in five minutes, I'm just finishing something", "followup", 3.5, TIME_CONTEXT, False),
    ("add a unit test for the parser and make sure it handles empty input", "followup", 4.5, TIME_CONTEXT, False),
    ("Hmm, what did I want to do next", "followup", 2.0, TIME_CONTEXT, False),
    ("Kolik je hodin v Praze?", "wake", 1.6, [], True),
    ("Thanks, that's great", "followup", 1.2, TIME_CONTEXT, True),
]


async def main_async(args: argparse.Namespace) -> int:
    cfg = load_config()
    name = args.model or cfg.llm.fast_model or cfg.llm.model
    lcfg = dataclasses.replace(cfg.llm, model=name, voice_temperature=float(cfg.llm.fast_temperature))
    llm = LLM(lcfg)
    budget = (args.budget_ms or cfg.manners.addressed_budget_ms) / 1000
    print(f"model {name}, budget {budget * 1000:.0f} ms; warming up…")
    await llm.warm_up()
    m = cfg.manners
    check = AddressCheck(AddressClassifier(llm, budget), long_speech_s=m.long_speech_s,
                         long_min_confidence=m.long_speech_min_confidence, fail_open=m.addressed_fail_open,
                         max_words_before=m.wake_max_words_before)
    llm_ms: list[float] = []
    wrong = 0
    for run in range(args.runs):
        for text, kind, secs, ctx, expected in CASES:
            d = await check.decide(text, kind=kind, duration_s=secs, context=ctx)
            ok = d.accept == expected
            wrong += not ok
            if d.source == "llm" and d.ms is not None:
                llm_ms.append(d.ms)
            ms = "" if d.ms is None else f"{d.ms:5.0f} ms"
            conf = "" if d.confidence is None else f" {d.confidence:.2f}"
            print(f"{'ok  ' if ok else 'WRONG'} run {run + 1} {kind:8s} {'yes' if d.accept else 'no ':3s} "
                  f"{d.source:8s}{conf:5s} {ms:>8s}  {text[:70]!r}  ({d.reason})")
    if llm_ms:
        print(f"classifier: median {statistics.median(llm_ms):.0f} ms, max {max(llm_ms):.0f} ms over "
              f"{len(llm_ms)} calls")
    print(f"{wrong} wrong of {len(CASES) * args.runs}")
    return 0 if wrong == 0 else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model")
    ap.add_argument("--runs", type=int, default=2)
    ap.add_argument("--budget-ms", type=int, default=0)
    return asyncio.run(main_async(ap.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
