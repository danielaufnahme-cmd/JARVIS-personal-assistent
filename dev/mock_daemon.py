#!/usr/bin/env python3
"""Fake jarvisd for UI work: speaks the §6 IPC protocol on the real socket path.

    python3 dev/mock_daemon.py            # keys: 1-8 scenarios, m model, h hud, q quit
    tail -f ctl.txt | python3 dev/mock_daemon.py   # scriptable: append keys to ctl.txt
    python3 dev/mock_daemon.py --dump full|empty  # print the HUD widget fixture as JSON (for dev/hud_harness)

Keys:
  1 idle   2 waking   3 listening (fake mic level)   4 thinking   5 speaking (fake TTS level)
  6 email draft (awaiting_confirm)   s SMS draft   r revise the pending draft (new id, like the real gate)
  x make the next send fail   z silently re-id the pending draft (tests stale-id acks)   7 alert   8 deep   m model loaded/unloaded   h hud toggle   q quit
  HUD widgets (section 9): w full data   e empty data (nothing connected)   c add a conversation exchange
  Commands the HUD sends (email.open, news.read, reminder.cancel, ...) are acked and logged.

Stdlib only, so it runs with any python3. The real daemon replaces it with no UI change.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import random
import socket
import sys
import time

IDLE_UNLOAD_S = 600
CONFIRM_WINDOW_S = 8


def default_socket() -> str:
    if os.environ.get("JARVIS_SOCKET"):
        return os.environ["JARVIS_SOCKET"]
    return os.path.join(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"), "jarvis.sock")


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# --- HUD widget fixtures (the §6 shapes, as built in sections 7, 8 and 11) -------------------------------------

def _hours_ago(now: float, h: float) -> float:
    return now - h * 3600


def fixture_widgets(kind: str = "full", now: float | None = None) -> dict:
    """Widget fields for every HUD panel. `empty`: nothing connected, the states a fresh install shows."""
    now = time.time() if now is None else now
    if kind == "empty":
        return {
            "emails": [], "emails_status": "not_configured",
            "messages": [], "messages_status": "unavailable",
            "news": [], "news_status": "error",
            "weather": None, "weather_status": "error",
            "reminders": [], "timers": [],
            "calendar": [], "calendar_status": "disabled",
            "calendar_hint": "Connect a calendar: put Google Calendar's secret iCal address (Settings → your calendar → "
                             "'Secret address in iCal format') into [calendar] ics_url in ~/.config/jarvis/config.toml.",
            **FIRM_NOT_CONNECTED,
        }
    lt = time.localtime(now)
    hour0 = now - (lt.tm_min * 60 + lt.tm_sec)

    def email(uid, name, addr, subject, mins, unread, snippet):
        ts = now - mins * 60
        return {"id": uid, "from": name, "from_addr": addr, "subject": subject,
                "date": time.strftime("%a, %d %b %Y %H:%M:%S", time.localtime(ts)), "ts": ts,
                "unread": unread, "snippet": snippet}

    emails = [
        email("48213", "Anna Horáková", "anna.horakova@example.cz", "Saturday dinner: 19:30 at Lokál?", 6, True,
              "Hi! Booked a table for four at 19:30. Can you let me know by tonight if Petr is coming too?"),
        email("48209", "GitHub", "noreply@github.com", "[jarvis] CI failed on main: test_gate_confirm_window", 38, True,
              "1 failing check. test_gate_confirm_window: AssertionError: expected 'listening', got 'idle'"),
        email("48202", "Hetzner Online", "billing@hetzner.com", "Your invoice R0021184 for September", 190, True,
              "Your invoice is available. Amount due: EUR 6.49. It will be charged to your card on file."),
        email("48190", "Lukáš Dvořák", "lukas@example.com", "Re: bike trip next weekend", 60 * 20, False,
              "Sounds good. Saturday works for me; I'll bring the spare tube."),
        email("48177", "Revolut", "no-reply@revolut.com", "Your monthly statement is ready", 60 * 30, False,
              "Your September statement for your EUR account is ready to download."),
        email("48151", "Booking.com", "customer.service@booking.com", "Your stay in Ronda: check-in in 5 days", 60 * 52,
              False, "Hotel Montelirio · check-in Thu 1 Oct from 15:00 · 2 nights"),
    ]

    def news(title, source, cat, mins, summary, link):
        ts = now - mins * 60
        return {"title": title, "source": source, "category": cat,
                "published": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(ts)), "ts": ts,
                "age": f"{mins} min ago" if mins < 60 else f"{mins // 60} h ago", "link": link,
                "summary": summary, "language": "cs" if cat == "czech" else "en"}

    headlines = [
        news("EU ministers agree on a common charger rule for laptops from 2027", "BBC World", "world", 14,
             "The rule extends the USB-C requirement from phones to laptops sold in the EU.",
             "https://www.bbc.co.uk/news/world-example-1"),
        news("Praha chystá nové tramvajové spojení na Pankrác", "ČT24", "czech", 31,
             "Dopravní podnik představil harmonogram stavby nové trati.",
             "https://ct24.ceskatelevize.cz/example-2"),
        news("Open-source GPU driver reaches feature parity for Vulkan 1.4", "Ars Technica", "tech", 52,
             "The Mesa release adds the missing extensions and passes the conformance suite.",
             "https://arstechnica.com/example-3"),
        news("Central bank holds rates steady, signals cut in December", "Reuters", "business", 75,
             "Policymakers cited slowing inflation but warned of energy price risks.",
             "https://www.reuters.com/example-4"),
        news("Astronomers map the largest known filament of dark matter", "BBC Science", "science", 110,
             "Weak-lensing data from two surveys reveal a structure 50 million light years long.",
             "https://www.bbc.co.uk/news/science-example-5"),
        news("Show HN: A 4 KB wake-word detector that runs on a microcontroller", "Hacker News", "tech", 140,
             "", "https://news.ycombinator.com/item?id=example-6"),
        news("Vláda schválila rozpočet na příští rok se schodkem 230 miliard", "iROZHLAS", "czech", 190,
             "Návrh nyní míří do Poslanecké sněmovny.", "https://www.irozhlas.cz/example-7"),
        news("Port strike enters second week as talks resume", "Reuters", "business", 260,
             "Container backlogs grow at three major terminals.", "https://www.reuters.com/example-8"),
    ]

    icons = ["clear", "clear", "partly-cloudy", "cloudy", "showers", "showers"]
    texts = {"clear": "Clear sky", "partly-cloudy": "Partly cloudy", "cloudy": "Overcast", "showers": "Light showers",
             "rain": "Rain"}
    codes = {"clear": 0, "partly-cloudy": 2, "cloudy": 3, "showers": 80, "rain": 61}
    temps = [23.4, 24.6, 25.1, 24.2, 21.8, 20.9]
    probs = [0, 5, 20, 45, 70, 65]
    hourly = []
    for i in range(6):
        ts = int(hour0 + i * 3600)
        hourly.append({"ts": ts, "time": time.strftime("%H:%M", time.localtime(ts)), "temp": temps[i],
                       "code": codes[icons[i]], "text": texts[icons[i]], "icon": icons[i], "is_day": True,
                       "precip_prob": probs[i], "precip_mm": 0.6 if icons[i] == "showers" else 0.0})
    rain = next(h for h in hourly if h["precip_prob"] >= 50)
    day0 = now - (lt.tm_hour * 3600 + lt.tm_min * 60 + lt.tm_sec)
    daily = []
    for i, (lab, lo, hi, icon, prob) in enumerate([("Today", 17.2, 25.3, "showers", 70),
                                                    ("Tomorrow", 16.1, 22.8, "rain", 85),
                                                    (time.strftime("%a", time.localtime(day0 + 2 * 86400)), 15.4, 24.0,
                                                     "partly-cloudy", 10)]):
        ts = int(day0 + i * 86400)
        daily.append({"ts": ts, "date": time.strftime("%Y-%m-%d", time.localtime(ts)), "label": lab, "min": lo,
                      "max": hi, "code": codes[icon], "text": texts[icon], "icon": icon, "precip_prob": prob,
                      "precip_mm": 4.2 if prob > 50 else 0.0, "sunrise": "08:14", "sunset": "20:05"})
    weather = {
        "location": "Marbella, Spain", "source": "noctalia", "lat": 36.5149, "lon": -4.8838, "ts": int(now),
        "utc_offset_s": 7200, "units": {"temp": "°C", "wind": "km/h", "precip": "mm"},
        "now": {"temp": 23.0, "feels_like": 25.4, "code": 0, "text": "Clear sky", "icon": "clear", "is_day": True,
                "humidity": 75, "wind_kmh": 5.0, "precip_mm": 0.0},
        "hourly": hourly, "daily": daily,
        "rain_next_3h": True, "rain_at": rain["time"], "rain_ts": rain["ts"],
    }

    def at(minutes):
        return now + minutes * 60

    reminders = [
        {"id": "r7", "kind": "reminder", "text": "Stretch and refill water", "due_ts": at(18),
         "time": time.strftime("%H:%M", time.localtime(at(18))), "day": "Today"},
        {"id": "r5", "kind": "reminder", "text": "Call the dentist about Thursday", "due_ts": at(185),
         "time": time.strftime("%H:%M", time.localtime(at(185))), "day": "Today"},
        {"id": "r6", "kind": "reminder", "text": "Take the bins out", "due_ts": at(60 * 22),
         "time": time.strftime("%H:%M", time.localtime(at(60 * 22))), "day": "Tomorrow"},
    ]
    timers = [
        {"id": "t3", "kind": "timer", "text": "Pasta", "label": "pasta", "due_ts": now + 492, "started_ts": now - 108,
         "duration_s": 600},
    ]
    def thread(tid, contact, text, mins, unread):
        return {"id": tid, "contact": contact, "last_message": text,
                "time": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now - mins * 60)), "unread": unread}

    threads = [
        thread("th-anna", "Anna Horáková", "Great, see you both on Saturday then!", 3, 2),
        thread("th-mom", "Mom", "Did you get home ok? Call me tomorrow x", 47, 1),
        thread("th-lukas", "Lukáš Dvořák", "Photo: the trail map for Sunday", 60 * 5, 0),
        thread("th-petr", "Petr", "ok 👍", 60 * 26, 0),
        thread("th-dhl", "DHL Express", "Your parcel 7720 is out for delivery, 12:00–15:00.", 60 * 29, 0),
    ]
    return {
        "emails": emails, "emails_status": "ok",
        "messages": threads, "messages_status": "ok",
        "news": headlines, "news_status": "ok",
        "weather": weather, "weather_status": "ok",
        "reminders": reminders, "timers": timers,
        "calendar": [], "calendar_status": "disabled",
        "calendar_hint": "Connect a calendar: put Google Calendar's secret iCal address (Settings → your calendar → "
                         "'Secret address in iCal format') into [calendar] ics_url in ~/.config/jarvis/config.toml.",
        **fixture_firm(now),
    }


# Section 18: the firm tracker (Geonix Wrench), the `widgets` fields as jarvis/integrations/firm emits them.
FIRM_NOT_CONNECTED = {
    "firm": None, "firm_status": "not_configured",
    "firm_hint": "Connect Geonix Wrench: run  jarvisctl setup firm geonix  (a Cloudflare Access service token for "
                 "admin.geonix.site/api/summary).",
}


def fixture_firm(now: float, stale: bool = False) -> dict:
    fetched = now - (3 * 3600 if stale else 7 * 60)
    earnings = [19.9, 19.9, 29.8, 29.8, 29.8, 29.8, 39.7, 39.7, 39.7, 39.7, 39.7, 49.3, 49.3, 49.3]  # 14 days
    history = [{"day": time.strftime("%Y-%m-%d", time.localtime(now - (13 - i) * 86400)), "monthly_earnings": m,
                "subscribers": round(m / 12.33), "job_cards": 700 + 8 * i, "signups": 44 + i}
               for i, m in enumerate(earnings)]
    return {
        "firm": {
            "provider": "geonix", "name": "Geonix Wrench", "currency": "EUR",
            "monthly_earnings": 49.3, "total_earned": 123.4,
            "subscribers": {"individual": 3, "shops": 1, "shop_seats": 4, "total": 4},
            "job_cards": {"total": 812, "last_7_days": 40}, "signups": {"total": 57, "last_7_days": 5},
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(fetched - 60)),
            "fetched_ts": fetched, "as_of": time.strftime("%H:%M", time.localtime(fetched)), "as_of_day": "Today",
            "age_s": int(now - fetched), "stale": stale, "error": "no answer within 10 s" if stale else None,
            "history": history,
        },
        "firm_status": "ok", "firm_hint": "",
    }


def fixture_system(t: float, model_loaded: bool) -> dict:
    """A plausible `system` sample for this machine (7900X, 30 GB, RTX 3060 12 GB)."""
    cpu = 6.0 + 4.0 * abs(math.sin(t / 7.0)) + random.uniform(-1.0, 1.5)
    model_mb = 11960 if model_loaded else None
    ram_used = 9800 + (model_mb or 0) + int(300 * math.sin(t / 30.0))
    return {
        "ts": int(t), "cpu_pct": round(cpu, 1), "ram_used_mb": ram_used, "ram_total_mb": 31_232,
        "ram_pct": round(ram_used / 31_232 * 100, 1), "model_rss_mb": model_mb,
        "model_proc": "llama-server" if model_loaded else None, "gpu_name": "NVIDIA GeForce RTX 3060",
        "vram_used_mb": 9310 if model_loaded else 2140, "vram_total_mb": 12_288,
        "gpu_util_pct": int(4 + random.uniform(0, 5)), "gpu_temp_c": 53 if model_loaded else 46,
        "disk_free_gb": 412.6, "disk_total_gb": 953.3, "disk_path": "/home",
    }


DEMO_EXCHANGES = [
    ("What's the weather like this afternoon?",
     "Clear for now, sir, around twenty-three degrees. Showers are likely from about three o'clock."),
    ("Remind me in eighteen minutes to stretch.", "Done. I'll remind you at a quarter past."),
    ("Anything important in my inbox?",
     "Three unread. Anna asks whether Petr is coming to dinner on Saturday, and the CI on main failed."),
    ("Email Anna that Petr is coming.", "Drafted. Shall I send it?"),
]

DEMO_DEEP = """## Local LLM on a 12 GB GPU: MoE vs dense

