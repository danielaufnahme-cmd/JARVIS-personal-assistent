"""Section 26's wiring into jarvisd: conversation memory, meeting notes, the content index and their IPC.

Kept out of daemon.py so the daemon only calls `Knowledge(daemon)`, `.register(bus)`, `.start()`, `.close()` and
`.meeting_state()` (the snapshot's `meeting` key).
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

log = logging.getLogger(__name__)


def jobs_busy() -> bool:
    """A coding job holds a 27B on the GPU, or JARVIS drives the mouse: no extra model work now."""
    from jarvis.integrations.coding_jobs import job_snapshot
    from jarvis.integrations.computer import CONTROL

    job = job_snapshot() or {}
    return bool(CONTROL.running) or job.get("state") == "running"


class Knowledge:
    def __init__(self, daemon: Any) -> None:
        from jarvis import memory
        from jarvis.integrations import meeting
        from jarvis.memory import conversations

        self.daemon = daemon
        self.cfg = daemon.cfg
        bus = daemon.bus
        memory.EMIT = lambda kind, text: bus.emit("memory.saved", kind=kind, text=text)
        self.recorder = conversations.ConversationRecorder(
            self.cfg, daemon.llm, busy=jobs_busy, session_active=lambda: bool(daemon.session.active))
        conversations.RECORDER = self.recorder
        daemon.agent.on_turn.append(self.recorder.on_turn)
        daemon.agent.on_reset.append(self.recorder.on_reset)
        self.meeting = meeting.MeetingNotes(
            bus, self.cfg, stt=daemon._question_stt, llm=daemon.llm, gate=daemon.gate, busy=jobs_busy,
            on_card=self._presented)
        meeting.MEETING = self.meeting
        self._slot_dirty = False
        self._slot_task: asyncio.Task[None] | None = None
        self._ended_at = 0.0
        memory.ON_CHANGE.append(self._memory_changed)
        self._index: Any = None
        self._tasks: list[asyncio.Task[Any]] = []

    def _presented(self, pending_id: str) -> None:
        # The spoken "Want reminders for …?" presented the card: a bare "yes" may confirm it (like a draft).
        self.daemon.agent._presented_draft = pending_id

    def meeting_state(self) -> dict[str, Any]:
        return self.meeting.snapshot()

    def register(self, bus: Any) -> None:
        from jarvis.tools import search

        search.register(bus, self.cfg)

        async def meeting_stop(_: dict[str, Any]) -> dict[str, Any]:
            result = await self.meeting.stop("button")
            return {"stopped": bool(result.get("ok"))}

        bus.handle("meeting.stop", meeting_stop)

    def start(self) -> None:
        bus = self.daemon.bus
        self._tasks.append(self.recorder.start(bus))
        try:
            self.meeting.recover()
        except Exception:  # noqa: BLE001
            log.exception("recovering a meeting transcript failed")
        s = getattr(self.cfg, "search", None)
        if s is not None and getattr(s, "enabled", False):
            from jarvis.integrations.content_index import get_content_index

            self._index = get_content_index(self.cfg)
            self._index.start_background(float(s.first_delay_s), float(s.refresh_min) * 60)

    async def close(self) -> None:
        if self._index is not None:
            self._index.stop()
        for t in (*self._tasks, self._slot_task):
            if t is not None and not t.done():
                t.cancel()
        await self.recorder.close()
        await self.meeting.close()
        from jarvis import memory
        from jarvis.integrations import meeting
        from jarvis.memory import conversations

        if self._memory_changed in memory.ON_CHANGE:
            memory.ON_CHANGE.remove(self._memory_changed)
        if conversations.RECORDER is self.recorder:
            conversations.RECORDER = None
        if meeting.MEETING is self.meeting:
            meeting.MEETING = None
        memory.EMIT = None

    # --- the fast model's prompt slot after a memory change --------------------------------------------------------

    def _memory_changed(self) -> None:
        """facts.md changed: the next conversation's system prompt differs. Rebuild the saved prompt slot once no
        session can still keep the old conversation (section 20), so the next wake restores instead of prefilling."""
        self._slot_dirty = True
        if self._slot_task is None or self._slot_task.done():
            try:
                self._slot_task = asyncio.get_running_loop().create_task(self._rebuild_slot(), name="memory-slot")
            except RuntimeError:
                pass  # no loop (a CLI)

    async def _rebuild_slot(self) -> None:
        session = self.daemon.session
        while self._slot_dirty:
            await asyncio.sleep(15.0)
            if session.active or jobs_busy():
                self._ended_at = time.monotonic()
                continue
            if session.keeps_context() or time.monotonic() - self._ended_at < 15.0:
                continue
            self._slot_dirty = False
            await self.daemon._prebuild_slot(delay_s=0.0)
