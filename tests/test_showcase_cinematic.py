"""Section 25: the cinematic showcase (jarvis/showcase/). The script, the dry run's order and timing, the events the UI
follows, the live coding's beats, a stop mid-way (only its own windows close, the user's workspace comes back), the
fresh-workspace stage on a simulated Hyprland, the day's answer, the encore, and the routing: the session's fast
path, the tool, jarvisd's commands and every stop path for both showcase styles.

Never the real showcase: every launcher here is a DryLauncher or a fake, speech is a fake, time is scaled down, and
the real launcher / runner refuse to start anything under pytest anyway."""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from jarvis.config import Config, ShowcaseConfig
from jarvis.events import Bus
from jarvis.integrations.desktop import Desktop, Runner
from jarvis.showcase import (SHOWCASE, Showcase, ShowcaseControl, any_running, build, day_answer, load_script,
                             stop_any, trigger)
from jarvis.showcase.apps import LIVECODE_LUA, Apps, DryLauncher
from jarvis.showcase.script import APP_ACTIONS, ScriptError, parse
from jarvis.showcase.stage import HyprWorkspaces, Stage, pick
from jarvis.showcase.windows import DryWindows

HERE = Path(__file__).resolve().parent.parent / "jarvis" / "showcase"
STEP_IDS = ["intro", "about", "machine", "coding", "day", "web", "outro"]
INSTALLED = {"ghostty", "nvim", "zen-browser", "xdg-open"}


def _which(installed=INSTALLED):
    return lambda name: f"/usr/bin/{name}" if name in installed else None


def make_apps(tmp_path, installed=INSTALLED, launcher=None) -> Apps:
    apps = Apps(env={"XDG_CURRENT_DESKTOP": "Hyprland"}, which=_which(installed), launcher=launcher or DryLauncher(),
                data_dir=tmp_path / "data", work_dir=tmp_path / "run", default_browser=lambda: "zen-browser",
                python="python3")
    apps.dry = not isinstance(launcher, CodeLauncher)   # the live coding's job file is written, as in a real run
    return apps


class CodeLauncher(DryLauncher):
    """A DryLauncher whose live coding Neovim reports at once through its status files, like livecode.lua: the
    typing's progress, the line count, the program running, done."""

    def __init__(self, fail: bool = False) -> None:
        super().__init__()
        self.fail = fail

    async def spawn(self, argv):
        pid, handle = await super().spawn(argv)
        if "-u" in argv and argv[argv.index("-u") + 1] == str(LIVECODE_LUA):
            job = json.loads(Path(re.search(r"'(.+)'", argv[argv.index("--cmd") + 1]).group(1)).read_text())
            status = Path(job["status"])
            if self.fail:
                (status / "error").write_text("E492: not an editor command")
            else:
                for name, text in (("progress", "0.5"), ("progress", "1"), ("typed", "41"), ("ran", "3"),
                                   ("done", "0")):
                    (status / name).write_text(text)
        return pid, handle


class Fakes:
    """What jarvisd gives a showcase: speech, the HUD, the bus."""

    def __init__(self) -> None:
        self.bus = Bus()
        self.queue = self.bus.subscribe(maxsize=10_000)
        self.said: list[str] = []
        self.hud: list[bool] = []
        self.hushed = 0
        self.gate: asyncio.Event | None = None     # set: the line containing `block_on` waits for it
        self.block_on = ""
        self.reached = asyncio.Event()

    async def speak(self, text: str, lang: str) -> None:
        self.said.append(text)
        if self.block_on and self.block_on in text and self.gate is not None:
            self.reached.set()
            await self.gate.wait()
        await asyncio.sleep(0)

    def hush(self) -> None:
        self.hushed += 1

    async def set_hud(self, open_: bool) -> None:
        self.hud.append(open_)

    def events(self) -> list[dict]:
        out = []
        while not self.queue.empty():
            out.append(self.queue.get_nowait())
        return out


class FakeStage:
    def __init__(self, ok: bool = True) -> None:
        self.ok = ok
        self.entered = 0
        self.left = 0
        self.active = False
        self.stage = SimpleNamespace(id="7")

    async def enter(self):
        from jarvis.showcase.stage import Entered

        self.entered += 1
        self.active = self.ok
        return Entered(self.ok, "7" if self.ok else "", "1", reason="" if self.ok else "no empty workspace")

    async def leave(self):
        from jarvis.showcase.stage import Left

        self.left += 1
        self.active = False
        return Left(True, "1", returned=True)


