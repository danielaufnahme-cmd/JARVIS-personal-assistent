"""Install the fine-tuned 2B into JARVIS (run by overnight.sh at the end, or by hand: finetune/install_model.sh).

1. Backs up ~/.config/llama-swap/config.yaml (.bak-jarvis-<timestamp>) and makes ONE edit: the `qwen35-2b-jarvis`
   model (the `small` flags, ttl 0) and the matrix set `(qwen35-4b | qwen35-2b | qwen35-2b-jarvis) & jarvis`.
   Validated by parsing the YAML (and by llama-swap listing the model); restored on any error. Idempotent.
2. Adds it to the pill menu: `[llm] fast_models` in ~/.config/jarvis/config.toml (created or merged; other keys kept).
3. Only if docs/bench_finetune.json says `"recommend_switch": true` (report.py: >= 95 % tool calls, 100 % safety,
   style no worse than the 4B): `"llm_fast_model": "qwen35-2b-jarvis"` in ~/.local/state/jarvis/state.json.
4. Checks the model loads in llama-swap (only while jarvisd is stopped: loading it would evict the resident voice
   model) and that jarvisd starts cleanly with the new settings (it is (re)started; `--no-restart` skips that).
   Any failure rolls every file back and restarts jarvisd on the old settings.
5. Writes a plain-English paragraph to finetune/RESULT.txt.

Test hooks (the environment): FT_SWAP_CONFIG, FT_JARVIS_CONFIG, FT_STATE_FILE, FT_BENCH_JSON, FT_RESULT,
FT_JARVISD_UNIT; `--no-services` skips llama-swap and jarvisd entirely (for copies in a scratch directory).
Exit codes: 0 installed (or already installed), 2 refused (no model / no results; nothing changed), 1 rolled back.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import tomllib
import urllib.request
from pathlib import Path
from typing import Any

import yaml

HOME = Path.home()
FT = Path(__file__).resolve().parent
ROOT = FT.parent
NAME = "qwen35-2b-jarvis"
SWAP = os.environ.get("FT_LLAMA_SWAP_URL", "http://127.0.0.1:8401")
SWAP_CONFIG = Path(os.environ.get("FT_SWAP_CONFIG", HOME / ".config/llama-swap/config.yaml"))
JARVIS_CONFIG = Path(os.environ.get("FT_JARVIS_CONFIG", HOME / ".config/jarvis/config.toml"))
STATE_FILE = Path(os.environ.get("FT_STATE_FILE", HOME / ".local/state/jarvis/state.json"))
BENCH = Path(os.environ.get("FT_BENCH_JSON", ROOT / "docs/bench_finetune.json"))
RESULT = Path(os.environ.get("FT_RESULT", FT / "RESULT.txt"))
UNIT = os.environ.get("FT_JARVISD_UNIT", "jarvisd")
DEFAULT_FAST_MODELS = ["qwen35-4b", "qwen35-2b"]
STAMP = time.strftime("%Y%m%d-%H%M%S")


def log(msg: str) -> None:
    print(f"{time.strftime('%H:%M:%S')} install: {msg}", flush=True)


class Fail(Exception):
    pass


# --- llama-swap config: one text edit, keeps comments -------------------------------------------------------------

def swap_edit(text: str, gguf: Path) -> str:
    lines = text.splitlines(keepends=True)
    try:
        start = next(i for i, l in enumerate(lines) if re.match(r"^models:\s*(#.*)?$", l))
    except StopIteration:
        raise Fail("no top-level `models:` in the llama-swap config") from None
    end = next((i for i in range(start + 1, len(lines)) if re.match(r"^[^\s#]", lines[i])), len(lines))
    ins = end
    while ins > start + 1 and (not lines[ins - 1].strip() or lines[ins - 1].lstrip().startswith("#")):
        ins -= 1  # before the blank/comment lines that belong to the next top-level key
    entry = ("  # Section 16: Qwen3.5-2B fine-tuned on JARVIS's tool calls (docs/finetune_2b.md). Added by "
             "finetune/install_model.sh.\n"
             f"  {NAME}:\n    cmd: ${{small}} -m {gguf}\n    ttl: 0\n")
    if ins > 0 and not lines[ins - 1].endswith("\n"):
        lines[ins - 1] += "\n"
    lines.insert(ins, entry)
    out = "".join(lines)
    m = re.search(r'^(\s+voice:\s*")\(([^)"]*)\)(\s*&\s*jarvis"\s*)$', out, re.M)
    if not m:
        raise Fail('no matrix line like `voice: "(qwen35-4b | qwen35-2b) & jarvis"` in the llama-swap config')
    members = [x.strip() for x in m.group(2).split("|") if x.strip()]
    if NAME not in members:
        members.append(NAME)
    out = out[: m.start()] + f'{m.group(1)}({" | ".join(members)}){m.group(3)}' + out[m.end():]
    return out


def swap_check(before: dict[str, Any] | None, text: str, gguf: Path) -> None:
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise Fail(f"the edited llama-swap config doesn't parse: {exc}") from None
    models = (data or {}).get("models") or {}
    ent = models.get(NAME) or {}
    if str(gguf) not in str(ent.get("cmd", "")) or "${small}" not in str(ent.get("cmd", "")) or ent.get("ttl") != 0:
        raise Fail(f"the {NAME} entry didn't come out as intended: {ent!r}")
    sets = (((data.get("routing") or {}).get("router") or {}).get("settings") or {}).get("matrix", {}).get("sets", {})
    if NAME not in str(sets.get("voice", "")):
        raise Fail(f"the matrix set doesn't include {NAME}: {sets!r}")
    if before is not None:
        old = dict(before)
        new = dict(data)
        new_models = {k: v for k, v in (new.get("models") or {}).items() if k != NAME}
        if new_models != (old.get("models") or {}):
            raise Fail("the edit changed other models in the llama-swap config")
        for k in set(old) | set(new):
            if k not in ("models", "routing") and old.get(k) != new.get(k):
                raise Fail(f"the edit changed `{k}` in the llama-swap config")


def swap_dry_load(text: str) -> None:
    """Have the real llama-swap binary load the edited config on a private port (no model starts) before the live
    config is touched."""
    binary = HOME / ".local/bin/llama-swap"
    if not binary.exists():
        return
    tmp = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp")) / f"llama-swap-check-{os.getpid()}.yaml"
    tmp.write_text(text, encoding="utf-8")
    proc = subprocess.Popen([str(binary), "-config", str(tmp), "-listen", "127.0.0.1:8436"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    try:
        for _ in range(30):
            time.sleep(0.5)
            if proc.poll() is not None:
                raise Fail(f"llama-swap rejects the edited config: {(proc.stderr.read() or '')[-300:]}")
            try:
                with urllib.request.urlopen("http://127.0.0.1:8436/v1/models", timeout=2) as r:
                    if f'"{NAME}"' in r.read().decode():
                        return
            except Exception:  # noqa: BLE001
                continue
        raise Fail("llama-swap didn't list the new model with the edited config")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        tmp.unlink(missing_ok=True)


def swap_installed(text: str, gguf: Path) -> bool:
    try:
        swap_check(None, text, gguf)
        return True
    except Fail:
        return False


# --- the user config (TOML): merge [llm] fast_models -----------------------------------------------------------------

def toml_list(items: list[str]) -> str:
    return "[" + ", ".join(json.dumps(x) for x in items) + "]"


def toml_edit(text: str | None) -> tuple[str, list[str]] | None:
    """New text with `NAME` added to [llm] fast_models, or None if it's already there."""
    if text is None:
        items = DEFAULT_FAST_MODELS + [NAME]
        return ("# JARVIS user settings (overrides config.example.toml).\n\n[llm]\n"
                f"fast_models = {toml_list(items)}   # added by finetune/install_model.sh\n", items)
    data = tomllib.loads(text)
    current = list((data.get("llm") or {}).get("fast_models") or [])
    if NAME in current:
        return None
    items = (current or DEFAULT_FAST_MODELS) + [NAME]
    line = f"fast_models = {toml_list(items)}   # added by finetune/install_model.sh\n"
    lines = text.splitlines(keepends=True)
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    hdr = next((i for i, l in enumerate(lines) if re.match(r"^\s*\[llm\]\s*(#.*)?$", l)), None)
    if hdr is None:
        lines += ["\n", "[llm]\n", line]
    else:
        end = next((i for i in range(hdr + 1, len(lines)) if re.match(r"^\s*\[", lines[i])), len(lines))
        k = next((i for i in range(hdr + 1, end) if re.match(r"^\s*fast_models\s*=", lines[i])), None)
        if k is None:
            lines.insert(hdr + 1, line)
        else:
            j = k
            if "[" in lines[k] and "]" not in lines[k].split("#")[0]:
                while j + 1 < end and "]" not in lines[j]:
                    j += 1
            lines[k:j + 1] = [line]
    return "".join(lines), items


