"""Section 15 dry run: prepare a coding job exactly as jarvisd would, but in a scratch folder and without
unloading, loading or launching anything. Prints the terminal command, opencode's argv and the project-local
opencode.json it wrote, plus the read-only preflight (fullscreen game, VRAM, Ollama).

    uv run scripts/coding_dry_run.py SCRATCH_DIR ["what to build"] [--name NAME] [--validate]

--validate runs `opencode debug config` in the scratch project (with opencode's data/state/cache dirs pointed into
SCRATCH_DIR, so the user's opencode state is untouched) and prints the resolved model and permissions.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jarvis.config import load_config  # noqa: E402
from jarvis.integrations.coding_jobs import CodingJobs, resolve_opencode  # noqa: E402


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("scratch", type=Path)
    ap.add_argument("description", nargs="?", default="a snake game in Python with a high-score table")
    ap.add_argument("--name", default="snake game")
    ap.add_argument("--validate", action="store_true")
    args = ap.parse_args()

    cfg = load_config()
    scratch = args.scratch.resolve()

    async def no_launch(argv: list[str]) -> None:
        raise AssertionError("dry run: nothing is launched")

    jobs = CodingJobs(None, cfg, launcher=no_launch, transport=None, root=scratch / "Projects",
                      state=scratch / "state")
    import httpx

    jobs._transport = httpx.AsyncHTTPTransport()  # read-only preflight (GET /api/ps) against the real Ollama
    check = await jobs.preflight()
    plan = jobs.plan(args.description, args.name)
    info = jobs.dry_run({k: plan[k] for k in ("name", "slug", "folder", "task")})

    print("== preflight (read-only)")
    print(json.dumps(check, indent=2))
    print("\n== terminal command")
    print(shlex.join(info["terminal_command"]))
    print("\n== opencode argv (run by bin/jarvis-coding-run from the spec file; no shell)")
    print(shlex.join(info["opencode_argv"]))
    print(f"\n== {info['folder']}/opencode.json")
    print(json.dumps(info["opencode_json"], indent=2))

    if args.validate:
        env = dict(os.environ)
        for var, sub in (("XDG_DATA_HOME", "oc-data"), ("XDG_STATE_HOME", "oc-state"), ("XDG_CACHE_HOME", "oc-cache")):
            env[var] = str(scratch / sub)
        out = subprocess.run([resolve_opencode(cfg.coding.opencode_bin), "debug", "config"], cwd=info["folder"],
                             env=env, capture_output=True, text=True, timeout=120)
        print("\n== opencode debug config (exit %d)" % out.returncode)
        try:
            resolved = json.loads(out.stdout)
            print(json.dumps({k: resolved.get(k) for k in ("model", "small_model", "default_agent", "share",
                                                           "permission")}, indent=2))
            print("provider.ollama.models:", sorted((resolved.get("provider", {}).get("ollama", {})
                                                     .get("models", {})).keys()))
        except ValueError:
            print(out.stdout[-3000:], out.stderr[-3000:])
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
