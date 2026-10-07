"""Configuration: config.example.toml defaults, overridden by ~/.config/jarvis/config.toml."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULTS_FILE = REPO_ROOT / "config.example.toml"
USER_FILE = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "jarvis" / "config.toml"


@dataclass(frozen=True)
class PersonaConfig:
    address: str = "sir"
    timezone: str = "Europe/Prague"


@dataclass(frozen=True)
class LLMConfig:
    base_url: str = "http://127.0.0.1:8401/v1"
    model: str = "jarvis"
    idle_unload_s: int = 600
    history_turns: int = 12
    voice_max_tokens: int = 300
    voice_temperature: float = 0.7
    deep_max_tokens: int = 4000
    deep_temperature: float = 0.6
    backend: str = "auto"            # "llama-swap" | "ollama" | "auto" (port 11434 = ollama)
    thinking_knob: str = "auto"      # "chat_template_kwargs" | "reasoning_effort" | "auto" (by backend)
    request_timeout_s: int = 300     # a cold model load can take a while
    # Section 12: a small model answers voice turns; `model` (the 35B) does deep_think and the fallback.
    fast_model: str = "qwen35-4b"    # a llama-swap model id; "" = no fast model (everything on `model`)
    fast_temperature: float = 0.2    # the fast model's voice temperature (small models call tools better cool)
    fast_models: tuple[str, ...] = ("qwen35-4b", "qwen35-2b")  # what the pill menu can switch between
    voice_brain: str = "fast"        # "fast" | "smart" (smart = every voice turn on `model`)
    fallback: bool = True            # retry a fast-model turn once on `model` after an invalid tool call etc.
    # Section 20: where the fast model lives. "on_demand": 0 VRAM while idle (weights hot in the RAM page cache),
    # loaded at the first sign of a conversation (wake trigger, click, HUD) and unloaded fast_idle_unload_s after
    # the session went idle. "resident": section 12's always-loaded model (jarvisd reloads it if it goes missing).
    # The pill menu overrides this at runtime (llm.fast.gpu_mode, saved in state.json).
    fast_gpu_mode: str = "on_demand"
    fast_idle_unload_s: int = 60     # on_demand only; <= 0 means 60
    deep_idle_unload_s: int = 60     # the 35B: after its last request (as the voice brain: after the session)
    # on_demand: a load restores the saved KV of the fixed prompt prefix (system prompt + tools) instead of
    # prefilling ~8k tokens (llama-server --slot-save-path must be this directory; see docs/tuning.md).
    slot_restore: bool = True
    slot_dir: str = "$XDG_RUNTIME_DIR/jarvis-slots"
    llama_swap_config: str = "~/.config/llama-swap/config.yaml"  # to find the fast model's GGUF ("auto" below)
    # GGUFs kept in the RAM page cache (posix_fadvise WILLNEED every page_cache_refresh_s), so a load is a RAM->VRAM
    # copy rather than a disk read. "auto" = the active fast model's file. Empty = off.
    keep_in_page_cache: tuple[str, ...] = ("auto",)
    page_cache_refresh_s: int = 60


@dataclass(frozen=True)
class SessionConfig:
    silence_timeout_s: int = 120   # section 13: only before the first question of a click-started session
    confirm_window_s: int = 8
    # Section 13: after JARVIS finishes speaking, a follow-up without the wake word must *start* within this.
    followup_s: float = 8.0
    # A new session keeps the conversation (for "and in Tokyo?") if the last one ended less than this ago.
    context_keep_s: int = 300


@dataclass(frozen=True)
class IPCConfig:
    socket: str = ""

    @property
    def socket_path(self) -> Path:
        if self.socket:
            return Path(self.socket).expanduser()
        runtime = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
        return Path(runtime) / "jarvis.sock"


@dataclass(frozen=True)
class DaemonConfig:
    log_level: str = "INFO"


@dataclass(frozen=True)
class WakeConfig:
    enabled: bool = True
    model: str = "jarvis:0.06, hey_jarvis:0.5"  # name[:threshold], comma-separated; see config.example.toml
    threshold: float = 0.5
    refractory_s: float = 2.0
    log_min_score: float = 0.03    # scores above this are logged at DEBUG, for tuning
    # Second stage: Whisper checks that "Jarvis" was really said (other apps' audio isn't echo-cancelled).
    verify: bool = True
    verify_min_ratio: float = 82.0  # fuzzy word match vs "jarvis" (jervis 83, jars 80, travis 67, service 46)
    verify_skip_score: float = 0.95  # skip the check at/above this score, but only when no other app is playing
    verify_preroll_s: float = 2.5
    verify_post_ms: int = 300
    verify_prompt: str = "Jarvis."
    barge_threshold_scale: float = 0.75  # stage-1 thresholds x this while JARVIS speaks (docs/tuning.md)
    # Section 12: the check runs on a small Whisper on the CPU, so waiting for "Jarvis" costs no VRAM at all.
    verify_model: str = "base"          # "" = use the [stt] model (the old behaviour)
    verify_device: str = "cpu"
    verify_compute_type: str = "int8"
    verify_cpu_threads: int = 4
    verify_min_score: float = -1.0      # also require this decoder confidence (avg log-prob); -99 = off


@dataclass(frozen=True)
class AudioConfig:
    voice_enabled: bool = False    # config.example.toml turns it on; a bare Config() (tests) stays silent
    echo_cancel: bool = True       # load module-echo-cancel at runtime (unloaded on exit)
    mic: str = "alsa_input.usb-MUSIC-BOOST_Trust_GXT_242_Microphone-00.mono-fallback"
    speaker: str = ""              # empty = the current default sink
    ec_method: str = "webrtc"
    # WebRTC's noise suppression and AGC chop up speech enough to confuse the VAD; keep only the echo canceller.
    ec_args: str = "webrtc.noise_suppression=false webrtc.gain_control=false webrtc.high_pass_filter=true"
    level_hz: float = 30.0
    # JARVIS's volume counts every sink its voice goes through: the hardware sink (the system volume) and a virtual
    # one in between (EasyEffects grabs the canceller's playback stream and moves it to easyeffects_sink).
    volume_include_virtual_sinks: bool = True
    # Ducking: other apps' streams go down while JARVIS is active (wake word, click, follow-up speech).
    filler_after_s: float = 2.5        # no spoken answer this long after the question: "One moment, sir." (0 = off)
    duck_enabled: bool = True
    duck_level: float = 0.2            # multiplies each stream's linear volume (0.2 ≈ -14 dB)
    duck_fade_in_ms: int = 250
    restore_fade_ms: int = 600
    duck_restore_grace_s: float = 1.0  # after JARVIS stops speaking
    duck_idle_restore_s: float = 8.0   # after a wake/click with nothing said
    vad_threshold: float = 0.5
    vad_silence_ms: int = 700      # end of turn after this much silence
    vad_max_turn_s: float = 30.0


@dataclass(frozen=True)
class STTConfig:
    model: str = "large-v3-turbo"
    device: str = "cuda"
    compute_type: str = "int8_float16"
    beam_size: int = 1
    languages: tuple[str, ...] = ("en", "de", "cs", "es")   # auto-detect among these; ties go to the earlier one
    min_free_vram_mb: int = 1800   # below this, Whisper falls back to the CPU
    cpu_threads: int = 8           # CTranslate2 threads when on the CPU
    # Section 12 (VRAM on demand): a GPU Whisper waits in RAM and moves to the GPU on a wake trigger or a click,
    # then back to RAM after `idle_unload_s` without a session.
    on_demand: bool = True
    idle_unload_s: int = 60
    park_in_ram: bool = False      # off the GPU = unloaded (reloads from disk in ~1.3 s); true = keep ~0.8 GB in RAM


@dataclass(frozen=True)
class TTSConfig:
    voice: str = "bm_george"       # bm_george | bm_lewis | bm_fable (samples in docs/voice-samples/)
    speed: float = 1.0
    lang: str = "en-gb"
    model_dir: str = "~/models/kokoro"
    # One voice per language the user may speak: "kokoro:<voice>" or "piper:<name>" (~/models/piper/<name>.onnx).
    voices: dict[str, str] = field(default_factory=lambda: {
        "en": "kokoro:bm_george", "es": "kokoro:em_alex",
        "de": "piper:de_DE-thorsten-high", "cs": "piper:cs_CZ-jirka-medium"})
    piper_dir: str = "~/models/piper"


@dataclass(frozen=True)
class EmailConfig:
    enabled: bool = True
    imap_host: str = "imap.gmail.com"
    imap_port: int = 993
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    primary_only: bool = True          # Gmail: only the Primary category (X-GM-RAW "category:primary")
    cache_size: int = 50               # newest INBOX headers kept in cache.db
    idle_renew_s: int = 240            # re-issue IMAP IDLE (and re-sync) this often; short, so a dead link costs minutes, not 25


@dataclass(frozen=True)
class ContactsConfig:
    default_region: str = "CZ"         # for turning local phone numbers into E.164 on import


# Section 11. Every default URL was checked live on 2026-09-25. `language` is the feed's language (en / cs).
DEFAULT_NEWS_FEEDS: tuple[dict[str, str], ...] = (
    {"name": "BBC News", "url": "https://feeds.bbci.co.uk/news/world/rss.xml", "category": "world", "language": "en"},
    {"name": "The Guardian", "url": "https://www.theguardian.com/world/rss", "category": "world", "language": "en"},
    {"name": "ČT24", "url": "https://ct24.ceskatelevize.cz/rss", "category": "czech", "language": "cs"},
    {"name": "iROZHLAS", "url": "https://www.irozhlas.cz/rss/irozhlas", "category": "czech", "language": "cs"},
    {"name": "Seznam Zprávy", "url": "https://www.seznamzpravy.cz/rss", "category": "czech", "language": "cs"},
    {"name": "Hacker News", "url": "https://news.ycombinator.com/rss", "category": "tech", "language": "en"},
    {"name": "The Verge", "url": "https://www.theverge.com/rss/index.xml", "category": "tech", "language": "en"},
    {"name": "Ars Technica", "url": "https://feeds.arstechnica.com/arstechnica/index", "category": "tech", "language": "en"},
    {"name": "BBC Business", "url": "https://feeds.bbci.co.uk/news/business/rss.xml", "category": "business", "language": "en"},
    {"name": "BBC Science", "url": "https://feeds.bbci.co.uk/news/science_and_environment/rss.xml", "category": "science", "language": "en"},
)


@dataclass(frozen=True)
class NewsConfig:
    enabled: bool = False          # config.example.toml turns it on; a bare Config() (tests) never touches the network
    ttl_s: int = 900               # a feed is fetched again after this long (memory + cache.db)
    refresh_s: int = 900           # the HUD headlines refresh this often
    timeout_s: float = 8.0
    max_feed_bytes: int = 5_000_000
    widget_items: int = 8
    user_agent: str = "Mozilla/5.0 (X11; Linux x86_64) JARVIS/0.1 (personal voice assistant; RSS reader)"
    feeds: tuple[dict[str, str], ...] = DEFAULT_NEWS_FEEDS


@dataclass(frozen=True)
class WebConfig:
    enabled: bool = False          # like [news]: on in config.example.toml, off for a bare Config()
    backend: str = "ddgs"          # "ddgs" (DuckDuckGo & co., no key) | "searxng"
    searxng_url: str = ""          # e.g. "http://127.0.0.1:8888" once the user runs one
    ddgs_backend: str = "auto"     # the ddgs package's own engine choice (text search)
    ddgs_news_backend: str = "yahoo"  # recent=true; ddgs's Bing news dates were wrong on 2026-09-25. Falls back to auto
    region: str = "wt-wt"          # no region; e.g. "cz-cs" or "uk-en"
    safesearch: str = "moderate"
    min_interval_s: float = 2.0    # at least this long between two searches
    timeout_s: float = 10.0
    page_max_bytes: int = 3_000_000
    page_max_chars: int = 6000
    user_agent: str = "Mozilla/5.0 (X11; Linux x86_64; rv:140.0) Gecko/20100101 Firefox/140.0"


# Section 8: weather, reminders/timers, calendar, system stats (the HUD's ④ ⑤ ⑥).
@dataclass(frozen=True)
class WeatherConfig:
    enabled: bool = False          # like [news]: on in config.example.toml; a bare Config() never touches the network
    location_file: str = "~/.cache/noctalia/location.json"  # Noctalia's (auto-)location; "" = use city/lat/lon
    city: str = "Prague"           # the fallback when the location file is missing or broken
    lat: float = 50.0755
    lon: float = 14.4378
    ttl_s: int = 900               # weather is fetched again after 15 min
    timeout_s: float = 8.0
    url: str = "https://api.open-meteo.com/v1/forecast"


@dataclass(frozen=True)
class RemindersConfig:
    # Reminders that fell due while jarvisd was down fire after start-up, once the voice is ready (or after this).
    startup_grace_s: float = 20.0
    keep_done_days: int = 7        # fired / cancelled rows are pruned after this


@dataclass(frozen=True)
class CalendarConfig:
    ics_url: str = ""              # read-only ICS ("secret address in iCal format"); "" = no calendar
    refresh_s: int = 600
    timeout_s: float = 10.0
    max_bytes: int = 5_000_000


@dataclass(frozen=True)
class SystemConfig:
    poll_s: float = 1.0            # system stats are polled only while the HUD is open
    disk_path: str = "/"
    model_processes: tuple[str, ...] = ("llama-server",)  # whose RSS counts as "the model's RAM"


# Section 13: conversation manners (barge-in, not overhearing dictation or calls, only answering when addressed).
@dataclass(frozen=True)
class MannersConfig:
    barge_in: bool = True              # a stage-1 wake trigger silences TTS at once, before verification
    barge_fade_ms: int = 40            # the fade-out on barge-in (TTS is silent < 100 ms after the trigger)
    barge_ignore_ms: int = 300         # triggers in the first 300 ms of each TTS chunk are ignored (echo onset)
    stop_words: bool = True            # "stop" / "wait" / "přestaň" … while JARVIS speaks (needs echo cancel)
    stop_min_speech_ms: int = 400      # this much speech before the quick Whisper pass
    stop_max_speech_s: float = 3.0     # longer speech while he talks isn't a stop word
    mic_busy: bool = True              # pause all listening while another app records the mic
    mic_busy_resume_s: float = 1.0     # listen again this long after that app stopped
    mic_busy_poll_s: float = 3.0       # safety-net poll (pactl subscribe events trigger re-checks at once)
    mic_busy_sources: tuple[str, ...] = ()   # more sources that count as the mic ([audio] mic, the echo-cancel
                                             # source, the default source and virtual sources always do)
    mic_busy_ignore: tuple[str, ...] = ("noctalia", "peak detect", "pavucontrol", "pwvucontrol", "cava",
                                        "easyeffects")   # substrings of app/node/media names that never count
    addressed_check: bool = True       # rules + fast-model classification of wake-started and follow-up turns
    addressed_budget_ms: int = 300     # the classifier's time budget
    addressed_fail_open: bool = True   # no answer in time: keep a normal-length turn (long speech is dropped)
    wake_max_words_before: int = 3     # more words of continuous speech before "Jarvis" = talking about him
    long_speech_s: float = 15.0        # a turn this long without the wake word at its start is dictation ...
    long_speech_min_confidence: float = 0.85   # ... unless the classifier is at least this sure
    require_name: bool = True          # only turns that call him by name (plus the turn right after the wake word, the
                                       # first question after a click, yes/no in the confirm window, stop words)
    barge_listen_s: float = 6.0        # after any barge-in trigger: stay stopped and listen this long (name needed)


# Section 14: desktop control and files.
@dataclass(frozen=True)
class DesktopConfig:
    enabled: bool = True
    launcher: str = "auto"             # "auto" (uwsm app if installed, else gtk-launch) | "uwsm" | "gtk-launch"
    app_aliases: dict[str, str] = field(default_factory=dict)   # spoken name -> desktop id, e.g. music = "quodlibet"
    screenshot_dir: str = "~/Pictures/Screenshots"
    lock_command: str = "~/.config/hypr/Scripts/lock.sh"   # what SUPER+L runs (falls back to hyprlock)
    terminal: tuple[str, ...] = ("ghostty", "--gtk-single-instance=false")  # wraps Terminal=true apps (nvim, btop)


@dataclass(frozen=True)
class FilesConfig:
    default_folder: str = "~/Documents/JARVIS"
    roots: tuple[str, ...] = ("~/Documents", "~/Desktop", "~/Downloads", "~/Projects", "~/Pictures", "~/Music",
                              "~/Videos")
    max_bytes: int = 1_000_000         # create/append limit (UTF-8 text only)
    read_max_bytes: int = 200_000


# Section 15: coding projects. opencode runs the job in a visible terminal on a heavier Ollama model, while the
# voice stays on the small model.
@dataclass(frozen=True)
class CodingConfig:
    enabled: bool = True
    model: str = "qwen3.8:27b-mtp-q4_K_M"   # an Ollama tag (Qwen3.8-27B dense, split GPU/RAM)
    # Ollama's VRAM-based default context is 4096 on this card, far too small for opencode's prompt and tools.
    # > 0: jarvis uses a derived model "jarvis-coder:<model>-<N/1024>k" with this num_ctx (same weights, created
    # through /api/create on first use; no download). 0 = the model as it is.
    num_ctx: int = 32768
    ollama_url: str = "http://127.0.0.1:11434"
    projects_dir: str = "~/Projects"
    opencode_bin: str = ""                  # "" = find it (the npm wrapper, else its platform binary)
    terminal: tuple[str, ...] = ("ghostty", "--gtk-single-instance=false")
    app_launcher: str = "auto"              # "auto" (uwsm app, else systemd-run --scope) | "none"
    min_free_vram_gb: float = 6.0           # below this (after unloading the 35B) the card warns first
    game_classes: tuple[str, ...] = ("steam_app_", "gamescope", ".exe", "lutris", "heroic", "wine", "proton",
                                     "retroarch", "minecraft")
    unload_smart_model: bool = True         # unload [llm] model (the 35B) through llama-swap before loading
    warm_up: bool = True                    # load the model (and measure tok/s) before the terminal opens
    keep_alive_after: str = "5m"            # Ollama keep_alive once the job has ended
    max_task_chars: int = 4000


# Section 17: run_command and the training tasks. Always a confirm card first, always a visible terminal.
@dataclass(frozen=True)
class CommandsConfig:
    enabled: bool = True
    default_workdir: str = "~"                 # must be an existing folder inside $HOME
    terminal: tuple[str, ...] = ("ghostty", "--gtk-single-instance=false")
    app_launcher: str = "auto"                 # "auto" (uwsm app, else systemd-run --scope) | "none"
    max_chars: int = 2000                      # longer commands are refused (they can't be read on a card)
    training_unit: str = "jarvis-finetune"
    training_script: str = "~/jarvis/finetune/start.sh"   # section 16: systemd-run with limits -> overnight.sh
    training_status_file: str = "~/jarvis/finetune/STATUS"
    training_hours: str = "12–13"             # shown on the card
    training_start_delay_s: float = 8.0        # the spoken line goes out first, then the terminal launches
    update_check_timeout_s: float = 120.0


# Section 18: the firm tracker (Geonix Wrench) and the once-a-day startup briefing.
@dataclass(frozen=True)
class FirmConfig:
    enabled: bool = False          # config.example.toml turns it on; a bare Config() (tests) never touches the network
    provider: str = "geonix"       # jarvis/integrations/firm/<provider>.py; credentials: jarvisctl setup firm geonix
    refresh_s: int = 900           # background refresh (only while jarvisd runs; at most one request a minute)
    timeout_s: float = 10.0
    stale_after_s: int = 1800      # numbers older than this are shown and spoken as stale


@dataclass(frozen=True)
class BriefingConfig:
    enabled: bool = True           # the pill menu's "Daily briefing" toggle overrides this (state.json)


@dataclass(frozen=True)
class ShowcaseConfig:
    """ "Jarvis, present yourself": section 25's cinematic showcase (jarvis/showcase/), or section 24's editable script
    (jarvis/integrations/showcase.py) with style = "script"."""
    enabled: bool = True
    style: str = "cinematic"            # "cinematic" (section 25) | "script" (section 24, ~/.config/jarvis/showcase.toml)
    mute: bool = False                  # true: the lines aren't spoken (the steps still run, at the speech's pace)
    # section 24 (style = "script")
    script: str = ""                    # "" = ~/.config/jarvis/showcase.toml (the repo default, copied on first use)
    # section 25 (style = "cinematic")
    terminal: str = ""                  # "" = [desktop] terminal (ghostty), then kitty, alacritty, foot, …
    browser: str = ""                   # "" = the default browser (Zen); its own profile, closed afterwards
    fresh_workspace: bool = True        # each window scene on an empty workspace, then back to yours
    workspace_settle_s: float = -1.0    # after a switch, for its animation (-1 = Hyprland's `workspaces` animation)
    takeover: bool = True               # any real key, click, wheel or mouse move stops it (needs the input group)
    takeover_grace_s: float = 2.0       # input in the first seconds doesn't count (the menu click)
    takeover_mouse_px: int = 60         # real mouse motion within 0.5 s that counts as taking over
    close_grace_s: float = 3.0          # SIGTERM, then SIGKILL after this, for the showcase's own windows
    scroll: bool = True                 # the web scene glides down the page and switches to its second tab
    code_cps: float = 150.0             # live coding: characters a second (the script's `cps` wins)


@dataclass(frozen=True)
class ComputerConfig:
    """Section 19: mouse/keyboard control (type_text, press_keys, mouse, and computer_task's vision loop)."""
    enabled: bool = True
    confirm: bool = False               # section 21: computer_task starts at once; true = the "Take control?" card
    max_steps: int = 40                 # one screenshot + one action per step
    max_seconds: int = 300
    image_width: int = 1280             # screenshots are downscaled to this width (in memory only)
    jpeg_quality: int = 80
    settle_s: float = 0.7               # after an action, before the next screenshot
    step_timeout_s: float = 90.0        # one model call (the first one may load the 35B)
    takeover_mouse_px: int = 25         # real-mouse motion (evdev units within 0.5 s) that hands control back
    debug_screenshots: bool = False     # True writes each step's screenshot to ~/.local/state/jarvis/computer/
    # Section 23: faster steps (docs/tuning.md). step_model = the llama-swap id that runs every step: the 4B with its
    # vision projector, fully on the GPU. The section 19 loop (the 35B for every step) is step_model = "jarvis",
    # prompt = "full", max_actions = 1, settle = "fixed", verify_done/zoom_retry = false.
    step_model: str = "qwen35-4b-vision"
    escalate_model: str = ""            # a llama-swap id that takes a step over when the step model is stuck ("" = never)
    prompt: str = "fast_note"           # "fast_note" (short, a 12-word screen note, several actions per reply), "fast", "full"
    max_actions: int = 4                # actions one reply may carry for one screenshot
    step_max_tokens: int = 200
    step_temperature: float = 0.0
    verify_done: bool = True            # the step model checks its own "done" once more on the screenshot
    zoom_retry: bool = True             # a click that changed nothing is re-aimed on a 4x zoomed crop
    history_steps: int = 6              # earlier steps sent as text (never old screenshots)
    settle: str = "adaptive"            # wait until the screen stops changing (settle_max_s at most); "fixed" = settle_s
    settle_max_s: float = 1.5
    escalate_after: int = 2             # steps in a row without a visible change before escalate_model looks
    give_up_after: int = 4              # this many steps in a row without a visible change end the task (0 = off)
    vram_need_mb: int = 5500            # free VRAM the 35B + mmproj needs before it loads for vision
    step_vram_need_mb: int = 5000       # free VRAM a small step model needs before it loads (else the voice model
                                        # leaves the GPU first, like vram_need_mb does for the 35B)
    direct: bool = True                 # simple goals ("open X", "search the web for Y") use open_app / open_url
    search_url: str = "https://duckduckgo.com/?q={q}"   # Zen's search engine


@dataclass(frozen=True)
class Config:
    persona: PersonaConfig = field(default_factory=PersonaConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    session: SessionConfig = field(default_factory=SessionConfig)
    ipc: IPCConfig = field(default_factory=IPCConfig)
    daemon: DaemonConfig = field(default_factory=DaemonConfig)
    wake: WakeConfig = field(default_factory=WakeConfig)
    audio: AudioConfig = field(default_factory=AudioConfig)
    stt: STTConfig = field(default_factory=STTConfig)
    tts: TTSConfig = field(default_factory=TTSConfig)
    email: EmailConfig = field(default_factory=EmailConfig)
    contacts: ContactsConfig = field(default_factory=ContactsConfig)
    news: NewsConfig = field(default_factory=NewsConfig)
    web: WebConfig = field(default_factory=WebConfig)
    weather: WeatherConfig = field(default_factory=WeatherConfig)
    reminders: RemindersConfig = field(default_factory=RemindersConfig)
    calendar: CalendarConfig = field(default_factory=CalendarConfig)
    system: SystemConfig = field(default_factory=SystemConfig)
    manners: MannersConfig = field(default_factory=MannersConfig)
    desktop: DesktopConfig = field(default_factory=DesktopConfig)
    files: FilesConfig = field(default_factory=FilesConfig)
    coding: CodingConfig = field(default_factory=CodingConfig)
    commands: CommandsConfig = field(default_factory=CommandsConfig)
    firm: FirmConfig = field(default_factory=FirmConfig)
    briefing: BriefingConfig = field(default_factory=BriefingConfig)
    computer: ComputerConfig = field(default_factory=ComputerConfig)
    showcase: ShowcaseConfig = field(default_factory=ShowcaseConfig)


def _merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def load_config(path: Path | None = None) -> Config:
    raw: dict[str, Any] = {}
    for candidate in (DEFAULTS_FILE, path or USER_FILE):
        if candidate.is_file():
            raw = _merge(raw, tomllib.loads(candidate.read_text()))

    sections = {}
    for f in fields(Config):
        section_cls = f.default_factory  # type: ignore[misc]
        known = {sf.name for sf in fields(section_cls)}
        values = {k: v for k, v in raw.get(f.name, {}).items() if k in known}
        sections[f.name] = section_cls(**values)
    return Config(**sections)
