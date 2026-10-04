"""Typed REPL for the agent: `uv run python -m jarvis.cli_text [--base-url URL] [--model NAME]`.

Type what you'd say. Draft cards and bus events are printed as they happen. Extra commands:
  /confirm   click Confirm on the current card (the UI path, with the card's id)
  /cancel    click Cancel on the current card
  /reset     start a new session (clear history)
  /quit      exit (Ctrl-D works too)
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import logging
import sys
import textwrap
from pathlib import Path
from typing import Any

from jarvis.agent import Agent
from jarvis.config import load_config
from jarvis.events import Bus, Event
from jarvis.gate import ApprovalGate, outbox_path
from jarvis.llm import LLMRouter
from jarvis.tools.registry import ToolRegistry
from jarvis.tools.senders import STUB_SENDERS

_TTY = sys.stdout.isatty()


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _TTY else text


class Printer:
    def __init__(self) -> None:
        self.in_reply = False
        self.in_deep = False

    def _end_stream(self) -> None:
        if self.in_reply or self.in_deep:
            print(flush=True)
        self.in_reply = self.in_deep = False

    def event(self, ev: Event) -> None:
        kind = ev.get("ev")
        if kind == "_mode":
            return
        if kind == "reply":
            if not self.in_reply:
                self._end_stream()
                print(_c("1;32", "jarvis> "), end="")
                self.in_reply = True
            print(ev.get("delta", ""), end="", flush=True)
            return
        if kind == "deep":
            if ev.get("done"):
                self._end_stream()
                print(_c("2", "[deep answer done]"))
                return
            if not self.in_deep:
                self._end_stream()
                print(_c("2", "[deep] "), end="")
                self.in_deep = True
            print(_c("2", ev.get("delta", "")), end="", flush=True)
            return
        self._end_stream()
        if kind == "draft":
            self.card(ev)
        elif kind == "draft_cleared":
            print(_c("33", f"[draft {ev.get('id')} {ev.get('result')}]"))
        elif kind == "error":
            print(_c("31", f"[error from {ev.get('source')}] {ev.get('message')}"))
        elif kind == "hud":
            print(_c("36", f"[hud {'open' if ev.get('open') else 'closed'}]"))
        else:
            print(_c("2", f"[{kind}] {ev}"))

    def mode(self, mode: str) -> None:
        if mode != "idle":
            self._end_stream()
            print(_c("2", f"· {mode}"), flush=True)

    def card(self, ev: Event) -> None:
        width = 64
        lines = [f"DRAFT {ev.get('kind', '').upper()}  id={ev.get('id')}", f"To:      {ev.get('to')}"]
        if ev.get("subject"):
            lines.append(f"Subject: {ev.get('subject')}")
        lines.append("")
        for para in str(ev.get("body", "")).splitlines() or [""]:
            lines.extend(textwrap.wrap(para, width - 4) or [""])
        lines += ["", "say 'confirm' / 'cancel', or type /confirm /cancel"]
        print(_c("33", "┌" + "─" * (width - 2) + "┐"))
        for line in lines:
            print(_c("33", "│ ") + line.ljust(width - 4) + _c("33", " │"))
        print(_c("33", "└" + "─" * (width - 2) + "┘"))


async def _print_events(queue: asyncio.Queue[Event], printer: Printer) -> None:
    while True:
        ev = await queue.get()
        if ev.get("ev") == "_mode":
            printer.mode(ev["mode"])
        else:
            printer.event(ev)


async def _drain(queue: asyncio.Queue[Event], printer: Printer) -> None:
    """Wait until the printer task has shown every queued event."""
    while not queue.empty():
        await asyncio.sleep(0.01)
    await asyncio.sleep(0)
    printer._end_stream()


async def run(args: argparse.Namespace) -> int:
    cfg = load_config(Path(args.config) if args.config else None)
    overrides = {k: v for k, v in (("base_url", args.base_url), ("model", args.model)) if v}
    if overrides:
        cfg = dataclasses.replace(cfg, llm=dataclasses.replace(cfg.llm, **overrides))

    bus = Bus()
    printer = Printer()
    queue = bus.subscribe(maxsize=10_000)
    llm = LLMRouter(cfg.llm)  # the same routing as jarvisd: fast voice model, 35B for deep_think
    gate = ApprovalGate(bus, STUB_SENDERS)
    gate.register(bus)
    hud_open = {"open": False}

    async def hud_cmd(command: dict[str, Any]) -> None:
        hud_open["open"] = command["cmd"] == "hud.open"
        bus.emit("hud", open=hud_open["open"])

    bus.handle("hud.open", hud_cmd)
    bus.handle("hud.close", hud_cmd)
    # Mode changes go through the same queue as bus events, so they print in order.
    agent = Agent(llm, gate, ToolRegistry(), bus, cfg, on_mode=lambda m: queue.put_nowait({"ev": "_mode", "mode": m}))
    printer_task = asyncio.create_task(_print_events(queue, printer))

    print(_c("2", f"JARVIS typed CLI · {cfg.llm.base_url} · voice {llm.describe()['voice_model']}, deep {cfg.llm.model} ({llm.backend})"))
    print(_c("2", f"outbox: {outbox_path()} · /confirm /cancel /reset /quit"))
    try:
        return await _repl(agent, gate, bus, queue, printer)
    finally:
        printer_task.cancel()


async def _repl(agent: Agent, gate: ApprovalGate, bus: Bus, queue: asyncio.Queue[Event], printer: Printer) -> int:
    while True:
        try:
            line = await asyncio.to_thread(input, _c("1;34", "you> ") if _TTY else "you> ")
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not _TTY:
            print(line)  # echo piped input so a transcript reads naturally
        line = line.strip()
        if not line:
            continue
        if line in ("/quit", "/exit"):
            return 0
        if line == "/reset":
            agent.reset()
            print(_c("2", "[history cleared]"))
            continue
        if line in ("/confirm", "/cancel"):
            pending = gate.pending
            if pending is None:
                print(_c("2", "[no pending draft]"))
                continue
            result = await bus.dispatch({"cmd": f"draft.{line[1:]}", "id": pending.id})
            await _drain(queue, printer)
            print(_c("2", f"[ack {result}]"))
            continue
        await agent.on_user_utterance(line)
        await _drain(queue, printer)
        if agent.awaiting_confirmation:
            print(_c("2", "[awaiting confirmation: the next utterance goes through the gate first]"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="jarvis.cli_text", description="Talk to the JARVIS agent by typing.")
    parser.add_argument("--base-url", help="OpenAI-compatible base URL (e.g. http://127.0.0.1:11434/v1)")
    parser.add_argument("--model", help="model name (e.g. qwen3.5:4b on Ollama)")
    parser.add_argument("--config", help="config file (default ~/.config/jarvis/config.toml)")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