def make_showcase(tmp_path, fakes: Fakes, *, installed=INSTALLED, launcher=None, dry_run=False, answer=None,
                  stage=None, windows=None, watch_factory=None) -> Showcase:
    async def no_answer():
        return None

    return Showcase(load_script(), "en", apps=make_apps(tmp_path, installed, launcher), speak=fakes.speak,
                    hush=fakes.hush, emit=fakes.bus.emit, hud=fakes.set_hud, answer=answer or no_answer,
                    dry_run=dry_run, time_scale=0.0005, stage=stage, windows=windows or DryWindows(),
                    watch_factory=watch_factory, address="sir")


# --- the script -------------------------------------------------------------------------------------------------------


def test_the_script_loads_with_its_scenes() -> None:
    script = load_script()
    assert [s.id for s in script.steps] == STEP_IDS
    assert [s.action for s in script.steps] == ["hud_open", "hud_close", "terminal", "code", "ask", "browser", "finish"]
    titled = [s.text("title") for s in script.steps if s.has("title")]
    assert titled == ["All local", "This machine", "Live coding", "Your day", "On the web"]
    for s in script.steps:
        assert s.line().strip()
        if s.action in APP_ACTIONS:
            assert s.has("missing")
    coding = next(s for s in script.steps if s.action == "code")
    assert coding.has("second") and coding.text("count") == "lines of Python"
    assert (HERE / coding.options["program"]).is_file()
    assert next(s for s in script.steps if s.action == "finish").text("recap").count("|") == 3


def test_no_company_content_anywhere_in_the_showcase() -> None:
    banned = re.compile(r"gradex|\berp\b|narex|\bvector\b|\bfair\b|\bmsv\b|company", re.IGNORECASE)
    for path in HERE.iterdir():
        if path.suffix in (".py", ".toml", ".lua"):
            assert not banned.search(path.read_text(encoding="utf-8")), path.name


def test_the_lines_say_no_stop_word() -> None:
    from jarvis.audio.bargein import fold

    stop = re.compile(r"\b(?:stop|enough|quiet|silence|wait|hold on|hang on|one moment|one second|moment|dost|ticho"
                      r"|chvilku|shut up)\b")
    for s in load_script().steps:
        for text in [s.line(), *(t.get("en", "") for t in s.texts.values())]:
            assert not stop.search(fold(text)), (s.id, text)


@pytest.mark.parametrize("broken, why", [
    ({"step": [{"id": "a", "action": "dance", "say": "x"}]}, "unknown action"),
    ({"step": [{"id": "a", "action": "terminal", "say": "x"}, {"id": "z", "action": "finish", "say": "y"}]}, "missing"),
    ({"step": [{"id": "a", "action": "hud_open", "say": "x"}]}, "last step must be the finish"),
    ({"step": [{"id": "a", "action": "finish", "say": "x", "path": [{"x": 2, "y": 0}]}]}, "out of range"),
])
def test_a_broken_script_is_refused(broken, why) -> None:
    with pytest.raises(ScriptError, match=why):
        parse(broken)


# --- the dry run ------------------------------------------------------------------------------------------------------


async def test_dry_run_order_and_timing(tmp_path) -> None:
    fakes = Fakes()
    launcher = DryLauncher()
    sc = make_showcase(tmp_path, fakes, launcher=launcher, dry_run=True)
    result = await sc.run()
    assert result.status == "done" and result.steps == STEP_IDS
    assert fakes.said == [] and fakes.hud == []  # speaks nothing, opens no HUD
    assert 100 <= result.seconds <= 160, result.seconds
    opened = [e["app"] for e in result.timeline if e["kind"] == "open"]
    assert opened == ["terminal", "code", "browser"]
    assert len(launcher.terminated) == 3   # each window scene's window closes after it
    says = [e["text"] for e in result.timeline if e["kind"] == "say"]
    assert says[0].startswith(("Good morning, sir", "Good afternoon, sir", "Good evening, sir"))
    assert "{" not in " ".join(says)
    assert says[-1] == "At your service, sir. Just say my name."
    times = [e["t"] for e in result.timeline if e["kind"] == "step"]
    assert times == sorted(times)


