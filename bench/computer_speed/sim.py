"""Section 23: a closed-loop sandbox that needs no real screen, mouse or keyboard.

The sandbox pages (bench/computer_speed/pages/) run in a headless Chromium (a private profile in a temp dir, no
GPU, muted). `SimScreen` hands the loop Chromium's screenshots of a 2560x1440 viewport (the user's monitor size, so
the model sees the same scale as on the desktop) and `SimActuator` turns the loop's clicks, scrolls, typing and key
presses into DevTools input events. The loop code under test is the real `ComputerLoop`; only the screen and the
input devices are swapped. Success is read from the page title, exactly like the real sandbox reads the Zen window.

Needs the `websockets` package: `uv run --with websockets bench/computer_speed/bench.py ...`.
"""

from __future__ import annotations

import asyncio
import base64
import itertools
import json
import os
import shutil
import signal
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

import httpx

from jarvis.integrations import computer as comp
from jarvis.integrations.desktop import Window

PAGES = Path(__file__).resolve().parent / "pages"
W, H = 2560, 1440


class CDP:
    def __init__(self, ws: Any) -> None:
        self.ws = ws
        self.ids = itertools.count(1)
        self.pending: dict[int, asyncio.Future[Any]] = {}
        self.reader = asyncio.create_task(self._read())

    async def _read(self) -> None:
        async for raw in self.ws:
            msg = json.loads(raw)
            fut = self.pending.pop(msg.get("id", -1), None)
            if fut is not None and not fut.done():
                if "error" in msg:
                    fut.set_exception(RuntimeError(str(msg["error"])))
                else:
                    fut.set_result(msg.get("result", {}))

    async def call(self, method: str, **params: Any) -> Any:
        i = next(self.ids)
        fut: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self.pending[i] = fut
        await self.ws.send(json.dumps({"id": i, "method": method, "params": params}))
        return await asyncio.wait_for(fut, 30)


