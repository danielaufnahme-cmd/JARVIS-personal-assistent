"""OpenAI-compatible client for llama-swap (production) or Ollama (development).

Two modes: voice (thinking off, short) and deep (thinking on, long). Since section 12 two models serve them:
`LLMRouter` sends voice turns to a small fast model and deep mode (plus any fallback) to the 35B. The thinking
switch is backend specific and was verified by hand:
  - llama-server (llama-swap):  extra_body={"chat_template_kwargs": {"enable_thinking": bool}}
  - Ollama's /v1 endpoint ignores chat_template_kwargs; it honours extra_body={"reasoning_effort": "none"}
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import inspect
import json
import logging
import os
import re
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote, urlparse

import httpx
from openai import AsyncOpenAI

from jarvis.config import LLMConfig

log = logging.getLogger(__name__)

Mode = Literal["voice", "deep"]


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str = "{}"  # raw JSON text, as the model produced it

    @property
    def args(self) -> dict[str, Any]:
        try:
            value = json.loads(self.arguments or "{}")
        except json.JSONDecodeError:
            return {}
        return value if isinstance(value, dict) else {}


@dataclass
class ChatDelta:
    content: str = ""
    tool_calls: list[ToolCall] | None = None
    finish_reason: str | None = None
    reasoning: str = ""  # thinking text, when the server reports it separately (never spoken)


class _ThinkFilter:
    """Removes <think>…</think> from streamed content, for servers that don't split reasoning out."""

    OPEN, CLOSE = "<think>", "</think>"

    def __init__(self) -> None:
        self.inside = False
        self.buf = ""

    def feed(self, text: str) -> tuple[str, str]:
        self.buf += text
        visible, hidden = [], []
        while self.buf:
            tag = self.CLOSE if self.inside else self.OPEN
            idx = self.buf.find(tag)
            if idx >= 0:
                (hidden if self.inside else visible).append(self.buf[:idx])
                self.buf = self.buf[idx + len(tag) :]
                self.inside = not self.inside
                continue
            # Keep a possible partial tag at the end for the next chunk.
            keep = 0
            for n in range(min(len(tag) - 1, len(self.buf)), 0, -1):
                if tag.startswith(self.buf[-n:]):
                    keep = n
                    break
            out, self.buf = self.buf[: len(self.buf) - keep], self.buf[len(self.buf) - keep :]
            (hidden if self.inside else visible).append(out)
            break
        return "".join(visible), "".join(hidden)

    def flush(self) -> str:
        out, self.buf = ("" if self.inside else self.buf), ""
        return out