async def test_build_dry_run_needs_nothing_live(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    result = await build(Config(), dry_run=True).run()
    assert result.status == "done" and result.steps == STEP_IDS


def test_jarvisctl_dry_run_prints_the_timeline() -> None:
    root = HERE.parent.parent
    out = subprocess.run([sys.executable, str(root / "bin" / "jarvisctl"), "showcase", "--dry-run"],
                         capture_output=True, text=True, timeout=60, cwd=root)
    assert out.returncode == 0, out.stderr
    assert "showcase dry run" in out.stdout and re.search(r"total ≈ \d+ s, 7 steps", out.stdout)
    assert "lines typed live in Neovim" in out.stdout


# --- the real run with fakes: the events the UI follows ---------------------------------------------------------------


async def test_events_and_beats_of_a_whole_run(tmp_path) -> None:
    fakes = Fakes()
    launcher = CodeLauncher()
    stage = FakeStage()

    async def answer():
        return {"text": "It's 18 degrees and clear.", "sources": ["Open-Meteo"]}

    sc = make_showcase(tmp_path, fakes, launcher=launcher, stage=stage, answer=answer)
    result = await asyncio.wait_for(sc.run(), 20)
    assert result.status == "done", result.reason
    events = fakes.events()
    kinds = [e["ev"] for e in events]
    assert kinds[0] == "showcase.start" and kinds[-1] == "showcase.end"
    start = events[0]
    assert start == {"ev": "showcase.start", "lang": "en", "steps": STEP_IDS, "total": 7, "dry_run": False,
                     "chapters": 5, "name": "JARVIS", "wordmark": "JARVIS"}
    steps = [e for e in events if e["ev"] == "showcase.step"]
    assert [s["id"] for s in steps] == STEP_IDS
    assert set(steps[3]) == {"ev", "index", "id", "total", "action", "path", "title", "chapter", "chapters"}
    assert steps[3]["action"] == "code" and steps[3]["title"] == "Live coding" and steps[3]["chapter"] == 3
    assert all(isinstance(p, list) and len(p) == 2 for p in steps[1]["path"])
    moves = [e for e in events if e["ev"] == "showcase.move"]
    assert moves and set(moves[0]) == {"ev", "x", "y", "ms"}
    frames = [e for e in events if e["ev"] == "showcase.frame" and not e.get("clear")]
    assert [f["app"] for f in frames] == ["terminal", "code", "browser"]
    assert set(frames[0]) == {"ev", "app", "x", "y", "w", "h"}
    beats = [(e["app"], e["kind"]) for e in events if e["ev"] == "showcase.beat"]
    assert ("code", "progress") in beats and ("code", "run") in beats and ("code", "done") in beats
    assert ("browser", "tab") in beats
    total = next(e for e in events if e["ev"] == "showcase.beat" and e["kind"] == "total")
    assert total["value"] == 41 and total["label"] == "lines of Python"
    order = [k for a, k in beats if a == "code"]
    assert order.index("total") < order.index("run") < order.index("done")
    cards = [e for e in events if e["ev"] == "showcase.card"]
    assert [c["kind"] for c in cards] == ["question", "answer", "clear"]
    assert cards[0]["question"] == "What does my day look like?"
    assert cards[1]["text"] == "It's 18 degrees and clear." and cards[1]["sources"] == ["Open-Meteo"]
    finale = [e for e in events if e["ev"] == "showcase.finale"]
    assert [f["phase"] for f in finale] == ["recap", "reveal", "name"]
    assert finale[0]["items"] == ["Code", "Desktop", "Web", "Your day"] and finale[2]["wordmark"] == "JARVIS"
    assert finale[2]["line"] == "At your service, sir. Just say my name."
    assert events[-1] == {"ev": "showcase.end", "status": "done", "reason": "", "home_ms": 1300}
    # the second line comes once the program runs; the day's answer is said as it is on the card
    second = next(s for s in load_script().steps if s.id == "coding").text("second")
    assert second in fakes.said and "It's 18 degrees and clear." in fakes.said
    assert fakes.hud == [True, False]
    assert stage.entered == 3 and stage.left == 3
    assert sorted(launcher.terminated) == sorted(range(90001, 90001 + len(launcher.spawned)))


async def test_the_day_falls_back_to_its_line(tmp_path) -> None:
    fakes = Fakes()
    sc = make_showcase(tmp_path, fakes, launcher=CodeLauncher())
    assert (await asyncio.wait_for(sc.run(), 20)).status == "done"
    answer = next(e for e in fakes.events() if e["ev"] == "showcase.card" and e["kind"] == "answer")
    assert answer["text"].startswith("A clear day, sir") and answer["sources"] == []


async def test_a_broken_live_coding_still_goes_on(tmp_path) -> None:
    fakes = Fakes()
    sc = make_showcase(tmp_path, fakes, launcher=CodeLauncher(fail=True))
    result = await asyncio.wait_for(sc.run(), 20)
    assert result.status == "done"
    beats = [(e["app"], e["kind"]) for e in fakes.events() if e["ev"] == "showcase.beat"]
    assert ("code", "done") not in beats and ("code", "run") not in beats
    assert any(e["kind"] == "typed" and e.get("state") == "error" for e in result.timeline)


async def test_missing_apps_are_told_not_opened(tmp_path) -> None:
    fakes = Fakes()
    launcher = DryLauncher()
    sc = make_showcase(tmp_path, fakes, installed={"xdg-open"}, launcher=launcher)
    result = await asyncio.wait_for(sc.run(), 20)
    assert result.status == "done"
    script = {s.id: s for s in load_script().steps}
    assert script["machine"].text("missing").replace("{address}", "sir") in fakes.said
    assert script["coding"].text("missing").replace("{address}", "sir") in fakes.said
    assert [a[0] for a in launcher.spawned] == ["xdg-open"]   # only the web, handed over (never closed)
    assert launcher.terminated == []


async def test_no_empty_workspace_means_no_window(tmp_path) -> None:
    fakes = Fakes()
    launcher = CodeLauncher()
    sc = make_showcase(tmp_path, fakes, launcher=launcher, stage=FakeStage(ok=False))
    assert (await asyncio.wait_for(sc.run(), 20)).status == "done"
    assert launcher.spawned == []


# --- a stop mid-way ---------------------------------------------------------------------------------------------------


async def test_stop_mid_way_closes_only_its_own_windows_and_goes_back(tmp_path) -> None:
    fakes = Fakes()
    fakes.gate = asyncio.Event()
    fakes.block_on = "live coding"
    launcher = CodeLauncher()
    stage = FakeStage()
    sc = make_showcase(tmp_path, fakes, launcher=launcher, stage=stage)
    control = ShowcaseControl()
    task = control.start(sc)
    await asyncio.wait_for(fakes.reached.wait(), 10)
    assert control.running and stage.active
    assert control.stop("the user spoke")
    result = await asyncio.wait_for(task, 10)
    assert result.status == "stopped" and result.reason == "the user spoke"
    assert result.steps == STEP_IDS[:4]
    assert sorted(result.closed) == sorted(launcher.terminated)
    assert set(launcher.terminated) <= set(range(90001, 90001 + len(launcher.spawned)))
    assert not stage.active and fakes.hushed == 1
    events = fakes.events()
    kinds = [e["ev"] for e in events]
    assert kinds.index("showcase.stopping") < kinds.index("showcase.end")
    assert events[-1]["status"] == "stopped"
    assert {"ev": "showcase.frame", "clear": True} in events
    assert not control.stop("again")


async def test_a_stop_before_it_begins_runs_nothing(tmp_path) -> None:
    fakes = Fakes()
    launcher = DryLauncher()
    sc = make_showcase(tmp_path, fakes, launcher=launcher)
    sc.stop("early")
    result = await sc.run()
    assert result.status == "stopped" and launcher.spawned == [] and fakes.said == []


async def test_the_takeover_watch_stops_it(tmp_path) -> None:
    fakes = Fakes()
    fakes.gate = asyncio.Event()
    fakes.block_on = "introduce myself"
    watches: list[Any] = []

    class Watch:
        def __init__(self, on_takeover):
            self.on_takeover = on_takeover
            self.stopped = False
            watches.append(self)

        async def start(self):
            return 2

        async def stop(self):
            self.stopped = True

    sc = make_showcase(tmp_path, fakes, watch_factory=Watch)
    task = asyncio.create_task(sc.run())
    await asyncio.wait_for(fakes.reached.wait(), 5)
    watches[0].on_takeover("key")
    result = await asyncio.wait_for(task, 5)
    assert result.status == "stopped" and result.reason == "key" and watches[0].stopped


def test_showcase_watch_any_key_but_not_a_modifier_alone() -> None:
    pytest.importorskip("evdev")
    from evdev import ecodes as e

    from jarvis.showcase import ShowcaseWatch

    fired: list[str] = []
    now = [0.0]
    w = ShowcaseWatch(fired.append, grace_s=2.0, clock=lambda: now[0])
    w.feed(e.EV_KEY, e.KEY_A, 1)
    assert fired == []            # the first 2 s don't count
    now[0] = 3.0
    w.feed(e.EV_KEY, e.KEY_LEFTCTRL, 1)
    assert fired == []            # a modifier alone isn't taking over
    w.feed(e.EV_KEY, e.KEY_A, 1)
    assert fired == ["key"]


# --- the stage on a simulated Hyprland --------------------------------------------------------------------------------


class HyprSim(Runner):
    """Workspaces with window counts, like `hyprctl -j` reports them; a focus dispatch switches (and makes the
    workspace), like Hyprland."""

    def __init__(self, windows=None, active=1, rules=None):
        self.windows = dict(windows if windows is not None else {1: 3, 2: 1})
        self.active = active
        self.rules = rules or []
        self.dispatched: list[str] = []

    async def run(self, argv, timeout=5.0):
        if argv[:2] == ["hyprctl", "dispatch"]:
            self.dispatched.append(argv[2])
            n = int(re.fullmatch(r"hl\.dsp\.focus\(\{ workspace = (\d+) \}\)", argv[2]).group(1))
            if self.windows.get(self.active) == 0:
                self.windows.pop(self.active)
            self.windows.setdefault(n, 0)
            self.active = n
            return 0, "ok", ""
        what = argv[2]
        if what == "activeworkspace":
            return 0, json.dumps({"id": self.active, "name": str(self.active), "monitor": "DP-1"}), ""
        if what == "workspaces":
            return 0, json.dumps([{"id": n, "name": str(n), "monitor": "DP-1", "windows": c}
                                  for n, c in sorted(self.windows.items())]), ""
        if what == "monitors":
            return 0, json.dumps([{"name": "DP-1", "activeWorkspace": {"id": self.active},
                                   "specialWorkspace": {"id": 0}}]), ""
        if what == "workspacerules":
            return 0, json.dumps(self.rules), ""
        if what == "animations":
            return 0, json.dumps([[{"name": "workspaces", "overridden": True, "enabled": True, "speed": 4}], []]), ""
        raise AssertionError(argv)

    async def spawn(self, argv):
        raise AssertionError("the stage never starts anything")


async def _no_sleep(_s):
    await asyncio.sleep(0)


async def test_stage_picks_the_lowest_empty_workspace_and_goes_back() -> None:
    sim = HyprSim({1: 3, 2: 1, 4: 2}, rules=[{"workspaceString": "3", "monitor": "HDMI-A-1"}])
    stage = Stage(HyprWorkspaces(Desktop(runner=sim)), sleep=_no_sleep)
    entered = await stage.enter()
    assert entered.ok and entered.workspace == "5" and entered.home == "1"   # 3 belongs to another monitor
    assert sim.active == 5 and await stage._settle_s() == pytest.approx(0.5)
    left = await stage.leave()
    assert left.returned and sim.active == 1 and 5 not in sim.windows
    assert sim.dispatched == ["hl.dsp.focus({ workspace = 5 })", "hl.dsp.focus({ workspace = 1 })"]


async def test_stage_leaves_a_user_who_went_elsewhere() -> None:
    sim = HyprSim()
    stage = Stage(HyprWorkspaces(Desktop(runner=sim)), settle_s=0, sleep=_no_sleep)
    assert (await stage.enter()).ok
    sim.active = 2   # the user switched to their own workspace 2 meanwhile
    left = await stage.leave()
    assert not left.returned and "left there" in left.reason and sim.active == 2


def test_pick_skips_busy_and_shown_workspaces() -> None:
    from jarvis.showcase.stage import WorkspaceInfo, WorkspaceRef, WorkspaceState

    state = WorkspaceState(WorkspaceRef("2", 2), (WorkspaceInfo(WorkspaceRef("1", 1), windows=1),
                                                  WorkspaceInfo(WorkspaceRef("3", 3), visible=True)), monitor="A")
    assert pick(state).number == 4


# --- the day's answer -------------------------------------------------------------------------------------------------


def test_day_answer_from_the_widgets() -> None:
    widgets = {
        "weather": {"now": {"temp": 18.4, "text": "Partly cloudy"}, "daily": [{"label": "Today", "max": 22.6}],
                    "rain_next_3h": True, "rain_at": "16:00"},
        "reminders": [{"day": "Today", "time": "14:30", "text": "private"}, {"day": "Today", "time": "18:00"},
                      {"day": "Tomorrow", "time": "09:00"}],
        "calendar": [{"day": "Today", "time": "15:30", "all_day": False, "title": "secret"}],
        "timers": [{"text": "pasta"}],
    }
    got = day_answer(widgets)
    assert got["text"] == ("It's 18 degrees and partly cloudy, with a high of 23. Rain is likely around 16:00. "
                           "You have two reminders today, the next at 14:30. "
                           "One event on the calendar, the first at 15:30. And one timer running.")
    assert got["sources"] == ["Open-Meteo", "Reminders", "Calendar"]
    assert "private" not in got["text"] and "secret" not in got["text"]   # never the reminders' own words


def test_day_answer_with_weather_only_and_with_nothing() -> None:
    got = day_answer({"weather": {"now": {"temp": 9, "text": "Clear"}}})
    assert got == {"text": "It's 9 degrees and clear. Nothing on the schedule.", "sources": ["Open-Meteo"]}
    assert day_answer({}) is None
    assert day_answer({"weather": None, "reminders": [], "calendar": []}) is None


# --- the live coding's own parts --------------------------------------------------------------------------------------


def test_the_demo_program_runs_headless_and_quickly(tmp_path) -> None:
    env = dict(os.environ, COLUMNS="60", LINES="16")
    t0 = time.monotonic()
    out = subprocess.run([sys.executable, str(HERE / "livecode_demo.py"), "0.4"], capture_output=True, text=True,
                         env=env, timeout=10, cwd=tmp_path)
    assert out.returncode == 0, out.stderr
    assert time.monotonic() - t0 < 3
    assert out.stdout.rstrip().endswith("Written live by JARVIS.")
    assert "\x1b[?25l" in out.stdout and "\x1b[?25h" in out.stdout   # the cursor hidden, then back
    assert "\x1b[38;2;" in out.stdout and "▀" in out.stdout


def test_the_demo_program_is_short_enough_to_type_live() -> None:
    lines = (HERE / "livecode_demo.py").read_text(encoding="utf-8").rstrip("\n").split("\n")
    assert 25 <= len(lines) <= 45 and max(len(line) for line in lines) <= 120


@pytest.mark.skipif(shutil.which("nvim") is None, reason="no Neovim")
def test_the_typist_lua_compiles(tmp_path) -> None:
    out = subprocess.run(["nvim", "--headless", "--clean", "-n", "-c", f"lua assert(loadfile([[{LIVECODE_LUA}]]))",
                          "-c", "qa!"], capture_output=True, text=True, timeout=20, cwd=tmp_path)
    assert out.returncode == 0, out.stderr


@pytest.mark.skipif(shutil.which("nvim") is None, reason="no Neovim")
def test_the_typist_types_the_program_and_runs_it(tmp_path) -> None:
    """livecode.lua in a headless Neovim (no window): the file it writes is the program, and the status files come
    in the runner's order."""
    root = HERE.parent.parent
    out = subprocess.run([sys.executable, str(root / "scripts" / "livecode_check.py"), "--cps", "4000", "--dir",
                          str(tmp_path / "lc"), "--timeout-s", "30"], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stdout + out.stderr
    seen = [line.split()[2].rstrip(":") for line in out.stdout.splitlines() if " s  " in line]
    assert seen.index("typed") < seen.index("done") and "error" not in seen
    assert (tmp_path / "lc" / "program.py").read_text() == (HERE / "livecode_demo.py").read_text()


async def test_open_code_writes_the_job_and_starts_neovim_in_its_own_terminal(tmp_path) -> None:
    launcher = DryLauncher()
    apps = Apps(env={"XDG_CURRENT_DESKTOP": "Hyprland"}, which=_which(), launcher=launcher, data_dir=tmp_path / "d",
                work_dir=tmp_path / "w", python="python3")
    apps.dry = False    # write the job like a real run (the launcher still starts nothing)
    code = await apps.open_code(HERE / "livecode_demo.py", "arc_reactor.py", cps=99, run_args=["6"])
    argv = launcher.spawned[0]
    assert argv[:5] == ["/usr/bin/ghostty", "--gtk-single-instance=false", "--title=JARVIS", "--font-size=15", "-e"]
    assert argv[5:8] == ["/usr/bin/nvim", "--clean", "-n"]
    assert argv[-1] == str(tmp_path / "w" / "code" / "arc_reactor.py")
    job = json.loads((tmp_path / "w" / "code" / "livecode.json").read_text())
    assert job["cps"] == 99 and job["args"] == ["6"] and job["source"].endswith("livecode_demo.py")
    code.go()
    assert code.has("go")
    with pytest.raises(ValueError):
        await apps.open_code(HERE / "livecode_demo.py", "../evil.sh")


async def test_the_browser_gets_its_own_zen_profile_without_onboarding(tmp_path) -> None:
    launcher = DryLauncher()
    apps = make_apps(tmp_path, launcher=launcher)
    apps.dry = False
    item = await apps.open_browser("https://en.wikipedia.org/wiki/J.A.R.V.I.S.",
                                   ["https://en.wikipedia.org/wiki/Iron_Man"])
    assert item.tracked and item.argv[:3] == ["/usr/bin/zen-browser", "--new-instance", "--profile"]
    prefs = (Path(item.argv[3]) / "user.js").read_text()
    assert 'user_pref("zen.welcome-screen.seen", true);' in prefs
    assert item.argv[-2:] == ["https://en.wikipedia.org/wiki/J.A.R.V.I.S.", "https://en.wikipedia.org/wiki/Iron_Man"]


# --- the encore -------------------------------------------------------------------------------------------------------


async def test_encore_only_right_after_a_finished_showcase(monkeypatch) -> None:
    control = ShowcaseControl()
    monkeypatch.setattr("jarvis.showcase.runner.SHOWCASE", control)
    assert trigger.encore("Jarvis, again!") is None
    control.done_at = time.monotonic()
    control.last = {"lang": "en"}
    assert trigger.encore("What's the weather?") is None
    assert trigger.encore("Jarvis, one more time.") == "en"
    assert trigger.take_encore() and not trigger.take_encore()
    assert trigger.encore("Znovu.") == "en" and trigger.take_encore()
    assert trigger.encore("again", now=time.monotonic() + trigger.ENCORE_WINDOW_S + 1) is None


def test_the_request_phrases_still_match() -> None:
    assert trigger.match("Jarvis, present yourself.") == "en"
    assert trigger.match("Představ se.") == "cs"
    assert trigger.match("What are you doing tonight?") is None


# --- routing: the fast path, the tool, jarvisd, the stops -------------------------------------------------------------


class Blocking:
    """A stand-in cinematic run in the global SHOWCASE: runs until it is stopped."""

    def __init__(self) -> None:
        self.lang = "en"
        self.stopped: list[str] = []
        self._stop = asyncio.Event()

    def stop(self, reason: str) -> bool:
        self.stopped.append(reason)
        self._stop.set()
        return True

    async def run(self):
        from jarvis.showcase import Result

        await self._stop.wait()
        return Result("stopped", self.stopped[0] if self.stopped else "")


@pytest.fixture
def clean_showcase():
    stop_any("test")
    yield
    stop_any("test")


async def test_stop_any_stops_the_cinematic_one(clean_showcase) -> None:
    run = Blocking()
    task = SHOWCASE.start(run)  # type: ignore[arg-type]
    await asyncio.sleep(0)
    assert any_running()
    assert stop_any("click")
    await asyncio.wait_for(task, 2)
    assert run.stopped == ["click"] and not any_running()


class FakeAgent:
    def __init__(self, ctx) -> None:
        self.tools = SimpleNamespace(ctx=ctx)
        self.user_language: str | None = None
        self.awaiting_confirmation = False
        self.turns: list[str] = []

    async def on_user_utterance(self, text: str) -> str:
        self.turns.append(text)
        return "An answer."

    def reset(self) -> None:
        pass


def _session(bus: Bus, style: str = "cinematic"):
    from jarvis.session import Session
    from jarvis.tools.registry import ToolContext

    ctx = ToolContext(bus=bus, cfg=Config(showcase=ShowcaseConfig(style=style)))
    agent = FakeAgent(ctx)
    return Session(bus, Config(), agent=agent), agent, ctx


async def test_session_fast_path_starts_the_cinematic_showcase_without_the_model(clean_showcase) -> None:
    bus = Bus()
    started: list[dict] = []

    async def start(cmd):
        started.append(cmd)
        return {"started": True, "lang": "en"}

    bus.handle("showcase.start", start)
    session, agent, _ = _session(bus)
    assert await session.handle_utterance("Jarvis, who are you?") == ""
    assert agent.turns == [] and started == [{"cmd": "showcase.start", "via": "voice"}]


async def test_the_tool_starts_it_through_jarvisd_and_ends_the_turn(clean_showcase) -> None:
    from jarvis.tools import showcase as tool

    bus = Bus()
    bus.handle("showcase.start", lambda cmd: asyncio.sleep(0, {"started": True}))
    _, _, ctx = _session(bus)
    ctx.turn_text = "Give my friend a quick demo."
    result = await tool._showcase(ctx, {})
    assert result["ok"] and result["end_turn"] and result["said"] == ""
    ctx.turn_text = "Say something in German."
    assert (await tool._showcase(ctx, {})).get("refused")


async def test_without_jarvisd_the_tool_says_so(clean_showcase) -> None:
    from jarvis.tools import showcase as tool

    _, _, ctx = _session(Bus())
    ctx.turn_text = "Show me what you can do."
    assert "only in jarvisd" in (await tool._showcase(ctx, {}))["error"]


async def test_a_question_while_it_runs_stops_it_and_is_answered(clean_showcase) -> None:
    bus = Bus()
    session, agent, _ = _session(bus)
    run = Blocking()
    task = SHOWCASE.start(run)  # type: ignore[arg-type]
    await asyncio.sleep(0)
    assert await session.handle_utterance("What's the weather?") == "An answer."
    await asyncio.wait_for(task, 2)
    assert run.stopped == ["the user spoke"] and agent.turns == ["What's the weather?"]


async def test_a_stop_word_while_it_runs_is_the_whole_turn(clean_showcase) -> None:
    session, agent, _ = _session(Bus())
    task = SHOWCASE.start(Blocking())  # type: ignore[arg-type]
    await asyncio.sleep(0)
    assert await session.handle_utterance("Stop.") == ""
    await asyncio.wait_for(task, 2)
    assert agent.turns == []


async def test_a_click_on_the_orb_only_stops_it(clean_showcase) -> None:
    session, _, _ = _session(Bus())
    run = Blocking()
    task = SHOWCASE.start(run)  # type: ignore[arg-type]
    await asyncio.sleep(0)
    await session.toggle()
    await asyncio.wait_for(task, 2)
    assert run.stopped == ["click"] and not session.active
    await session.toggle()
    assert session.active   # nothing running: the click opens a session as always
    await session.stop()


async def test_closing_the_session_stops_it(clean_showcase) -> None:
    session, _, _ = _session(Bus())
    await session.start()
    run = Blocking()
    task = SHOWCASE.start(run)  # type: ignore[arg-type]
    await asyncio.sleep(0)
    assert session._busy()   # the session stays open while it runs
    await session.stop()
    await asyncio.wait_for(task, 2)
    assert run.stopped == ["session closed"]


async def test_jarvisd_commands_snapshot_hold_and_stop(tmp_path, monkeypatch, clean_showcase) -> None:
    import jarvis.llm
    import jarvis.showcase as pkg
    from jarvis.daemon import Daemon
    from tests.test_ipc import ScriptedLLM

    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(jarvis.llm, "LLM", ScriptedLLM)
    built: list[dict] = []
    gate = asyncio.Event()

    def fake_build(cfg, lang="en", **kw):
        built.append(kw)
        fakes = Fakes()
        fakes.gate, fakes.block_on = gate, "introduce myself"
        return Showcase(load_script(), "en", apps=make_apps(tmp_path), speak=fakes.speak, hush=kw.get("hush"),
                        emit=kw.get("emit"), windows=DryWindows(), time_scale=0.0005)

    monkeypatch.setattr(pkg, "build", fake_build)
    daemon = Daemon(Config(), socket_path=tmp_path / "j.sock")
    daemon.register_commands()
    q = daemon.bus.subscribe(maxsize=10_000)
    ack = await daemon.bus.dispatch({"cmd": "showcase.start", "via": "menu"})
    assert ack == {"started": True, "lang": "en"} and built[0]["speak"] == daemon._showcase_speak
    await asyncio.sleep(0.05)
    assert daemon.snapshot()["showcase"] == {"active": True, "lang": "en"}
    assert any(hold() for hold in daemon.model.holds)   # the voice model stays loaded meanwhile
    with pytest.raises(RuntimeError, match="already running"):
        await daemon.bus.dispatch({"cmd": "showcase.start"})
    assert await daemon.bus.dispatch({"cmd": "showcase.stop"}) == {"stopped": True}
    await asyncio.wait_for(SHOWCASE.task, 5)
    assert daemon.snapshot()["showcase"] == {"active": False, "lang": ""}
    assert await daemon.bus.dispatch({"cmd": "showcase.stop"}) == {"stopped": False}
    events = [q.get_nowait()["ev"] for _ in range(q.qsize())]
    assert "showcase.start" in events and "showcase.stopping" in events and events[-1] == "showcase.end"
    gate.set()


async def test_jarvisd_dry_run_and_turned_off(tmp_path, monkeypatch, clean_showcase) -> None:
    import jarvis.llm
    from jarvis.daemon import Daemon
    from tests.test_ipc import ScriptedLLM

    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(jarvis.llm, "LLM", ScriptedLLM)
    daemon = Daemon(Config(), socket_path=tmp_path / "j.sock")
    daemon.register_commands()
    dry = await daemon.bus.dispatch({"cmd": "showcase.start", "dry_run": True})
    assert dry["dry_run"] and dry["status"] == "done" and dry["steps"] == STEP_IDS and dry["timeline"]
    assert not any_running()
    off = Daemon(Config(showcase=ShowcaseConfig(enabled=False)), socket_path=tmp_path / "k.sock")
    off.register_commands()
    with pytest.raises(RuntimeError, match="turned off"):
        await off.bus.dispatch({"cmd": "showcase.start"})


async def test_speaking_without_a_voice_keeps_the_pace(tmp_path, monkeypatch) -> None:
    import jarvis.llm
    import jarvis.showcase as pkg
    from jarvis.daemon import Daemon
    from tests.test_ipc import ScriptedLLM

    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(jarvis.llm, "LLM", ScriptedLLM)
    monkeypatch.setattr(pkg, "speech_seconds", lambda text, *a: 0.0)
    daemon = Daemon(Config(), socket_path=tmp_path / "j.sock")
    q = daemon.bus.subscribe()
    t0 = time.monotonic()
    await daemon._showcase_speak("Good evening, sir.", "en")
    assert time.monotonic() - t0 < 1.5
    assert q.get_nowait() == {"ev": "reply", "delta": "Good evening, sir. "}   # still on screen
