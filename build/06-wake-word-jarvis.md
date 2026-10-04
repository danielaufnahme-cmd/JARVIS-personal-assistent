# Section 6: The "Jarvis" wake word

**Read first:** `JARVIS_BUILD_PROMPT.md` §5.1.

## Goal
A plain **"Jarvis"** wakes it reliably. "Hey Jarvis" works too. Nothing else does.

## You own
`wakeword/` (training config, `jarvis.onnx`, eval scripts) and the wake-word part of `config.example.toml`.

## Rules for this build
- **Don't ask the user anything now.** Recordings of the user's own voice come later, so build the evaluation so
  that it takes a folder of WAVs, and run it now on **synthetic, held-out test clips** (voices/speeds not used in
  training) plus a speech corpus for false wakes.
- Section 5 is building the audio pipeline at the same time. You deliver a model file plus its threshold; section 5
  loads whatever `[wake] model` in the config points to (the default `hey_jarvis`).
- GPU is shared: llama-server may hold about 7.4 GB and Whisper about 1.5 GB. Use the GPU only while
  `nvidia-smi` shows ≥ 2 GB free, otherwise the CPU. **Never unload the JARVIS model yourself.**
- Downloads can be large (negative-feature datasets). Keep everything under `wakeword/data/` (git-ignored) and
  report the disk use.

## Steps
1. Get a baseline for the pre-trained openWakeWord `hey_jarvis` on your synthetic test set: plain "Jarvis", "Hey
   Jarvis", and the near-miss negatives.
2. Train a single-word model with **livekit-wakeword** (preferred: one YAML config) or the openWakeWord training
   pipeline.
   - Positives: `jarvis` and `hey jarvis`, with many voices, speeds and accents (English and Czech-accented
     English), and room-impulse and noise augmentation.
   - Negatives: "service", "nervous", "Travis", "Harvey", "car keys", "jars", "garbage", "jar of", "office",
     plus generic speech.
3. Evaluate both models on the same held-out set: detection rate and false wakes per hour on the negative corpus.
   Set the threshold from the score distribution.
4. Output `wakeword/jarvis.onnx` (+ `wakeword/jarvis.yaml` with the threshold, and the metrics in
   `docs/tuning.md`). Add `[wake] model`, `threshold` and `refractory_s` to `config.py`/`config.example.toml` if
   section 5 hasn't already; coordinate by re-reading the files before editing.
5. `wakeword/eval.py <folder>`: the same evaluation on any folder of recordings, ready for the user's own samples
   later.

## Acceptance checks
On the synthetic held-out set: ≥ 95 % detection of a plain "Jarvis" and ≤ 0.5 false wakes/hour on the negative
corpus, better than `hey_jarvis` for a single "Jarvis". Write the numbers into `docs/tuning.md`. The real-room test
(≥ 9/10 at 2 m, 0 false wakes in 30 min) is done later with the user.