class LLM:
    def __init__(
        self,
        cfg: LLMConfig,
        *,
        client: AsyncOpenAI | None = None,
        transport: httpx.AsyncBaseTransport | None = None,  # tests inject a mock transport here
    ) -> None:
        self.cfg = cfg
        self._transport = transport
        self.last_use: float = 0.0
        self.loading: bool = False  # True between the start of a request and its first token
        self.last_tok_s: float | None = None
        self.active = 0  # requests streaming right now (an idle unload must wait for them)
        self.last_warm: dict[str, Any] | None = None  # how the last warm-up went (section 20)
        self._unloaded = True
        self._client = client or AsyncOpenAI(
            base_url=cfg.base_url,
            api_key=getattr(cfg, "api_key", "") or "sk-local",
            timeout=httpx.Timeout(getattr(cfg, "request_timeout_s", 300), connect=5.0),
            max_retries=0,
            http_client=httpx.AsyncClient(transport=transport) if transport is not None else None,
        )
        self.backend = self._detect_backend()

    # --- backend details --------------------------------------------------------

    def _detect_backend(self) -> Literal["llama-swap", "ollama"]:
        configured = getattr(self.cfg, "backend", "auto")
        if configured in ("llama-swap", "ollama"):
            return configured
        return "ollama" if urlparse(self.cfg.base_url).port == 11434 else "llama-swap"

    @property
    def _root(self) -> str:
        url = self.cfg.base_url.rstrip("/")
        return url[: -len("/v1")] if url.endswith("/v1") else url

    def thinking_body(self, enabled: bool) -> dict[str, Any]:
        knob = getattr(self.cfg, "thinking_knob", "auto")
        if knob == "auto":
            knob = "reasoning_effort" if self.backend == "ollama" else "chat_template_kwargs"
        if knob == "reasoning_effort":
            return {"reasoning_effort": "high" if enabled else "none"}
        return {"chat_template_kwargs": {"enable_thinking": enabled}}

    # --- load state ---------------------------------------------------------------

    def _touch(self) -> None:
        self.last_use = time.monotonic()
        self._unloaded = False

    def unload_in_s(self) -> int | None:
        if self.last_use == 0.0 or self._unloaded:
            return None
        left = self.cfg.idle_unload_s - (time.monotonic() - self.last_use)
        return max(0, int(left))

    async def warm_up(self, quiet: bool = False, prime: tuple[list[dict[str, Any]], list[dict[str, Any]]] | None = None,
                      slots: bool = False) -> None:
        """A 1-token request, so llama-swap starts loading the model while the user is still talking.

        `quiet` doesn't raise the `loading` flag: the section 12 pre-warm on an unverified wake trigger must not
        show anything (a rejected trigger just lets the model idle out). `prime` = (messages, tools): send JARVIS's
        real system prompt and tool schemas, so their ~8k-token prefill is in llama-server's prompt cache before
        the first question (measured: 3.9 s of TTFT on a freshly loaded 4B otherwise).
        `slots` (section 20, the on-demand voice model): nothing to do if the model is already loaded; else the
        load restores the saved KV of that prefix (25 ms) instead of prefilling it."""
        self.loading = not quiet
        self.active += 1
        t0 = time.monotonic()
        try:
            if slots and prime is not None and self.backend == "llama-swap":
                if await self.is_loaded():
                    self.last_warm = {"how": "loaded", "s": round(time.monotonic() - t0, 3)}
                    self._touch()
                    return
                try:
                    if await self._restore_prefix(*prime):
                        self._touch()
                        return
                except Exception:  # noqa: BLE001 - fall back to the plain primer below
                    log.warning("slot restore failed; priming the prompt instead", exc_info=True)
            messages, tools = prime if prime is not None else ([{"role": "user", "content": "hi"}], None)
            extra: dict[str, Any] = {"tools": tools} if tools else {}
            await self._client.chat.completions.create(
                model=self.cfg.model,
                messages=messages,
                max_tokens=1,
                extra_body=self.thinking_body(False),
                **extra,
            )
            self.last_warm = {"how": "primer", "s": round(time.monotonic() - t0, 3)}
            self._touch()
        except Exception:  # noqa: BLE001 - warm-up is best effort
            log.warning("LLM warm-up failed", exc_info=True)
        finally:
            self.active -= 1
            self.loading = False

    # --- section 20: slot save/restore of the fixed prompt prefix -------------------------------------------------

    def slot_dir(self) -> Path | None:
        raw = str(getattr(self.cfg, "slot_dir", "") or "")
        if not raw:
            return None
        runtime = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
        return Path(raw.replace("$XDG_RUNTIME_DIR", runtime)).expanduser()

    def slot_file(self, system: dict[str, Any], tools: list[dict[str, Any]] | None) -> str:
        """The slot file for this model + prefix: a new system prompt, tool list or model gets a new file."""
        blob = json.dumps([self.cfg.model, system, tools or []], sort_keys=True, ensure_ascii=False)
        stem = re.sub(r"[^A-Za-z0-9_.-]", "_", self.cfg.model)
        return f"{stem}-{hashlib.sha256(blob.encode()).hexdigest()[:16]}.bin"

    def has_slot(self, prime: tuple[list[dict[str, Any]], list[dict[str, Any]]]) -> bool:
        messages, tools = prime
        directory = self.slot_dir()
        return bool(messages) and directory is not None and (directory / self.slot_file(messages[0], tools)).is_file()

    async def ensure_slot(self, prime: tuple[list[dict[str, Any]], list[dict[str, Any]]]) -> None:
        """Save the prompt slot if there is none for this prefix yet (jarvisd, while idle after a start). Loads the
        model if needed; even an already loaded one gets the prefill, which a wake never does (it would compete
        with the user's turn)."""
        if self.has_slot(prime):
            return
        self.active += 1
        try:
            await self._restore_prefix(*prime)
            self._touch()
        except Exception:  # noqa: BLE001
            log.warning("building the prompt slot failed", exc_info=True)
        finally:
            self.active -= 1

    async def _restore_prefix(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None) -> bool:
        """Load the model through llama-swap and put the system prompt + tools KV into slot 0.

        Restore it from the slot file if there is one; else prefill exactly that prefix (never more: the hybrid
        Qwen3.5 can't roll its recurrent state back, so a cached prefix is only reused when the new prompt extends
        it token for token) and save it. A slot request goes to /upstream/<model>/..., which loads the model."""
        system = messages[0] if messages and messages[0].get("role") == "system" else None
        if system is None:
            return False
        name = self.slot_file(system, tools)
        up = f"{self._root}/upstream/{quote(self.cfg.model, safe='')}"
        directory = self.slot_dir()
        if directory is not None:
            try:
                directory.mkdir(parents=True, exist_ok=True)  # llama-server doesn't create it
            except OSError:
                log.debug("can't create %s", directory, exc_info=True)
        t0 = time.monotonic()
        timeout = httpx.Timeout(getattr(self.cfg, "request_timeout_s", 300), connect=5.0)
        async with httpx.AsyncClient(timeout=timeout, transport=self._transport) as http:
            resp = await http.post(f"{up}/slots/0?action=restore", json={"filename": name})
            if resp.status_code == 200:
                data = resp.json()
                ms = (data.get("timings") or {}).get("restore_ms")
                self.last_warm = {"how": "restore", "s": round(time.monotonic() - t0, 3),
                                  "tokens": data.get("n_restored"), "restore_ms": ms}
                await self._extend_prefix(http, up, messages, tools)
                log.info("%s loaded and its prompt prefix restored (%s tokens, restore %s ms%s, %.2f s in all)",
                         self.cfg.model, data.get("n_restored"), round(ms) if ms else "?",
                         f"; +{self.last_warm['history_tokens']} conversation tokens"
                         if self.last_warm.get("history_tokens") else "", time.monotonic() - t0)
                self.last_warm["s"] = round(time.monotonic() - t0, 3)
                return True
            # Nothing saved for this prefix yet (or saved by another llama-server build): prefill it once, save it.
            prefix = await self._render_prefix(http, up, [system], tools)
            if not prefix:
                return False
            t1 = time.monotonic()
            resp = await http.post(f"{up}/completion", json={"prompt": prefix, "n_predict": 0, "id_slot": 0,
                                                              "cache_prompt": True})
            resp.raise_for_status()
            prefill_s = time.monotonic() - t1
            saved = await http.post(f"{up}/slots/0?action=save", json={"filename": name})
            self.last_warm = {"how": "prefill", "s": round(time.monotonic() - t0, 3),
                              "tokens": resp.json().get("tokens_cached"), "prefill_s": round(prefill_s, 3)}
            if saved.status_code != 200:
                log.warning("saving the prompt slot failed (%s %s); is llama-server started with "
                            "--slot-save-path %s?", saved.status_code, saved.text[:200], directory)
            else:
                self._prune_slots(directory, name)
                log.info("%s: prompt prefix prefilled in %.2f s and saved as %s (the next load restores it)",
                         self.cfg.model, prefill_s, name)
            await self._extend_prefix(http, up, messages, tools)
            self.last_warm["s"] = round(time.monotonic() - t0, 3)
            return True

    async def _extend_prefix(self, http: httpx.AsyncClient, up: str, messages: list[dict[str, Any]],
                             tools: list[dict[str, Any]] | None) -> None:
        """A kept conversation (a follow-up within context_keep_s, after the model idled out): prefill it on top of
        the restored prefix now, while the user is still talking, instead of in the question's request. It extends
        slot 0 token for token, so nothing is rolled back."""
        history = messages[1:-1]  # messages = [system, *history, the primer's user message]
        if not history:
            return
        t1 = time.monotonic()
        prefix = await self._render_prefix(http, up, [messages[0], *history], tools)
        if not prefix:
            return
        resp = await http.post(f"{up}/completion", json={"prompt": prefix, "n_predict": 0, "id_slot": 0,
                                                          "cache_prompt": True})
        resp.raise_for_status()
        timings = resp.json().get("timings") or {}
        if self.last_warm is not None:
            self.last_warm.update(history_tokens=timings.get("prompt_n"), history_s=round(time.monotonic() - t1, 3))

    async def _render_prefix(self, http: httpx.AsyncClient, up: str, prefix: list[dict[str, Any]],
                             tools: list[dict[str, Any]] | None) -> str:
        """The chat template's text for `prefix` (the system message [+ a conversation]) + tools, cut before the next
        user turn: render two prompts that differ only in a following user message and keep what they share, up to
        the last special token (a special token is always its own token, so the cut is also a token boundary)."""
        rendered: list[str] = []
        for text in ("A", "B"):
            body: dict[str, Any] = {"messages": [*prefix, {"role": "user", "content": text}],
                                    **self.thinking_body(False)}
            if tools:
                body["tools"] = tools
            resp = await http.post(f"{up}/apply-template", json=body)
            resp.raise_for_status()
            rendered.append(str(resp.json().get("prompt", "")))
        a, b = rendered
        n = 0
        limit = min(len(a), len(b))
        while n < limit and a[n] == b[n]:
            n += 1
        cut = a.rfind("<|im_start|>", 0, n)
        return a[: cut if cut > 0 else n]

    def _prune_slots(self, directory: Path | None, keep: str, max_files: int = 2) -> None:
        """Keep the newest few slot files of this model (~180 MB each, in RAM): a pending draft changes the prompt
        for a while, and the usual one should still be there afterwards."""
        if directory is None or not directory.is_dir():
            return
        stem = keep.rsplit("-", 1)[0]
        files = sorted((f for f in directory.glob(f"{stem}-*.bin") if f.name != keep),
                       key=lambda f: f.stat().st_mtime, reverse=True)
        for old in files[max_files - 1:]:
            try:
                old.unlink()
            except OSError:
                log.debug("can't remove %s", old, exc_info=True)

    async def unload(self) -> None:
        try:
            async with httpx.AsyncClient(timeout=10, transport=self._transport) as http:
                if self.backend == "ollama":
                    resp = await http.post(
                        f"{self._root}/api/generate", json={"model": self.cfg.model, "keep_alive": 0}
                    )
                    resp.raise_for_status()
                else:
                    resp = await http.post(f"{self._root}/api/models/unload/{quote(self.cfg.model)}")
                    if resp.status_code == 404:  # older llama-swap: unload everything
                        resp = await http.get(f"{self._root}/unload")
                    resp.raise_for_status()
            self._unloaded = True
        except Exception:  # noqa: BLE001
            log.warning("LLM unload failed", exc_info=True)

    async def busy_elsewhere(self) -> bool:
        """Section 20: is another client (a script, another tool) using this model through llama-swap right now?
        jarvisd's idle clock only sees its own requests; unloading under someone else's request cuts it off, and
        their retry loads the model straight back (seen live: a 12 s load/unload loop). Asks llama-server's /slots,
        and only while llama-swap says the model is ready (an /upstream call would load it otherwise)."""
        if self.backend != "llama-swap":
            return False
        try:
            async with httpx.AsyncClient(timeout=2, transport=self._transport) as http:
                resp = await http.get(f"{self._root}/running")
                resp.raise_for_status()
                if not any(r.get("model") == self.cfg.model and r.get("state") == "ready"
                           for r in resp.json().get("running", [])):
                    return False
                resp = await http.get(f"{self._root}/upstream/{quote(self.cfg.model, safe='')}/slots")
                if resp.status_code != 200:
                    return False
                slots = resp.json()
        except Exception:  # noqa: BLE001
            log.debug("slot check failed", exc_info=True)
            return False
        return isinstance(slots, list) and any(isinstance(x, dict) and x.get("is_processing") for x in slots)

    async def is_loaded(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=3, transport=self._transport) as http:
                if self.backend == "ollama":
                    resp = await http.get(f"{self._root}/api/ps")
                    resp.raise_for_status()
                    names = {m.get("name") for m in resp.json().get("models", [])}
                    loaded = self.cfg.model in names or f"{self.cfg.model}:latest" in names
                else:
                    resp = await http.get(f"{self._root}/running")
                    resp.raise_for_status()
                    loaded = any(
                        r.get("model") == self.cfg.model and r.get("state", "ready") == "ready"
                        for r in resp.json().get("running", [])
                    )
        except Exception:  # noqa: BLE001
            return False
        if not loaded:
            self._unloaded = True
        return loaded

    # --- chat -------------------------------------------------------------------------

    async def complete(
        self, messages: list[dict[str, Any]], *, max_tokens: int = 400, temperature: float = 0.2,
        json_object: bool = False,
    ) -> str:
        """One non-streamed answer without thinking (section 19's vision steps; messages may hold image_url parts).
        Counts as use, and `active` holds off jarvisd's idle unload while it runs."""
        self._touch()
        self.active += 1
        try:
            resp = await self._client.chat.completions.create(
                model=self.cfg.model, messages=messages, max_tokens=max_tokens, temperature=temperature,
                extra_body=self.thinking_body(False),
                **({"response_format": {"type": "json_object"}} if json_object else {}),
            )
        finally:
            self.active -= 1
            self._touch()
        return (resp.choices[0].message.content or "") if resp.choices else ""

    async def stream_chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        mode: Mode,
    ) -> AsyncIterator[ChatDelta]:
        deep = mode == "deep"
        params: dict[str, Any] = {
            "model": self.cfg.model,
            "messages": messages,
            "stream": True,
            "max_tokens": self.cfg.deep_max_tokens if deep else self.cfg.voice_max_tokens,
            "temperature": self.cfg.deep_temperature if deep else self.cfg.voice_temperature,
            "stream_options": {"include_usage": True},
            "extra_body": self.thinking_body(deep),
        }
        if tools:
            params["tools"] = tools

        self._touch()
        self.loading = True
        self.active += 1
        started = time.monotonic()
        first_token: float | None = None
        chunks = 0
        usage_tokens: int | None = None
        calls: dict[int, ToolCall] = {}
        think = _ThinkFilter()
        finish: str | None = None
        try:
            stream = await self._client.chat.completions.create(**params)
            async for chunk in stream:
                if chunk.usage is not None and chunk.usage.completion_tokens:
                    usage_tokens = chunk.usage.completion_tokens
                if not chunk.choices:
                    continue
                choice = chunk.choices[0]
                delta = choice.delta
                extra = delta.model_extra or {}
                reasoning = extra.get("reasoning_content") or extra.get("reasoning") or ""
                visible, hidden = think.feed(delta.content or "")
                reasoning += hidden
                for tc in delta.tool_calls or []:
                    slot = calls.setdefault(tc.index, ToolCall(id="", name="", arguments=""))
                    if tc.id:
                        slot.id = tc.id
                    if tc.function is not None:
                        if tc.function.name:
                            slot.name += tc.function.name
                        if tc.function.arguments:
                            slot.arguments += tc.function.arguments
                if visible or reasoning or delta.tool_calls:
                    if first_token is None:
                        first_token = time.monotonic()
                        self.loading = False
                    chunks += 1
                if visible or reasoning:
                    yield ChatDelta(content=visible, reasoning=reasoning)
                if choice.finish_reason:
                    finish = choice.finish_reason
            tail = think.flush()
            if tail:
                yield ChatDelta(content=tail)
            assembled = [
                ToolCall(id=c.id or f"call_{i}", name=c.name, arguments=c.arguments or "{}")
                for i, c in sorted(calls.items())
                if c.name
            ]
            yield ChatDelta(tool_calls=assembled or None, finish_reason=finish or "stop")
        finally:
            self.active -= 1
            self.loading = False
            self._touch()
            if first_token is not None:
                elapsed = time.monotonic() - first_token
                tokens = usage_tokens or chunks
                if elapsed > 0.2 and tokens > 1:
                    self.last_tok_s = round(tokens / elapsed, 1)
            log.debug("LLM %s call took %.2fs", mode, time.monotonic() - started)