class Browser:
    """One headless Chromium with one page."""

    def __init__(self, port: int = 9333) -> None:
        self.port = port
        self.proc: subprocess.Popen[bytes] | None = None
        self.tmp = tempfile.mkdtemp(prefix="jarvis-sim-")
        self.cdp: CDP | None = None

    async def start(self) -> None:
        self.proc = subprocess.Popen(
            ["chromium", "--headless=new", f"--remote-debugging-port={self.port}", f"--user-data-dir={self.tmp}",
             "--disable-gpu", "--mute-audio", "--no-first-run", "--no-default-browser-check",
             f"--window-size={W},{H}", "--hide-crash-restore-bubble", "about:blank"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        async with httpx.AsyncClient(timeout=2) as http:
            for _ in range(100):
                try:
                    targets = (await http.get(f"http://127.0.0.1:{self.port}/json/list")).json()
                    pages = [t for t in targets if t.get("type") == "page"]
                    if pages:
                        break
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(0.1)
            else:
                raise RuntimeError("chromium didn't start")
        import websockets

        ws = await websockets.connect(pages[0]["webSocketDebuggerUrl"], max_size=64 * 1024 * 1024)
        self.cdp = CDP(ws)
        await self.cdp.call("Page.enable")
        await self.cdp.call("Emulation.setDeviceMetricsOverride", width=W, height=H, deviceScaleFactor=1,
                            mobile=False)

    async def goto(self, page: str) -> None:
        assert self.cdp is not None
        await self.cdp.call("Page.navigate", url=(PAGES / page).as_uri())
        await asyncio.sleep(0.4)

    async def title(self) -> str:
        assert self.cdp is not None
        r = await self.cdp.call("Runtime.evaluate", expression="document.title", returnByValue=True)
        return str(r.get("result", {}).get("value", ""))

    async def png(self) -> bytes:
        assert self.cdp is not None
        for _ in range(20):  # during a navigation the capture can stall: retry rather than wait 30 s
            try:
                r = await asyncio.wait_for(self.cdp.call("Page.captureScreenshot", format="png"), 1.5)
                break
            except (TimeoutError, RuntimeError):
                await asyncio.sleep(0.05)
        else:
            raise RuntimeError("no screenshot")
        return base64.b64decode(r["data"])

    async def close(self) -> None:
        if self.cdp is not None:
            self.cdp.reader.cancel()
            try:
                await asyncio.wait_for(self.cdp.ws.close(), 3)
            except BaseException:  # noqa: BLE001
                pass
        if self.proc is not None:
            try:
                os.killpg(self.proc.pid, signal.SIGTERM)
                self.proc.wait(10)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                os.killpg(self.proc.pid, signal.SIGKILL)
        shutil.rmtree(self.tmp, ignore_errors=True)


class _Runner:
    async def run(self, argv: list[str], timeout: float = 10) -> tuple[int, str, str]:
        raise RuntimeError(f"the sim runs nothing: {argv}")


class SimScreen(comp.Screen):
    def __init__(self, browser: Browser, width: int = 1280, quality: int = 80) -> None:
        async def grab(_output: str) -> bytes:
            return await browser.png()

        super().__init__(_Runner(), width=width, quality=quality, grab=grab)
        self.grab_s: list[float] = []

    async def monitor(self, max_age_s: float = 1.0) -> comp.Monitor:
        return comp.Monitor("sim", 0, 0, W, H, ("7",))


class _Pointer:
    async def release_all(self) -> None:
        pass


_KEYCODES = {"Return": ("Enter", 13, "\r"), "Tab": ("Tab", 9, ""), "Escape": ("Escape", 27, ""),
             "BackSpace": ("Backspace", 8, ""), "Delete": ("Delete", 46, ""), "Up": ("ArrowUp", 38, ""),
             "Down": ("ArrowDown", 40, ""), "Left": ("ArrowLeft", 37, ""), "Right": ("ArrowRight", 39, ""),
             "Home": ("Home", 36, ""), "End": ("End", 35, ""), "space": (" ", 32, " "),
             "Prior": ("PageUp", 33, ""), "Next": ("PageDown", 34, "")}


class SimActuator:
    """The loop's Actuator, driving the headless page through DevTools input events."""

    def __init__(self, browser: Browser) -> None:
        self.b = browser
        self.pointer = _Pointer()
        self.x = W // 2
        self.y = H // 2
        self.log: list[str] = []

    @property
    def cdp(self) -> CDP:
        assert self.b.cdp is not None
        return self.b.cdp

    async def move(self, x: int, y: int) -> None:
        self.x, self.y = int(x), int(y)
        await self.cdp.call("Input.dispatchMouseEvent", type="mouseMoved", x=self.x, y=self.y)

    async def click(self, x: int | None, y: int | None, button: str = "left", count: int = 1) -> None:
        if x is not None and y is not None:
            await self.move(x, y)
        for i in range(1, max(1, count) + 1):
            for kind in ("mousePressed", "mouseReleased"):
                await self.cdp.call("Input.dispatchMouseEvent", type=kind, x=self.x, y=self.y, button=button,
                                    clickCount=i)
        self.log.append(f"click {button} {self.x},{self.y} x{count}")

    async def drag(self, x1: int, y1: int, x2: int, y2: int) -> None:
        await self.move(x1, y1)
        await self.cdp.call("Input.dispatchMouseEvent", type="mousePressed", x=x1, y=y1, button="left", clickCount=1)
        for i in range(1, 9):
            await self.cdp.call("Input.dispatchMouseEvent", type="mouseMoved", x=x1 + (x2 - x1) * i // 8,
                                y=y1 + (y2 - y1) * i // 8, button="left", buttons=1)
        await self.cdp.call("Input.dispatchMouseEvent", type="mouseReleased", x=x2, y=y2, button="left", clickCount=1)

    async def scroll(self, x: int | None, y: int | None, direction: str, amount: int = 3) -> None:
        if x is not None and y is not None:
            await self.move(x, y)
        n = max(1, min(int(amount or 3), 15))
        dy = {"down": 100, "up": -100}.get(direction, 0) * n
        dx = {"right": 100, "left": -100}.get(direction, 0) * n
        await self.cdp.call("Input.dispatchMouseEvent", type="mouseWheel", x=self.x, y=self.y, deltaX=dx, deltaY=dy)
        self.log.append(f"scroll {direction} {n}")

    async def type_text(self, text: str) -> None:
        import re

        clean = re.sub(r"[\r\n]+", " ", str(text))
        if clean:
            await self.cdp.call("Input.insertText", text=clean)
        self.log.append(f"type {clean!r}")

    async def keys(self, combo: comp.Combo) -> None:
        mods = set(combo.mods)
        flags = (2 if "ctrl" in mods else 0) | (8 if "shift" in mods else 0) | (1 if "alt" in mods else 0)
        self.log.append(f"key {combo.label}")
        if mods & {"logo"} or ("alt" in mods and combo.key == "Tab"):
            return  # desktop shortcuts (the launcher, alt+tab) do nothing inside the page
        if combo.key == "a" and mods == {"ctrl"}:
            await self.cdp.call("Input.dispatchKeyEvent", type="keyDown", key="a", code="KeyA",
                                windowsVirtualKeyCode=65, modifiers=2, commands=["selectAll"])
            await self.cdp.call("Input.dispatchKeyEvent", type="keyUp", key="a", code="KeyA",
                                windowsVirtualKeyCode=65, modifiers=2)
            return
        key, code, text = _KEYCODES.get(combo.key, (combo.key, ord(combo.key.upper()[0]), combo.key))
        down: dict[str, Any] = {"type": "keyDown" if text and not flags & 3 else "rawKeyDown", "key": key,
                                "windowsVirtualKeyCode": code, "modifiers": flags}
        if text and not flags & 3:
            down["text"] = text
        await self.cdp.call("Input.dispatchKeyEvent", **down)
        await self.cdp.call("Input.dispatchKeyEvent", type="keyUp", key=key, windowsVirtualKeyCode=code,
                            modifiers=flags)

    async def active_window(self) -> Window:
        title = await self.b.title()
        return Window("0xsim", "zen", f"{title} — Zen Browser", "7", 1, True)


async def smoke() -> None:
    b = Browser()
    await b.start()
    try:
        await b.goto("form.html")
        act = SimActuator(b)
        screen = SimScreen(b)
        t0 = time.monotonic()
        shot = await screen.capture()
        print("capture", round(time.monotonic() - t0, 3), shot.width, shot.height)
        t0 = time.monotonic()
        await screen.quick()
        print("quick", round(time.monotonic() - t0, 3))
        await act.click(1035, 202)  # the name field (page coordinates)
        await act.type_text("Ada")
        await act.keys(comp.parse_combo("tab"))
        await act.type_text("a@b.c")
        await act.keys(comp.parse_combo("enter"))
        await asyncio.sleep(0.2)
        print(await b.title())
    finally:
        await b.close()


if __name__ == "__main__":
    asyncio.run(smoke())
