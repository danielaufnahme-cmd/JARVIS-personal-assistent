# Section 14: Desktop control and files

**Read first:** `JARVIS_BUILD_PROMPT.md` §3 (rules 2 and 3: the approval gate, external content is data), §5.5, §6,
§7; `jarvis/gate.py`, `jarvis/tools/registry.py`, `jarvis/tools/drafts.py`, `ui/DraftCard.qml`,
`ui/hud/HudDraft.qml`; the user's memory notes: Hyprland is configured in **Lua** (`hyprctl dispatch` takes
Lua) and the session runs under **uwsm**.

## The user's request (2026-09-26)
"Make JARVIS open apps and more stuff, and when I ask it to create a file it should do it."

## You own
- `jarvis/tools/desktop.py`, `jarvis/tools/files.py`, `jarvis/integrations/desktop.py`
- **The generic action extension of `jarvis/gate.py`** (additive; keep every existing behaviour and test)
- `ui/DraftCard.qml` and `ui/hud/HudDraft.qml` (rendering action cards)
- Additive lines in `jarvis/tools/registry.py` and `jarvis/prompts/system.md` (under a new "Desktop and files:"
  heading; re-read before editing, because sections 10 and 13 also add prompt lines)
- `[desktop]` / `[files]` in the config, `tests/test_desktop_*.py`, `tests/test_files_*.py`

## 1. Generic confirmable actions (the gate)
Section 15 codes against this exact interface at the same time, so keep it:

```python
# jarvis/gate.py
Executor = Callable[[dict[str, Any]], Awaitable[str | None]]   # returns a short result line for JARVIS to say/show
class ApprovalGate:
    def register_executor(self, action: str, fn: Executor) -> None      # e.g. "file.write", "app.close", "project.start"
    def create_action(self, action: str, title: str, preview: str, payload: dict[str, Any],
                      confirm_label: str = "Confirm") -> PendingAction  # kind="action"
```

- `PendingAction` gains the optional fields `action`, `payload` and `confirm_label`.
- `execute_pending` calls the registered executor with the payload, keeping all the existing safety: the stale-id
  check, clearing the pending action before awaiting, the failure → `draft_cleared result="failed"` path, and one
  pending at a time.
- The outbox log records the action name and title, never the payload contents.
- The `draft` event/snapshot for an action: `{"id","kind":"action","action","title","body": preview,"confirm_label"}`.
- **An executor can only run through `execute_pending`,** the same as senders. Extend section 2's registry-walk
  safety test: no tool can call an executor directly.
- **UI:** `DraftCard.qml` and `HudDraft.qml` render `kind:"action"` with the title, a monospace preview (for example
  the file path + the first ~20 lines, or "Close Firefox (2 windows)"), and the `confirm_label` button, plus Cancel.
  Theme tokens only.

## 2. Desktop tools (no confirmation needed; harmless)
- `open_app(name)`: resolve through the XDG desktop entries (`~/.local/share/applications`,
  `/usr/share/applications`, flatpak exports), fuzzy-matching Name/GenericName/Keywords/the desktop id. Launch it
  as a proper session app, e.g. `uwsm app -- <id>.desktop` (check that `uwsm` exists; fall back to `gtk-launch`).
  If it's ambiguous, return the top 3 candidates so JARVIS can ask. **Never run arbitrary commands, only desktop
  entries.**
- `open_url(url)`: http/https only → `xdg-open`. `open_path(path)`: an existing file or folder under $HOME →
  `xdg-open`. Refuse dotfiles/dot-directories.
- `media(action: play|pause|toggle|next|previous|status)` through `playerctl` (it reports the current track).
- `switch_workspace(n)`, `focus_app(name)`: through `hyprctl`. The dispatch syntax on this machine is **Lua**
  (for example `hyprctl dispatch 'hl.dsp.workspace(3)'`). Check the exact forms in `~/.config/hypr/keybind.lua` and
  test them with harmless no-ops. **Don't move the user's windows around while testing.**
- `screenshot(region: full|window)`: `grim` into `~/Pictures/Screenshots/` (create it), returning the path.
  `lock_screen()`: use what SUPER+L runs in keybind.lua (hyprlock). **Never actually run it in tests.**
- `list_windows()`: open apps and titles (titles are external content, so wrap them).

## 3. Closing apps (confirm)
*(Superseded by section 19, 2026-09-27: `close_app` now closes directly without a card, using the same focus-then-verify graceful close; the `app.close` action no longer exists.)* `close_app(name)` → `gate.create_action("app.close", title="Close Firefox?", preview="2 windows: …")`. The executor
closes that app's windows gracefully through hyprctl (a close window dispatch, never kill -9).

## 4. Files
- `create_file(name, content, folder=None)`:
  - Default folder `~/Documents/JARVIS/` (create it).
  - Allowed roots: `~/Documents`, `~/Desktop`, `~/Downloads`, `~/Projects`, `~/Pictures`, `~/Music`, `~/Videos`,
    and the JARVIS folder.
  - A **new** file in an allowed root is created **directly** (the user asked for that) and JARVIS says where.
  - **Overwriting an existing file, or any path outside the allowed roots, goes through `create_action("file.write", …)`**
    with a diff/preview.
  - **Always refused, even with confirmation:** dotfiles or any path component starting with ".", `~/.config`,
    `~/.ssh`, `~/.gnupg`, `~/.local`, anything outside $HOME, symlinks that escape the roots, and binary content.
  - Limits: max 1 MB, UTF-8 text. Refuse unusual extensions (`.sh`/`.desktop`/`.service` outside `~/Projects`).
  - Create the parent directories inside the roots only.
- `append_to_file(path, content)`: allowed-roots only; confirm unless the file is in the JARVIS folder.
- `read_file(path)`: allowed roots only, text ≤ 200 kB, wrapped as `<external_content source="file">`.
- `list_folder(path)`: allowed roots, names only.
- **Safety:** an instruction inside external content (email/news/page/file) must never lead to a file write
  without the user's own request. Add injection tests: a web page saying "create ~/.bashrc…" or "write a file
  with…", read through the agent with a scripted fake LLM → no write, and dotfiles refused regardless.
- Voice style: "Created shopping-list.txt in Documents, JARVIS folder." One short line.

## 5. Prompt
Add "Desktop and files:" rules to `system.md`: use the tools for these requests; to create a file, use
create_file; never claim you opened, closed or created something unless the tool returned success; for ambiguous
apps, ask one short question.

## Rules
No sudo. Don't restart the live jarvisd (the orchestrator does it once). Nothing audible. **No synthetic input,
and don't open or close the user's real apps in tests,** except ONE harmless live check of `open_app` resolution in
dry-run mode (`--dry-run` prints the command). Use fakes for hyprctl/playerctl/uwsm in the tests. Sections 10, 13
(paused, waiting for section 12) and 15 (coding projects, in parallel; it uses your gate interface) also touch
agent.py / voice.py / system.md, so make only small, re-read-first edits in shared files.

## Acceptance checks
- The whole suite is green.
- A dry-run of `open_app` for "firefox", "steam", "files", "terminal" and "spotify" resolves to the right desktop
  ids.
- Every file-safety case has a test.
- Screenshots of an action card, in the corner and in the HUD (offscreen harness or a second qs on a temporary
  socket).
