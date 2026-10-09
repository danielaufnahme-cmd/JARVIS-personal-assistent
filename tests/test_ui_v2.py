"""The v2 UI (pill, HUD, showcase choreography): the corner mode, no company leftovers, the baked shaders, and
offscreen renders of the real QML through dev/hud_harness (Qt's software renderer: no GPU, nothing on screen)."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
UI = REPO / "ui"
HARNESS = REPO / "dev" / "hud_harness"
QML = Path("/usr/lib/qt6/bin/qml")
QML_FILES = sorted(UI.glob("*.qml")) + sorted((UI / "hud").glob("*.qml"))

# Files whose every radius is a corner (cards, plates, buttons, chips, popups, bars): each must follow Theme.round,
# so the whole UI goes 90° in "square" mode. Decorative circles (the orb, the core's rings, sparks, glows) are
# elsewhere or allowed below.
CORNERED = [
    "CornerPill.qml", "CornerToggle.qml", "DraftCardView.qml", "ReadingView.qml", "PillButton.qml",
    "ShowcaseCard.qml", "ShowcaseTitle.qml", "ShowcaseFrame.qml",
    "hud/HudPanel.qml", "hud/HudButton.qml", "hud/HudDraft.qml", "hud/EmailsPanel.qml", "hud/TodayPanel.qml",
    "hud/HeadlinesPanel.qml", "hud/ClockWeather.qml", "hud/SystemPanel.qml", "hud/ConversationPanel.qml",
    "hud/Meter.qml", "hud/FirmPanel.qml", "hud/EmptyState.qml", "PillDockView.qml",
]
ROUND_AWARE = ("Theme.round", "Theme.cardRadius", "Theme.pillRadius", "Theme.hudCardRadius", "card.rad", "card.radius")


def test_theme_follows_the_corner_mode_file() -> None:
    theme = (UI / "Theme.qml").read_text()
    # the same file corners-toggle writes (MODE_FILE = ~/.local/state/corners/mode), watched and reloaded
    assert 'Quickshell.env("HOME") + "/.local/state/corners/mode"' in theme
    block = theme[theme.index("id: cornersFile"):]
    block = block[:block.index("}")]
    assert "watchChanges: true" in block and "onFileChanged: reload()" in block
    assert "onLoadFailed: theme.applyCorners(\"round\")" in block   # no file = round, like corners-toggle
    for token in ("pillRadius: (pillHeight / 2) * round", "cardRadius: 16 * round", "hudCardRadius: 14 * round"):
        assert token in theme


@pytest.mark.parametrize("name", CORNERED)
def test_every_corner_follows_the_corner_mode(name: str) -> None:
    for n, line in enumerate((UI / name).read_text().splitlines(), 1):
        code = line.split("//")[0]
        m = re.search(r"\b(?:radius|[a-z]+Radius):\s*(.+)", code)
        if not m or "property real radius" in code:
            continue
        assert any(tok in m.group(1) for tok in ROUND_AWARE), f"{name}:{n}: {line.strip()}"


def test_no_company_leftovers_in_the_ui() -> None:
    # ("vector" as in vector graphics is fine: "Pure vector (Shapes)", "Vector rings")
    banned = re.compile(r"(?i:gradex|vectord|vectorctl|\"vector\"|fair mode|Theme\.brand|brand/|cornerRight|docJob"
                        r"|DocProgress|KWin|Moonlight)|\bVector(?! rings)\b|\bERP\b|\bKDE\b|\bApp\.")
    for f in [*QML_FILES, *(HARNESS.glob("*.qml")), *(HARNESS.glob("*.sh")), *(UI / "hud" / "shaders").glob("*.frag")]:
        for n, line in enumerate(f.read_text().splitlines(), 1):
            assert not banned.search(line), f"{f.relative_to(REPO)}:{n}: {line.strip()}"


def test_no_hex_colours_outside_the_theme() -> None:
    for f in QML_FILES:
        if f.name == "Theme.qml":
            continue
        code = re.sub(r"//.*", "", f.read_text())
        assert not re.search(r'"#[0-9a-fA-F]{3,8}"', code), f.name


def test_every_shader_is_baked() -> None:
    frags = sorted((UI / "hud" / "shaders").glob("*.frag"))
    assert {f.stem for f in frags} >= {"backdrop", "core", "arc", "card", "grid"}
    for f in frags:
        assert f.with_name(f.name + ".qsb").is_file(), f"{f.name}.qsb missing: run ui/hud/shaders/build.sh"
    for qml in (UI / "hud").glob("*.qml"):
        for ref in re.findall(r"shaders/([\w.]+\.qsb)", qml.read_text()):
            assert (UI / "hud" / "shaders" / ref).is_file(), (qml.name, ref)


def test_the_showcase_ui_knows_the_daemons_actions_and_beats() -> None:
    """The choreography keys off jarvis/showcase's step actions and beat kinds (never step ids)."""
    for name in ("ShowcaseFx.qml", "CoreField.qml"):
        assert 'appActions: ["terminal", "code", "browser"]' in (UI / name).read_text()
    beats = (UI / "ShowcaseBeats.qml").read_text()
    for kind in ("progress", "total", "run", "chart", "done", "tab"):
        assert f'case "{kind}":' in beats
    assert '"ask"' in (UI / "CoreField.qml").read_text()
    shell = (UI / "shell.qml").read_text()
    assert "ShowcaseCores {" in shell and "onShowcaseActiveChanged" in shell