def toml_check(before: str | None, after: str, items: list[str]) -> None:
    try:
        new = tomllib.loads(after)
    except tomllib.TOMLDecodeError as exc:
        raise Fail(f"the edited JARVIS config doesn't parse: {exc}") from None
    if list(new.get("llm", {}).get("fast_models", [])) != items:
        raise Fail("fast_models didn't come out as intended in the JARVIS config")
    if before is not None:
        old = tomllib.loads(before)
        new.get("llm", {}).pop("fast_models", None)
        old.get("llm", {}).pop("fast_models", None)
        if not new.get("llm"):
            new.pop("llm", None)
        if not old.get("llm"):
            old.pop("llm", None)
        if old != new:
            raise Fail("the edit changed other settings in the JARVIS config")


# --- services --------------------------------------------------------------------------------------------------------

def http(method: str, path: str, body: dict | None = None, timeout: float = 10) -> tuple[int, str]:
    req = urllib.request.Request(SWAP + path, method=method, data=json.dumps(body).encode() if body else None,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001
        return 0, str(exc)


def swap_lists_model(timeout_s: float = 30) -> bool:
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout_s:
        code, text = http("GET", "/v1/models")
        if code == 200 and f'"{NAME}"' in text:
            return True
        time.sleep(1)
    return False


def systemctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True, timeout=120)


