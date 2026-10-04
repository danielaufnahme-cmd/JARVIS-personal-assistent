"""Real use: per-turn timings from the jarvisd and llama-swap journals (read-only).

    uv run python bench/voice_model_compare/analyze_journal.py [--since "2 days ago"]

For every transcript jarvisd acted on ("heard ..." followed by a reply, a filler or a fallback before the next
"heard"), it collects: the fast model active at that moment (from "jarvisd ready (... voice brain fast: X" and
"fast model: X -> Y"), the voice latency line (end of speech -> first audio), the "One moment" filler, a 35B
fallback, and the llama-swap requests that ran during the turn (count, longest, 35B loads).
Writes results/real_turns.json and prints a summary per fast model.
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import subprocess
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent


def journal(unit: str, since: str) -> list[tuple[datetime, str]]:
    out = subprocess.run(["journalctl", "--user", "-u", unit, "--since", since, "--no-pager", "-o", "short-iso"],
                         capture_output=True, text=True).stdout
    rows = []
    for line in out.splitlines():
        if "hostapi/alsa" in line:
            continue
        m = re.match(r"^(\S+)\s+\S+\s+\S+:\s(.*)$", line)
        if not m:
            continue
        try:
            t = datetime.fromisoformat(m.group(1))
        except ValueError:
            continue
        rows.append((t, m.group(2)))
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2 days ago")
    a = ap.parse_args()
    jd = journal("jarvisd", a.since)
    ls = journal("llama-swap", a.since)
    reqs = []
    for t, msg in ls:
        m = re.search(r'"POST /v1/chat/completions HTTP/1.1" (\d+) (\d+) "[^"]*" ([\d.]+)(µs|ms|s)', msg)
        if m:
            v = float(m.group(3)) * {"µs": 1e-6, "ms": 1e-3, "s": 1.0}[m.group(4)]
            reqs.append((t, v, int(m.group(2))))
    loads = [(t, re.search(r"<(\S+)>", msg).group(1)) for t, msg in ls if "Health check passed" in msg]
    reloads = [t for t, msg in ls if "reloading configuration" in msg]

    model = "?"
    turns = []
    cur = None
    for t, msg in jd:
        m = re.search(r"voice brain fast: ([\w.-]+)", msg)
        if m:
            model = m.group(1).rstrip(",")
        m = re.search(r"fast model: (\S+) -> (\S+)", msg)
        if m:
            model = m.group(2)
        if "jarvis.voice: heard" in msg:
            if cur:
                turns.append(cur)
            text = re.search(r"\): (.*)$", msg)
            cur = {"t": t, "model": model, "text": (text.group(1) if text else msg)[:160], "latency": None,
                   "filler": False, "fallback": None, "answered": False}
            continue
        if cur is None:
            continue
        if "voice latency:" in msg:
            cur["latency"] = {k: float(v) for k, v in re.findall(r"(\w+)=([\d.]+)", msg)}
            cur["answered"] = True
        elif "no answer" in msg and "filler" in msg:
            cur["filler"] = True
            cur["answered"] = True
        elif "fast model fallback" in msg:
            cur["fallback"] = msg.split("fast model fallback", 1)[1][:160]
            cur["answered"] = True
        elif "deep route" in msg:
            cur["deep"] = True
        elif "agent turn failed" in msg:
            cur["failed"] = True
    if cur:
        turns.append(cur)
    turns = [x for x in turns if x["answered"]]
    for i, x in enumerate(turns):
        end = turns[i + 1]["t"] if i + 1 < len(turns) else None
        rs = [(tt, v, n) for tt, v, n in reqs if tt >= x["t"] and (end is None or tt < end)
              and (tt - x["t"]).total_seconds() < 120]
        x["llm_requests"] = len(rs)
        x["llm_longest_s"] = round(max((v for _, v, _ in rs), default=0.0), 2)
        x["llm_sum_s"] = round(sum(v for _, v, _ in rs), 2)
        x["loads_35b"] = sum(1 for tt, mm in loads if mm == "jarvis" and tt >= x["t"] and (end is None or tt < end)
                             and (tt - x["t"]).total_seconds() < 120)
        x["config_reload_near"] = any(abs((tt - x["t"]).total_seconds()) < 180 for tt in reloads)
        # the end of the turn's LLM work = the last request finishing within the turn window
        x["llm_done_after_s"] = round(max(((tt - x["t"]).total_seconds() for tt, _, _ in rs), default=0.0), 1)
        x["t"] = x["t"].isoformat()
    (HERE / "results" / "real_turns.json").write_text(json.dumps(turns, indent=1, ensure_ascii=False))

    def med(v: list[float]) -> float | None:
        return round(statistics.median(v), 2) if v else None

    by: dict[str, list[dict]] = {}
    for x in turns:
        by.setdefault(x["model"], []).append(x)
    for m, xs in by.items():
        lat = [x["latency"]["total_s"] for x in xs if x["latency"]]
        llm = [x["latency"].get("llm_first_sentence_s") for x in xs if x["latency"]
               and x["latency"].get("llm_first_sentence_s") is not None]
        print(f"== {m}: {len(xs)} answered turns ({xs[0]['t'][:16]} .. {xs[-1]['t'][:16]})")
        print(f"   end of speech -> first audio: median {med(lat)} s, p90 "
              f"{round(sorted(lat)[int(0.9 * (len(lat) - 1))], 2) if lat else None} s (n={len(lat)}); "
              f"LLM first sentence median {med(llm)} s")
        print(f"   filler said: {sum(x['filler'] for x in xs)}, fallbacks to the 35B: "
              f"{sum(1 for x in xs if x['fallback'])}, turns that loaded the 35B: {sum(1 for x in xs if x['loads_35b'])}")
        print(f"   LLM requests per turn: median {med([x['llm_requests'] for x in xs])}, "
              f"LLM work done after median {med([x['llm_done_after_s'] for x in xs])} s")
        for x in xs:
            if x["fallback"] or x["loads_35b"] or (x["latency"] and x["latency"]["total_s"] > 4) or x["filler"]:
                print(f"   {x['t'][11:19]} lat={x['latency'] and x['latency']['total_s']} filler={int(x['filler'])} "
                      f"req={x['llm_requests']} longest={x['llm_longest_s']}s 35Bload={x['loads_35b']} "
                      f"fb={'Y' if x['fallback'] else '-'} | {x['text'][:90]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
