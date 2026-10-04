"""Section 14: print the `draft` events the real tools make for confirmable actions, as JSON, for action.qml.

Runs close_app / create_file against a temp $HOME and a DryRunner (nothing on the real desktop, nothing written to
the real home), so the harness shows exactly what the daemon would send:
    uv run python dev/hud_harness/action_cards.py > cards.json
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

CLIENTS = [
    {"address": "0x1a", "class": "firefox", "title": "Inbox (3) - daniel@example.com - Gmail", "pid": 10,
     "workspace": {"id": 1, "name": "1"}, "mapped": True},
    {"address": "0x1b", "class": "firefox", "title": "Hacker News", "pid": 10, "workspace": {"id": 2, "name": "2"},
     "mapped": True},
]


async def main() -> dict:
    home = Path(tempfile.mkdtemp(prefix="jarvis-cards-"))
    os.environ["JARVIS_FILES_HOME"] = str(home)
    from jarvis.events import Bus
    from jarvis.gate import ApprovalGate
    from jarvis.integrations.desktop import Desktop, DryRunner
    from jarvis.tools.registry import ToolRegistry

    for d in ("Documents/JARVIS", "Games"):
        (home / d).mkdir(parents=True)
    (home / "Documents/JARVIS/shopping-list.txt").write_text("milk\neggs\nbread\n")
    bus = Bus()
    queue = bus.subscribe()
    reg = ToolRegistry(bus=bus, gate=ApprovalGate(bus, {}))
    apps = home / "apps"
    apps.mkdir()
    (apps / "firefox.desktop").write_text("[Desktop Entry]\nType=Application\nName=Firefox\nExec=firefox\n")
    runner = DryRunner({"hyprctl -j clients": (0, json.dumps(CLIENTS), ""),
                        "hyprctl -j activewindow": (0, "{}", "")})
    reg.ctx.desktop = Desktop(None, runner, home=home, app_dirs=[apps])

    cards: dict[str, dict] = {}

    def last_draft() -> dict:
        ev = None
        while not queue.empty():
            e = queue.get_nowait()
            if e["ev"] == "draft":
                ev = e
        assert ev is not None
        return {k: v for k, v in ev.items() if k != "ev"}

    await reg.call("close_app", {"name": "firefox"})
    cards["close"] = last_draft()
    await reg.call("create_file", {"name": "shopping-list.txt",
                                   "content": "milk\neggs (a dozen)\nbread\ncoffee\nolive oil\n"})
    cards["overwrite"] = last_draft()
    await reg.call("create_file", {"name": "notes.txt", "folder": "~/Games",
                                   "content": "".join(f"Lap {i}: 1:{40 + i % 7}.{i * 37 % 1000:03d}\n"
                                                      for i in range(1, 31))})
    cards["create"] = last_draft()
    # Section 17: run_command (a long one, to show wrapping and scrolling) and start_training, with a fake
    # systemctl and a launcher that refuses: only the cards are made.
    from jarvis.integrations.commands import Commands

    async def no_launch(argv):
        raise AssertionError("the harness never launches anything")

    (home / "Projects" / "site").mkdir(parents=True)
    script = home / "jarvis" / "finetune" / "overnight.sh"
    script.parent.mkdir(parents=True)
    script.write_text("#!/usr/bin/env bash\n")
    script.chmod(0o755)
    reg.ctx.commands = Commands(None, runner=DryRunner({"systemctl --user is-active": (3, "inactive\n", "")}),
                                home=home, launcher=no_launch)
    await reg.call("run_command", {
        "command": "rsync -avh --progress --delete --exclude node_modules --exclude .git ~/Projects/site/ "
                   "~/Backups/site-$(date +%F)/ && du -sh ~/Backups/site-$(date +%F) && ls -la ~/Backups | tail -n 5",
        "reason": "Mirrors the site project into a dated backup folder, then shows its size and the latest backups.",
        "workdir": "~/Projects"})
    cards["command"] = last_draft()
    await reg.call("run_command", {
        "command": "tar czf ~/Backups/holiday-photos.tgz " + " ".join(f"~/Pictures/Holiday/IMG_{n:04d}.jpg"
                                                                      for n in range(2301, 2341)),
        "reason": "Packs the forty holiday photos into one archive in Backups.", "workdir": "~"})
    cards["command_long"] = last_draft()
    await reg.call("run_command", {"command": "htop", "reason": "Opens htop, the interactive process viewer."})
    cards["command_short"] = last_draft()
    await reg.call("start_training", {})
    cards["training"] = last_draft()
    cards["done_message"] = "Closed Firefox."
    shutil.rmtree(home, ignore_errors=True)
    return cards


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    print(json.dumps(asyncio.run(main()), ensure_ascii=False, indent=1))