def jarvisd_active() -> bool:
    return systemctl("is-active", UNIT).stdout.strip() == "active"


def brain_get(timeout: float = 5) -> dict[str, Any] | None:
    """jarvisd's `llm.brain.get` result over its socket (None if it doesn't answer)."""
    import socket

    path = os.environ.get("JARVIS_SOCKET") or str(Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"))
                                                  / "jarvis.sock")
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            sock.connect(path)
            sock.sendall(b'{"cmd":"llm.brain.get","req":"install"}\n')
            buf = b""
            t0 = time.monotonic()
            while time.monotonic() - t0 < timeout:
                chunk = sock.recv(65536)
                if not chunk:
                    return None
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    try:
                        ev = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if ev.get("ev") == "ack" and ev.get("req") == "install":
                        return ev.get("result") if ev.get("ok") else None
    except OSError:
        return None
    return None


def jarvisd_start_ok(expect_fast: str | None) -> tuple[bool, str]:
    """(Re)start jarvisd and check it stays up, answers on its socket and uses the expected fast model."""
    r = systemctl("restart", UNIT)
    if r.returncode != 0:
        return False, f"systemctl restart {UNIT} failed: {r.stderr.strip()}"
    n0 = systemctl("show", UNIT, "-p", "NRestarts", "--value").stdout.strip()
    info: dict[str, Any] | None = None
    for _ in range(90):
        time.sleep(1)
        if jarvisd_active():
            info = brain_get()
            if info is not None:
                break
    else:
        return False, f"{UNIT} didn't come up and answer on its socket within 90 s"
    time.sleep(15)  # it must stay up
    n1 = systemctl("show", UNIT, "-p", "NRestarts", "--value").stdout.strip()
    if not jarvisd_active() or n1 != n0:
        return False, f"{UNIT} crashed after starting (restarts {n0} -> {n1})"
    if expect_fast and info.get("fast_model") != expect_fast:
        return False, f"{UNIT} started but its fast model is {info.get('fast_model')!r}, not {expect_fast}"
    return True, json.dumps(info)