def _run(argv: list[str], tmp_path: Path, **extra: str) -> str:
    env = dict(os.environ, XDG_RUNTIME_DIR=str(tmp_path), **extra)
    run = subprocess.run(argv, capture_output=True, text=True, timeout=120, env=env)
    return run.stdout + run.stderr


def _qml_errors(log: str) -> list[str]:
    return [ln for ln in log.splitlines() if re.search(r"\.qml:\d+|TypeError|ReferenceError|unavailable|not a type", ln)]


needs_qml = pytest.mark.skipif(not QML.exists(), reason="needs Qt 6's qml tool")


@needs_qml
@pytest.mark.parametrize("mode,corners", [("idle", "1"), ("speaking", "0"), ("deep", "1"), ("awaiting_confirm", "0")])
def test_hud_renders_offscreen_without_qml_errors(mode: str, corners: str, tmp_path: Path) -> None:
    out = tmp_path / f"{mode}.png"
    log = _run([str(HARNESS / "render.sh"), "1920", "1080", mode, "full", str(out)], tmp_path,
               HUD_SOFTWARE="1", HUD_DELAY="700", HUD_ROUND=corners)
    assert out.is_file() and out.stat().st_size > 50_000, log
    assert not _qml_errors(log), log


@needs_qml
@pytest.mark.parametrize("corners,variant", [("1", "on"), ("0", "showcase"), ("1", "off"),
                                             # section 28 (the harness checks the 460 px limit for each)
                                             ("1", "drop"), ("0", "rec"), ("1", "notify"), ("0", "focus"),
                                             ("1", "focuspaused"), ("1", "busy"), ("0", "busy"), ("1", "memory")])
def test_pill_offscreen(corners: str, variant: str, tmp_path: Path) -> None:
    out = tmp_path / "pill.png"
    log = _run([str(HARNESS / "render_pill.sh"), str(out), variant], tmp_path, HUD_ROUND=corners)
    assert "PILL OK (" + ("round" if corners == "1" else "square") + ")" in log, log
    assert not _qml_errors(log), log


@needs_qml
def test_ipc_handles_the_showcase_and_the_hud(tmp_path: Path) -> None:
    log = _run([str(HARNESS / "check_ipc.sh")], tmp_path)
    assert "IPC OK" in log, log
    assert not _qml_errors(log), log


# --- section 28: drop on the orb, the dock under the pill, the round's events ------------------------------------


def test_the_ui_speaks_the_section_28_contract() -> None:
    ipc = (UI / "Ipc.qml").read_text()
    for ev in ("attach.state", "meeting.state", "notify.unseen", "focus.state", "search.results", "memory.saved"):
        assert f'case "{ev}":' in ipc, ev
    for key in ("msg.attach", "msg.meeting", "msg.notify", "msg.focus"):  # read from the snapshot on (re)connect
        assert key in ipc, key
    sent = "".join((UI / f).read_text() for f in ("CornerPill.qml", "PillDockView.qml", "hud/TodayPanel.qml",
                                                 "hud/ConversationPanel.qml"))
    for cmd in ("attach.add", "attach.clear", "meeting.stop", "notify.summary", "notify.clear", "focus.pause",
                "focus.stop", "search.open"):
        assert f'"{cmd}"' in sent, cmd


def test_a_drop_is_only_ever_a_copy() -> None:
    pill = (UI / "CornerPill.qml").read_text()
    block = pill[pill.index("DropArea {"):]
    block = block[:block.index("\n        }\n")]
    assert block.count("Qt.CopyAction") == 2 and "MoveAction" not in pill and "acceptProposedAction" not in block


def test_the_dock_moves_the_cards_down() -> None:
    shell = (UI / "shell.qml").read_text()
    assert "PillDock {" in shell and shell.count("stackOffset: dock.stackHeight") == 2
    for f in ("DraftCard.qml", "ReadingPanel.qml"):
        assert "top: Theme.barTop + Theme.pillHeight + 8 + stackOffset" in (UI / f).read_text()


def test_a_recording_keeps_the_pill_visible_over_fullscreen() -> None:
    pill = (UI / "CornerPill.qml").read_text()
    active = pill[pill.index("readonly property bool jarvisActive"):]
    assert "recording" in active[:active.index("visible:")]


@needs_qml
@pytest.mark.parametrize("corners,variant", [("1", "files"), ("0", "region"), ("1", "results"), ("0", "all")])
def test_dock_offscreen(corners: str, variant: str, tmp_path: Path) -> None:
    out = tmp_path / "dock.png"
    log = _run([str(HARNESS / "render_dock.sh"), str(out), variant], tmp_path, HUD_ROUND=corners)
    assert "DOCK OK (" + ("round" if corners == "1" else "square") + ")" in log, log
    assert not _qml_errors(log), log


@needs_qml
def test_hud_shows_meeting_focus_results_and_attachments(tmp_path: Path) -> None:
    out = tmp_path / "s28.png"
    log = _run([str(HARNESS / "render.sh"), "1920", "1080", "s28", "full", str(out)], tmp_path,
               HUD_SOFTWARE="1", HUD_DELAY="700", HUD_ROUND="1")
    assert out.is_file() and out.stat().st_size > 50_000, log
    assert not _qml_errors(log), log
