# Training JARVIS's small voice model

JARVIS normally answers with Qwen3.5-4B. The 2B model is twice as fast and uses half the graphics memory, but it
picks the wrong tool more often. This training teaches the 2B to use JARVIS's tools like the big 35B model does:
the 35B answers about 2,800 practice requests (with pretend tools, so nothing on your computer is touched), the
2B learns from those answers, and then both are tested on 330 requests the 2B never saw.

## Start it (one command)

```bash
~/jarvis/finetune/start.sh
```

It runs in the background (closing the terminal doesn't stop it). It switches JARVIS off during training, so the
graphics card is free for it, and **switches JARVIS back on by itself at the end**, whether it finished, failed or
was stopped. Running the same command again later **continues where it stopped** (for example after a crash or a
power cut; nothing already done is lost or done twice).

- **How long:** about 12–13 hours of work, plus any pauses (see below).
- **Progress:** `cat ~/jarvis/finetune/STATUS` (one line: the step, how far along, the time left, and "paused: …"
  when it waits).
- **The log:** `journalctl --user -u jarvis-finetune -f`
- **Stop:** `systemctl --user stop jarvis-finetune` (JARVIS comes back within a minute).
- **Resume:** `~/jarvis/finetune/start.sh` again.

## It keeps your PC usable

- **RAM:** it always leaves at least 4 GB of memory free. If other programs need more, it stops starting new work
  ("paused: keeping RAM free"); below 3 GB, or when the system starts struggling for memory, it shuts its big model
  down at once, and it carries on once 6 GB have been free for a minute. The 35B model needs about 11–13 GB of RAM
  while it works (the first ~3–4 hours), so **with big memory users open (a GitLab container, a local AI model in
  Ollama, many browser tabs) it waits until there's room**. Closing them lets it start. The training part after
  that needs much less memory.
- **A hard limit on top:** start.sh runs it inside kernel limits (it may use at most your RAM minus 5 GB before it
  is slowed down, and minus 3 GB at most; 75 % of the CPU; low CPU and disk priority), so even a fault in the
  training can't freeze the PC.
- **CPU:** its programs use at most 8 of your 24 threads each, so there are always threads free for the desktop.
- **Graphics card:** it pauses while a game runs full screen, or while another program (a game) uses a lot of
  graphics memory ("paused: game running"), and it always leaves some graphics memory free.

If you start JARVIS yourself while it trains, the training waits ("paused: keeping VRAM for JARVIS" in the log)
until JARVIS is stopped again.

## When it's done

- **`~/jarvis/finetune/RESULT.txt`**: one paragraph in plain English: whether the new model was installed,
  whether it is now the active one, and the key numbers.
- **`~/jarvis/docs/finetune_2b.md`**: the full report.
- The new model is only made the active voice model if it is at least as good as the 4B on the test (95 %
  correct tool calls, fully safe, answers as short and clean as the 4B's). Otherwise it's installed, but the 4B
  stays active.
- **Switching models:** right-click the JARVIS orb → *Fast model* → `qwen35-2b-jarvis` (the new one) or
  `qwen35-4b` (the old one).

## The slower alternative: JARVIS keeps working

```bash
~/jarvis/finetune/start.sh --with-jarvis
```

Trains next to the running JARVIS (with a smaller, memory-saving method), so JARVIS keeps working, though its
answers may be slower. It takes about 20 hours and needs more free RAM for the 35B part. It doesn't install
anything; afterwards run `~/jarvis/finetune/install_model.sh`. Stop, resume and progress work the same way.

## By voice

"Jarvis, start the training" shows a confirm card and then runs the same `start.sh`. "Training status" and "stop
the training" work too.

## Optional: a system-wide safety net

The training watches memory itself, but a general guard against *any* program freezing the PC is a good idea. If
you want one, install it yourself (it needs your password, so JARVIS never does this):

```bash
sudo pacman -S earlyoom && sudo systemctl enable --now earlyoom
```

It closes the biggest memory hog before the system can lock up.

Details for developers: `finetune/README.md`.