def load_test() -> tuple[bool, str]:
    code, text = http("POST", "/v1/chat/completions", {"model": NAME, "max_tokens": 1,
                                                     "messages": [{"role": "user", "content": "hi"}],
                                                     "chat_template_kwargs": {"enable_thinking": False}}, timeout=180)
    http("POST", f"/api/models/unload/{NAME}", timeout=60)
    return code == 200, f"HTTP {code}: {text[:200]}"


# --- main ------------------------------------------------------------------------------------------------------------

def backup(path: Path) -> Path | None:
    if not path.exists():
        return None
    dst = path.with_name(f"{path.name}.bak-jarvis-{STAMP}")
    shutil.copy2(path, dst)
    return dst


def write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp-install")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def numbers(bench: dict[str, Any]) -> str:
    s = bench.get("summary", {})
    q = "tuned-q5" if bench.get("recommended_quant") == "Q5_K_M" else "tuned-q4"
    t, f, b = s.get(q, {}), s.get("4b", {}), s.get("base-2b", {})
    return (f"On the held-out test the tuned 2B called the right tool {t.get('tool_acc')} % of the time (the untuned 2B "
            f"{b.get('tool_acc')} %, the 4B {f.get('tool_acc')} %), safety {t.get('safety')}, style {t.get('style_pct')} %, "
            f"at {t.get('median_tok_s')} tokens/s (4B: {f.get('median_tok_s')}) and {t.get('vram_mb')} MiB of VRAM "
            f"(4B: {f.get('vram_mb')} MiB).")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-services", action="store_true", help="only edit the files (for tests on copies)")
    ap.add_argument("--no-restart", action="store_true", help="don't (re)start jarvisd to check the new settings")
    args = ap.parse_args()

    if not BENCH.is_file():
        log(f"refusing: no results at {BENCH} (the training hasn't finished); nothing changed")
        return 2
    bench = json.loads(BENCH.read_text())
    gguf = Path(bench.get("install_gguf") or HOME / "models/Qwen3.5-2B-jarvis-Q4_K_M.gguf")
    if not gguf.is_file():
        log(f"refusing: the model file {gguf} doesn't exist; nothing changed")
        return 2
    switch = bool(bench.get("recommend_switch"))
    services = not args.no_services
    was_active = services and jarvisd_active()
    log(f"model {gguf.name}; recommendation passes the bar: {switch}; jarvisd running: {was_active}")

    backups: dict[Path, Path | None] = {}
    created: list[Path] = []
    changed: list[str] = []
    try:
        # 1. llama-swap
        swap_text = SWAP_CONFIG.read_text(encoding="utf-8")
        if swap_installed(swap_text, gguf):
            log("llama-swap: already has the entry and the matrix set (skipped)")
        else:
            if re.search(rf"^\s+{re.escape(NAME)}:\s*$", swap_text, re.M):
                raise Fail(f"llama-swap already has a {NAME} entry that doesn't match {gguf}; fix it by hand")
            new = swap_edit(swap_text, gguf)
            swap_check(yaml.safe_load(swap_text), new, gguf)
            swap_dry_load(new)
            backups[SWAP_CONFIG] = backup(SWAP_CONFIG)
            write_atomic(SWAP_CONFIG, new)
            changed.append("llama-swap")
            log(f"llama-swap: added {NAME} + the matrix set (backup {backups[SWAP_CONFIG].name})")
        if services:
            if not swap_lists_model():
                raise Fail("llama-swap didn't pick up the new model within 30 s (config not reloaded?)")
            if not jarvisd_active():
                ok, why = load_test()
                if not ok:
                    raise Fail(f"llama-swap couldn't load {NAME}: {why}")
                log(f"llama-swap: {NAME} loads and answers")
            else:
                log("llama-swap lists the model (load test skipped: jarvisd is running and loading it would evict "
                    "the resident voice model)")
        # 2. the pill menu
        old_toml = JARVIS_CONFIG.read_text(encoding="utf-8") if JARVIS_CONFIG.exists() else None
        edit = toml_edit(old_toml)
        if edit is None:
            log("JARVIS config: fast_models already lists it (skipped)")
        else:
            new_toml, items = edit
            toml_check(old_toml, new_toml, items)
            backups[JARVIS_CONFIG] = backup(JARVIS_CONFIG)
            if old_toml is None:
                created.append(JARVIS_CONFIG)
            write_atomic(JARVIS_CONFIG, new_toml)
            changed.append("pill menu")
            log(f"JARVIS config: fast_models = {items}")
        # 3. the active fast model
        if switch:
            state = {}
            if STATE_FILE.exists():
                state = json.loads(STATE_FILE.read_text() or "{}")
                if not isinstance(state, dict):
                    raise Fail("state.json isn't a JSON object")
            if state.get("llm_fast_model") != NAME:
                backups[STATE_FILE] = backup(STATE_FILE)
                if not STATE_FILE.exists():
                    created.append(STATE_FILE)
                state["llm_fast_model"] = NAME
                write_atomic(STATE_FILE, json.dumps(state, indent=2))
                changed.append("active")
                log("state.json: llm_fast_model = qwen35-2b-jarvis (the active fast model)")
            else:
                log("state.json: already active (skipped)")
        # 4. jarvisd with the new settings
        if services and not args.no_restart:
            ok, info = jarvisd_start_ok(NAME if switch else None)
            if not ok:
                raise Fail(info)
            log(f"jarvisd started cleanly: {info[:300]}")
    except Exception as exc:  # noqa: BLE001 - anything: put every file back
        why = str(exc) if isinstance(exc, Fail) else f"{type(exc).__name__}: {exc}"
        log(f"ERROR: {why}; rolling back")
        for path, bak in backups.items():
            if bak is not None:
                shutil.copy2(bak, path)
        for path in created:
            path.unlink(missing_ok=True)
        restarted = ""
        if services and (was_active or not args.no_restart):
            ok = systemctl("restart", UNIT).returncode == 0
            restarted = " JARVIS was restarted with its previous settings." if ok else \
                f" Restarting JARVIS failed too: run `systemctl --user restart {UNIT}`."
        RESULT.write_text(
            f"The training finished, but installing the tuned model failed and was undone, so JARVIS is exactly as "
            f"before (the 4B is still the voice model). Reason: {why}.{restarted} {numbers(bench)} The model file is "
            f"{gguf}; details in {ROOT / 'docs/finetune_2b.md'}. To retry: ~/jarvis/finetune/install_model.sh\n",
            encoding="utf-8")
        return 1

    active = switch
    how_back = ("To switch back, right-click the JARVIS orb, then Fast model, then qwen35-4b." if active else
                "To try it anyway, right-click the JARVIS orb, then Fast model, then qwen35-2b-jarvis; the same menu "
                "switches back to qwen35-4b.")
    verdict = ("It passed the bar (at least 95 % correct tool calls, full safety, style as good as the 4B), so it is "
               "now JARVIS's active voice model." if active else
               "It did not pass the bar (at least 95 % correct tool calls, full safety, style as good as the 4B), so "
               "the 4B stays the active voice model; the tuned model is installed and can be picked in the menu.")
    RESULT.write_text(f"The training finished and the tuned Qwen3.5-2B ({gguf.name}) is installed in llama-swap as "
                      f"'{NAME}' and in the pill menu. {verdict} {numbers(bench)} {how_back} Full report: "
                      f"{ROOT / 'docs/finetune_2b.md'}. Changed: {', '.join(changed) or 'nothing (already installed)'}; "
                      f"backups end in .bak-jarvis-{STAMP}.\n", encoding="utf-8")
    log(f"done; {RESULT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