**Short answer:** for this machine the 35B mixture-of-experts model is the better fit, as long as the experts stay in system RAM.

### Why
- Only ~3B parameters are *active* per token, so CPU-side expert compute is cheap on a 7900X.
- Attention and the shared layers fit on the RTX 3060, which keeps prompt processing fast.
- A dense 14B at Q4 would fit entirely in VRAM, but answers noticeably worse on reasoning tasks.

### Trade-offs
| | MoE 35B-A3B | Dense 14B |
|---|---|---|
| VRAM | ~9.5 GB | ~10 GB |
| RAM | ~12 GB | ~1 GB |
| Speed | ~27 tok/s | ~38 tok/s |

### Recommendation
Keep the MoE model, lower `--n-cpu-moe` until VRAM sits near 9.5 GB, and let llama-swap unload it after ten idle minutes.
"""


class Mock:
    def __init__(self) -> None:
        self.clients: set[asyncio.StreamWriter] = set()
        self.mode = "idle"
        self.session = False
        self.hud = False
        self.draft: dict | None = None
        self.loaded = False
        self.loading = False
        self.last_use = 0.0
        self.tok_s = 0.0
        self.draft_seq = 0
        self.fail_next_send = False
        self._confirm_window: asyncio.TimerHandle | None = None
        self._level_task: asyncio.Task | None = None
        self._pending: set[asyncio.Task] = set()
        self.widgets: dict = fixture_widgets("full")
        self.widget_kind = "full"
        self.demo_i = 0

    # --- output -------------------------------------------------------------

    def model_ev(self) -> dict:
        left = max(0, int(IDLE_UNLOAD_S - (time.monotonic() - self.last_use))) if self.loaded else None
        return {"ev": "model", "loaded": self.loaded, "loading": self.loading,
                "unload_in_s": left, "tok_s": self.tok_s if self.loaded else None}

    def snapshot(self) -> dict:
        m = self.model_ev()
        m.pop("ev")
        return {"ev": "snapshot", "state": {"mode": self.mode, "session": self.session},
                "draft": self.draft, "model": m, "hud": {"open": self.hud},
                "widgets": {**self.widgets, **({"system": fixture_system(time.time(), self.loaded)}
                                               if self.widget_kind == "full" else {})}}

    def emit(self, ev: dict) -> None:
        line = (json.dumps(ev) + "\n").encode()
        for w in list(self.clients):
            try:
                w.write(line)
            except Exception:
                self.clients.discard(w)
        if ev.get("ev") != "level":
            log(f"-> {json.dumps(ev)[:160]}")

    def set_mode(self, mode: str, session: bool | None = None) -> None:
        if session is not None:
            self.session = session
        self.mode = mode
        self.emit({"ev": "state", "mode": mode, "session": self.session})
        self._restart_level()

    def later(self, delay: float, fn) -> None:
        async def run():
            await asyncio.sleep(delay)
            fn()
        t = asyncio.create_task(run())
        self._pending.add(t)
        t.add_done_callback(self._pending.discard)

    # --- fake levels (30 Hz) -------------------------------------------------

    def _restart_level(self) -> None:
        if self._level_task:
            self._level_task.cancel()
            self._level_task = None
        if self.mode in ("listening", "speaking"):
            self._level_task = asyncio.create_task(self._levels(self.mode))
        else:
            self.emit({"ev": "level", "v": 0.0})

    async def _levels(self, mode: str) -> None:
        t0 = time.monotonic()
        while True:
            t = time.monotonic() - t0
            if mode == "listening":
                # Syllable-ish bursts: a slow sine envelope with jitter on top.
                env = max(0.0, math.sin(t * 2.3)) * 0.6 + max(0.0, math.sin(t * 7.1)) * 0.25
                v = env + random.uniform(-0.08, 0.12)
            else:
                # TTS: steadier, faster word rhythm.
                v = 0.35 + 0.3 * abs(math.sin(t * 5.5)) + 0.15 * math.sin(t * 13.0) + random.uniform(-0.05, 0.05)
            self.emit({"ev": "level", "v": round(min(1.0, max(0.0, v)), 3)})
            await asyncio.sleep(1 / 30)

    # --- model ----------------------------------------------------------------

    def load_model(self, then=None) -> None:
        if self.loaded:
            self.last_use = time.monotonic()
            self.emit(self.model_ev())
            if then:
                then()
            return
        self.loading = True
        self.emit(self.model_ev())

        def done():
            self.loading, self.loaded, self.tok_s = False, True, 27.4
            self.last_use = time.monotonic()
            self.emit(self.model_ev())
            if then:
                then()
        self.later(2.0, done)

    def unload_model(self) -> None:
        self.loaded = self.loading = False
        self.emit(self.model_ev())

    async def model_ticker(self) -> None:
        while True:
            await asyncio.sleep(1)
            if self.hud and self.widget_kind == "full":
                self.emit({"ev": "widgets", "system": fixture_system(time.time(), self.loaded)})
            if self.loaded:
                ev = self.model_ev()
                if ev["unload_in_s"] <= 0:
                    self.unload_model()
                else:
                    self.emit(ev)

    # --- scenarios ------------------------------------------------------------

    def scenario(self, key: str) -> None:
        if key == "1":
            self.set_mode("idle", session=False)
        elif key == "2":
            self.set_mode("waking", session=True)
            self.load_model()
        elif key == "3":
            self.set_mode("listening", session=True)
        elif key == "4":
            self.set_mode("thinking", session=True)
        elif key == "5":
            self.set_mode("speaking", session=True)
            self.emit({"ev": "reply", "delta": "Certainly, sir. "})
        elif key == "6":
            self.new_draft("email")
        elif key == "s":
            self.new_draft("sms")
        elif key == "r":
            self.revise_draft()
        elif key == "z" and self.draft:
            # Test aid: change the pending id without telling clients, so the UI's next command is stale.
            self.draft["id"] = self._next_id()
            log(f"pending draft silently re-id'd to {self.draft['id']}")
        elif key == "x":
            self.fail_next_send = not self.fail_next_send
            log(f"next send will {'FAIL' if self.fail_next_send else 'succeed'}")
        elif key == "7":
            self.emit({"ev": "alert", "kind": "reminder", "text": "Call the dentist about Thursday"})
        elif key == "8":
            self.set_mode("deep", session=True)
            self.emit({"ev": "transcript", "text": "Think hard about which local model suits this GPU best.",
                       "final": True})
            self.stream_deep()
        elif key == "w":
            self.set_widgets("full")
        elif key == "e":
            self.set_widgets("empty")
        elif key == "c":
            q, a = DEMO_EXCHANGES[self.demo_i % len(DEMO_EXCHANGES)]
            self.demo_i += 1
            self.emit({"ev": "transcript", "text": q, "final": True})
            for part in a.replace(". ", ".|").split("|"):
                self.emit({"ev": "reply", "delta": part.strip() + " "})
        elif key == "m":
            if self.loaded or self.loading:
                self.unload_model()
            else:
                self.loaded, self.tok_s = True, 27.4
                # Start part-way through the countdown so the ring is visibly partial.
                self.last_use = time.monotonic() - 150
                self.emit(self.model_ev())
        elif key == "h":
            self.set_hud(not self.hud)
        elif key == "q":
            raise SystemExit(0)

    def set_widgets(self, kind: str) -> None:
        self.widget_kind = kind
        self.widgets = fixture_widgets(kind)
        extra = {"system": fixture_system(time.time(), self.loaded)} if kind == "full" else {"system": None}
        self.emit({"ev": "widgets", **self.widgets, **extra})

    def stream_deep(self) -> None:
        chunks = [DEMO_DEEP[i:i + 90] for i in range(0, len(DEMO_DEEP), 90)]
        for i, chunk in enumerate(chunks):
            last = i == len(chunks) - 1
            self.later(0.05 * i, lambda c=chunk, d=last: self.emit({"ev": "deep", "delta": c, "done": d}))

    def _next_id(self) -> str:
        self.draft_seq += 1
        return f"d{self.draft_seq}"

    def open_confirm_window(self) -> None:
        # Like the real gate: awaiting_confirm is only the 8 s voice window; the draft stays pending after it.
        self.set_mode("awaiting_confirm", session=True)
        if self._confirm_window:
            self._confirm_window.cancel()

        def expire():
            if self.mode == "awaiting_confirm":
                self.set_mode("listening")
        self._confirm_window = asyncio.get_running_loop().call_later(CONFIRM_WINDOW_S, expire)

    def new_draft(self, kind: str) -> None:
        if self.draft:
            self.emit({"ev": "draft_cleared", "id": self.draft["id"], "result": "replaced"})
        if kind == "sms":
            self.draft = {"id": self._next_id(), "kind": "sms", "to": "Dad <+420 777 123 456>",
                          "subject": None, "body": "On my way, see you at 7."}
        else:
            self.draft = {
                "id": self._next_id(), "kind": "email", "to": "Mom <mom@example.com>",
                "subject": "Running late tonight",
                "body": "Hi Mom,\n\nI'm running about twenty minutes late tonight, the meeting ran over. "
                        "Start without me and I'll be there as soon as I can.\n\nLove,\nDaniel",
            }
        self.emit({"ev": "draft", **self.draft})
        self.open_confirm_window()
        self.emit({"ev": "reply", "delta": "Drafted. Shall I send it?"})

    def revise_draft(self, **changes) -> None:
        """A revision is a new PendingAction: new id, a plain `draft` event, no draft_cleared."""
        if not self.draft:
            log("no pending draft to revise")
            return
        if not changes:
            changes = {"body": self.draft["body"].replace("twenty", "thirty")}
        self.draft = {**self.draft, **changes, "id": self._next_id()}
        self.emit({"ev": "draft", **self.draft})
        self.open_confirm_window()

    def set_hud(self, open_: bool) -> None:
        self.hud = open_
        self.emit({"ev": "hud", "open": open_})

    # --- commands -----------------------------------------------------------

    def handle(self, cmd: dict) -> tuple[bool, str | None, object]:
        """-> (ok, error, result) for the ack."""
        name = cmd.get("cmd")
        if name == "ping":
            return True, None, "pong"
        if name in ("session.toggle", "session.start", "session.stop"):
            want = {"session.toggle": not self.session, "session.start": True, "session.stop": False}[name]
            if want and not self.session:
                self.set_mode("waking", session=True)
                self.load_model(then=lambda: self.session and self.set_mode("listening"))
            elif not want and self.session:
                self.set_mode("idle", session=False)
            return True, None, None
        if name in ("hud.toggle", "hud.open", "hud.close"):
            self.set_hud({"hud.toggle": not self.hud, "hud.open": True, "hud.close": False}[name])
            return True, None, None
        if name == "model.unload":
            self.unload_model()
            return True, None, None
        if name in ("draft.confirm", "draft.cancel", "draft.edit"):
            if not self.draft or cmd.get("id") != self.draft["id"]:
                return False, "no pending draft with that id", None
            if name == "draft.edit":
                self.revise_draft(**{k: cmd[k] for k in ("body", "subject", "to") if isinstance(cmd.get(k), str)})
                return True, None, {"id": self.draft["id"]}
            if self._confirm_window:
                self._confirm_window.cancel()
            old = self.draft["id"]
            self.draft = None
            if name == "draft.cancel":
                self.emit({"ev": "draft_cleared", "id": old, "result": "cancelled"})
                result = {"cancelled": True}
            elif self.fail_next_send:
                self.fail_next_send = False
                self.emit({"ev": "error", "source": "gate", "message": "SMTP: connection refused (mock)"})
                self.emit({"ev": "draft_cleared", "id": old, "result": "failed"})
                result = {"sent": False}
            else:
                self.emit({"ev": "draft_cleared", "id": old, "result": "sent"})
                result = {"sent": True}
            self.set_mode("listening" if self.session else "idle")
            return True, None, result
        if name in ("email.open", "email.reply", "email.mark_read", "sms.open", "sms.reply", "news.read"):
            key = "thread" if name.startswith("sms.") else "link" if name == "news.read" else "id"
            if not isinstance(cmd.get(key), str) or not cmd.get(key):
                return False, f'{name} needs a "{key}"', None
            if name == "email.mark_read":
                for e in self.widgets.get("emails", []):
                    if e["id"] == cmd["id"]:
                        e["unread"] = False
                self.widgets["emails"].sort(key=lambda e: not e["unread"])
                self.emit({"ev": "widgets", "emails": self.widgets["emails"], "emails_status": "ok"})
                return True, None, {"id": cmd["id"], "unread": False}
            return True, None, None
        if name == "reminder.cancel":
            key = cmd.get("id")
            for field in ("reminders", "timers"):
                before = self.widgets.get(field, [])
                after = [r for r in before if r["id"] != key]
                if len(after) != len(before):
                    self.widgets[field] = after
                    self.emit({"ev": "widgets", field: after})
                    return True, None, {"id": key, "cancelled": True}
            return True, None, {"id": key, "cancelled": False}
        if name in ("news.refresh", "email.refresh", "weather.refresh", "calendar.refresh", "system.poll"):
            return True, None, None
        if name in ("firm.refresh", "firm.reload"):
            return True, None, {"firm_status": self.widgets.get("firm_status"), "stale": False, "hint": ""}
        if name in ("briefing.get", "briefing.set", "briefing.preview"):
            if name == "briefing.set":
                if not isinstance(cmd.get("enabled"), bool):
                    return False, 'briefing.set needs "enabled": true|false', None
                self.briefing = cmd["enabled"]
            result = {"enabled": getattr(self, "briefing", True), "done_today": False}
            if name == "briefing.preview":
                result["text"] = ("Good morning, sir: €49.30 a month from 3 subscribers and 1 shop, €123.40 earned "
                                  "in total, 40 PDFs this week. Nothing scheduled today; 23 degrees and clear sky.")
            return True, None, result
        if name == "say":
            self.emit({"ev": "transcript", "text": str(cmd.get("text", "")), "final": True})
            return True, None, None
        return False, f"unknown command: {name}", None

    async def client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.clients.add(writer)
        log(f"client connected ({len(self.clients)})")
        writer.write((json.dumps(self.snapshot()) + "\n").encode())
        try:
            while line := await reader.readline():
                try:
                    cmd = json.loads(line)
                    if not isinstance(cmd, dict):
                        raise ValueError("not an object")
                except ValueError as e:
                    writer.write((json.dumps({"ev": "ack", "cmd": None, "ok": False, "error": f"bad json: {e}"}) + "\n").encode())
                    continue
                log(f"<- {json.dumps(cmd)}")
                ok, err, result = self.handle(cmd)
                ack = {"ev": "ack", "cmd": cmd.get("cmd"), "ok": ok}
                if result is not None:
                    ack["result"] = result
                if err:
                    ack["error"] = err
                if "req" in cmd:
                    ack["req"] = cmd["req"]
                writer.write((json.dumps(ack) + "\n").encode())
                await writer.drain()
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            self.clients.discard(writer)
            writer.close()
            log(f"client gone ({len(self.clients)})")


def socket_in_use(path: str) -> bool:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.connect(path)
        return True
    except OSError:
        return False
    finally:
        s.close()


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--socket", default=default_socket())
    ap.add_argument("--dump", choices=["full", "empty"], help="print the HUD widget fixture as JSON and exit")
    args = ap.parse_args()
    if args.dump:
        w = fixture_widgets(args.dump)
        if args.dump == "full":
            w["system"] = fixture_system(time.time(), True)
        print(json.dumps({"widgets": w, "exchanges": DEMO_EXCHANGES, "deep": DEMO_DEEP}, ensure_ascii=False))
        return

    if os.path.exists(args.socket):
        if socket_in_use(args.socket):
            sys.exit(f"{args.socket} is served by another process (the real jarvisd?); not replacing it")
        os.unlink(args.socket)

    mock = Mock()
    server = await asyncio.start_unix_server(mock.client, path=args.socket)
    os.chmod(args.socket, 0o600)
    log(f"mock jarvisd on {args.socket}  (keys: 1-8 scenarios, s sms, r revise, x fail next send, m model, h hud, w/e widgets full/empty, c chat, q quit)")

    loop = asyncio.get_running_loop()
    stop = loop.create_future()
    tty = sys.stdin.isatty()
    old_attrs = None
    if tty:
        import termios
        import tty as tty_mod
        old_attrs = termios.tcgetattr(sys.stdin)
        tty_mod.setcbreak(sys.stdin.fileno())

    def on_input() -> None:
        data = os.read(sys.stdin.fileno(), 256).decode(errors="ignore")
        if not data and not tty:
            loop.remove_reader(sys.stdin.fileno())  # EOF on a pipe: keep serving
            return
        for ch in data.strip() if not tty else data:
            try:
                mock.scenario(ch)
            except SystemExit:
                if not stop.done():
                    stop.set_result(None)
                return

    loop.add_reader(sys.stdin.fileno(), on_input)
    ticker = asyncio.create_task(mock.model_ticker())
    try:
        await stop
    finally:
        ticker.cancel()
        server.close()
        for w in list(mock.clients):
            w.close()
        if os.path.exists(args.socket):
            os.unlink(args.socket)
        if old_attrs is not None:
            import termios
            termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_attrs)
        log("bye")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