# --- section 12: a fast voice model in front of the 35B -------------------------------------------------------

BRAINS = ("fast", "smart")
GPU_MODES = ("on_demand", "resident")  # section 20: where the fast model lives between conversations


def model_name(llm: Any) -> str:
    return str(getattr(getattr(llm, "cfg", None), "model", "") or "")


class LLMRouter:
    """Picks the model for each request; the Agent, ModelStatus and the tools talk to it like to one `LLM`.

    - voice turns go to the *voice model*: the fast model while `brain == "fast"`, else the smart one (the 35B);
    - deep mode, and a turn the Agent retries after the fast model got it wrong (`smart=True`), go to the 35B;
    - warm-up loads only the voice model; unload unloads both.
    If the fast model fails before its first token (not loaded in llama-swap, context overflow, ...), that request
    goes to the 35B instead. `fallbacks` counts both kinds of retry.
    """

    def __init__(
        self,
        cfg: LLMConfig,
        *,
        smart: Any | None = None,
        fast: Any | None = None,
        brain: str | None = None,
    ) -> None:
        self.cfg = cfg
        # `LLM` is looked up at call time, so tests can swap the class on the module.
        self.smart = smart if smart is not None else LLM(cfg)
        fast_name = str(getattr(cfg, "fast_model", "") or "")
        if fast is None and fast_name and fast_name != cfg.model:
            fast_cfg = dataclasses.replace(cfg, model=fast_name)
            if isinstance(getattr(cfg, "fast_temperature", None), (int, float)):
                fast_cfg = dataclasses.replace(fast_cfg, voice_temperature=float(cfg.fast_temperature))
            fast = LLM(fast_cfg)
        self.fast = fast
        # The fast models the pill menu can switch between (4B / 2B / a tuned 2B, ...).
        options = tuple(getattr(cfg, "fast_models", ()) or ())
        self.fast_options: tuple[str, ...] = options or ((fast_name,) if fast_name else ())
        default = getattr(cfg, "voice_brain", "fast")
        self.brain: str = brain if brain in BRAINS else (default if default in BRAINS else "fast")
        self.fallbacks = 0
        self._last: Any = self.voice
        # Also run on unload() (go to sleep / Unload now): the daemon adds "park the GPU Whisper" here.
        self.on_unload: list[Callable[[], Any]] = []
        # After an explicit unload even a resident model stays unloaded until the next wake/click/request.
        self.asleep = False
        # () -> (messages, tools): what a warm-up sends so the prompt cache is primed (the daemon sets the Agent's).
        self.primer: Callable[[], Any] | None = None
        # One warm-up at a time: two concurrent 5k-token prefills took ~10 s each (measured) instead of 2.6 s.
        self._warming: asyncio.Task[None] | None = None
        mode = str(getattr(cfg, "fast_gpu_mode", "on_demand"))
        self.gpu_mode: str = mode if mode in GPU_MODES else "on_demand"
        self._extra: dict[str, Any] = {}  # section 23: clients for other llama-swap ids (the computer step model)

    # --- which model -----------------------------------------------------------------

    @property
    def voice(self) -> Any:
        return self.fast if self.brain == "fast" and self.fast is not None else self.smart

    @property
    def fast_voice(self) -> bool:
        """True while voice turns run on the fast model (the Agent only checks/retries those)."""
        return self.voice is not self.smart

    @property
    def voice_role(self) -> str:
        return "fast" if self.fast_voice else "smart"

    def models(self) -> dict[str, Any]:
        out = {"smart": self.smart}
        if self.fast is not None:
            out = {"fast": self.fast, **out}
        return out

    def role_of(self, llm: Any) -> str:
        return "fast" if llm is self.fast and self.fast is not None else "smart"

    def idle_timeout(self, role: str) -> int:
        """Seconds a model stays loaded without use; 0 = resident (never unloaded for being idle). The fast model:
        resident in `gpu_mode` "resident", else `fast_idle_unload_s` (60 s) after the session went idle (section
        20). The 35B unloads 60 s after its last request (as the voice brain, its clock only runs while no session
        is open)."""
        if role == "fast":
            if self.gpu_mode == "resident":
                return 0
            timeout = int(getattr(self.cfg, "fast_idle_unload_s", 60))
            return timeout if timeout > 0 else 60
        return int(getattr(self.cfg, "deep_idle_unload_s", self.cfg.idle_unload_s))

    def resident(self, role: str) -> bool:
        return self.idle_timeout(role) <= 0

    def set_brain(self, brain: str) -> str:
        if brain not in BRAINS:
            raise ValueError(f"brain must be one of {', '.join(BRAINS)}")
        if brain != self.brain:
            log.info("voice brain: %s -> %s (%s)", self.brain, brain,
                     model_name(self.fast if brain == "fast" and self.fast is not None else self.smart))
        self.brain = brain
        self._last = self.voice
        return self.brain

    def set_gpu_mode(self, mode: str) -> str:
        """Section 20, the pill menu: "on_demand" (GPU only while talking) or "resident" (always on the GPU)."""
        if mode not in GPU_MODES:
            raise ValueError(f"gpu mode must be one of {', '.join(GPU_MODES)}")
        if mode != self.gpu_mode:
            log.info("fast model GPU mode: %s -> %s", self.gpu_mode, mode)
        self.gpu_mode = mode
        return mode

    @property
    def warming(self) -> bool:
        return self._warming is not None and not self._warming.done()

    def set_fast_model(self, name: str) -> str:
        """Swap the fast voice model (pill menu). The old one is unloaded by the caller; llama-swap's matrix
        keeps only one fast model resident anyway."""
        if name not in self.fast_options:
            raise ValueError(f"fast model must be one of {', '.join(self.fast_options)}")
        if self.fast is not None and model_name(self.fast) == name:
            return name
        fast_cfg = dataclasses.replace(self.cfg, model=name)
        if isinstance(getattr(self.cfg, "fast_temperature", None), (int, float)):
            fast_cfg = dataclasses.replace(fast_cfg, voice_temperature=float(self.cfg.fast_temperature))
        log.info("fast model: %s -> %s", model_name(self.fast) if self.fast is not None else None, name)
        self.fast = LLM(fast_cfg)
        self._last = self.voice
        return name

    def llm_for(self, name: str) -> Any:
        """Section 23: the client for a llama-swap model id (the computer loop's step model): the fast or the smart
        one when the name is theirs (so jarvisd's idle clock and holds see its use), else a client of its own."""
        name = str(name or "")
        for llm in (self.fast, self.smart):
            if llm is not None and name and model_name(llm) == name:
                return llm
        if not name:
            return self.smart
        if name not in self._extra:
            self._extra[name] = LLM(dataclasses.replace(self.cfg, model=name))
        return self._extra[name]

    def describe(self) -> dict[str, Any]:
        return {
            "brain": self.brain,
            "voice_model": model_name(self.voice),
            "fast_models": list(self.fast_options),
            "fast_model": model_name(self.fast) if self.fast is not None else None,
            "smart_model": model_name(self.smart),
            "fast_gpu_mode": self.gpu_mode,
            "fallbacks": self.fallbacks,
        }

    # --- the LLM interface --------------------------------------------------------------

    async def stream_chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        mode: Mode,
        *,
        smart: bool = False,
    ) -> AsyncIterator[ChatDelta]:
        llm = self.smart if (smart or mode == "deep") else self.voice
        self._last = llm
        self.asleep = False
        if llm is self.voice and self._warming is not None and not self._warming.done():
            # A warm-up is loading this model and prefilling the prompt: wait, then reuse its prompt cache.
            try:
                await asyncio.wait_for(asyncio.shield(self._warming), timeout=30)
            except Exception:  # noqa: BLE001 - the request itself will surface any real problem
                pass
        if llm is self.smart:
            async for delta in llm.stream_chat(messages, tools, mode):
                yield delta
            return
        started = False
        try:
            async for delta in llm.stream_chat(messages, tools, mode):
                started = True
                yield delta
        except Exception as exc:  # noqa: BLE001
            if started or not getattr(self.cfg, "fallback", True):
                raise
            self.fallbacks += 1
            log.warning("fast model %s failed before its first token (%s: %s); using %s",
                        model_name(llm), type(exc).__name__, exc, model_name(self.smart))
            self._last = self.smart
            async for delta in self.smart.stream_chat(messages, tools, mode):
                yield delta

    async def build_slot(self) -> None:
        """Section 20: make sure the on-demand fast model has its prompt slot saved (see `LLM.ensure_slot`). Runs as
        the warm-up task, so a wake meanwhile waits for it instead of loading in parallel."""
        fast = self.fast
        if (fast is None or self.primer is None or not callable(getattr(fast, "ensure_slot", None))
                or self.gpu_mode != "on_demand" or not getattr(self.cfg, "slot_restore", True)):
            return
        if self._warming is not None and not self._warming.done():
            return
        prime = self.primer()
        if fast.has_slot(prime):
            return
        log.info("no saved prompt slot for %s yet: building it now (the model then idles out)", model_name(fast))
        self._warming = asyncio.create_task(fast.ensure_slot(prime), name="llm-slot-build")
        await asyncio.shield(self._warming)

    async def warm_up(self, quiet: bool = False) -> None:
        self.asleep = False
        if self._warming is None or self._warming.done():
            self._warming = asyncio.create_task(self._warm_up(quiet), name="llm-warm-up-voice")
        await asyncio.shield(self._warming)

    async def _warm_up(self, quiet: bool) -> None:
        kw: dict[str, Any] = {"quiet": True} if quiet else {}
        params = inspect.signature(self.voice.warm_up).parameters
        if self.primer is not None and "prime" in params:
            try:
                kw["prime"] = self.primer()
            except Exception:  # noqa: BLE001
                log.exception("building the warm-up prompt failed")
        if "quiet" not in params:
            kw.pop("quiet", None)
        # Section 20: the on-demand fast model restores its saved prompt KV on a load (resident = section 12 as is).
        if (self.fast_voice and self.gpu_mode == "on_demand" and getattr(self.cfg, "slot_restore", True)
                and "prime" in kw and "slots" in params):
            kw["slots"] = True
        await self.voice.warm_up(**kw)

    async def unload(self) -> None:
        # "Go to sleep" / "Unload now" never unload a resident model: the user wants the fast model always
        # ready (2026-09-26). They unload the 35B (and the on-demand fast model), and the hooks park the GPU Whisper.
        roles = self.models()
        self.asleep = not any(self.resident(r) for r in roles)
        await asyncio.gather(*(m.unload() for r, m in roles.items() if not self.resident(r)))
        for hook in list(self.on_unload):
            try:
                result = hook()
                if asyncio.iscoroutine(result):
                    await result
            except Exception:  # noqa: BLE001
                log.exception("unload hook failed")

    async def is_loaded(self) -> bool:
        return bool(await self.voice.is_loaded())

    async def loaded_models(self) -> dict[str, bool]:
        roles = self.models()
        results = await asyncio.gather(*(m.is_loaded() for m in roles.values()), return_exceptions=True)
        return {role: r is True for role, r in zip(roles, results, strict=True)}

    def unload_in_s(self) -> int | None:
        return self.voice.unload_in_s()

    @property
    def loading(self) -> bool:
        return getattr(self.voice, "loading", False) is True

    @property
    def last_tok_s(self) -> float | None:
        return getattr(self._last, "last_tok_s", None)

    @property
    def last_use(self) -> float:
        return float(getattr(self.voice, "last_use", 0.0) or 0.0)

    def __getattr__(self, name: str) -> Any:
        # Anything else (backend, thinking_body, test counters, ...) is the voice model's.
        if name.startswith("_") or name in ("smart", "fast", "brain", "cfg", "fallbacks", "on_unload", "asleep",
                                            "primer", "gpu_mode"):
            raise AttributeError(name)
        return getattr(self.voice, name)
