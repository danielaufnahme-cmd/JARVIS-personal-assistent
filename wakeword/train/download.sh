#!/usr/bin/env bash
# Re-create everything under wakeword/data/ that training and the test set need (~30 GB).
# Order: this script, then train/gen_testset.py (test set), then train/run.sh all (training).
set -euo pipefail
WW="$(cd "$(dirname "$0")/.." && pwd)"
D="$WW/data"
mkdir -p "$D"/{tools,corpus/train_bg/fleurs_cs,corpus/fleurs_cs,voices,voices_train}

# training venv (torch/piper stay out of the main project env)
[ -d "$D/livekit-wakeword" ] || git clone https://github.com/livekit/livekit-wakeword.git "$D/livekit-wakeword"
[ -x "$WW/.venv-train/bin/python" ] || uv venv --python 3.12 "$WW/.venv-train"
VIRTUAL_ENV="$WW/.venv-train" uv pip install -e "$D/livekit-wakeword[train,eval,export]" openwakeword piper-tts kokoro-onnx
cp -n "$WW/../.venv/lib/python3.12/site-packages/openwakeword/resources/models/"*.onnx \
      "$WW/.venv-train/lib/python3.12/site-packages/openwakeword/resources/models/" 2>/dev/null || true

# espeak-ng CLI (livekit's Piper generator shells out to it; no sudo needed)
if [ ! -x "$D/tools/espeak-ng/bin/espeak-ng" ]; then
  git clone --depth 1 https://github.com/espeak-ng/espeak-ng.git "$D/tools/espeak-ng-src"
  cmake -S "$D/tools/espeak-ng-src" -B "$D/tools/espeak-ng-src/build" -DCMAKE_INSTALL_PREFIX="$D/tools/espeak-ng" \
    -DCMAKE_BUILD_TYPE=Release -DUSE_ASYNC=OFF -DUSE_MBROLA=OFF -DUSE_LIBSONIC=OFF -DUSE_LIBPCAUDIO=OFF -DUSE_SPEECHPLAYER=OFF
  cmake --build "$D/tools/espeak-ng-src/build" -j16 && cmake --install "$D/tools/espeak-ng-src/build"
fi

# ACAV100M features (17 GB), validation features, MUSAN noise, MIT RIRs, Piper LibriTTS checkpoint
HF_HOME="$D/hf" "$WW/.venv-train/bin/livekit-wakeword" setup --config "$WW/train/setup.yaml"

# Piper voices: held-out (test set) and training-only
B=https://huggingface.co/rhasspy/piper-voices/resolve/main
get() { [ -f "$1/$(basename "$2").onnx" ] || { curl -fsSL -o "$1/$(basename "$2").onnx" "$B/$2.onnx"; curl -fsSL -o "$1/$(basename "$2").onnx.json" "$B/$2.onnx.json"; }; }
for v in en/en_GB/vctk/medium/en_GB-vctk-medium en/en_US/l2arctic/medium/en_US-l2arctic-medium en/en_GB/alan/medium/en_GB-alan-medium \
  en/en_GB/northern_english_male/medium/en_GB-northern_english_male-medium en/en_GB/cori/high/en_GB-cori-high en/en_US/ryan/high/en_US-ryan-high \
  en/en_US/joe/medium/en_US-joe-medium cs/cs_CZ/jirka/medium/cs_CZ-jirka-medium en/en_GB/semaine/medium/en_GB-semaine-medium en/en_US/amy/medium/en_US-amy-medium; do
  get "$D/voices" "$v"; done
for v in en/en_US/libritts_r/medium/en_US-libritts_r-medium en/en_US/arctic/medium/en_US-arctic-medium en/en_GB/aru/medium/en_GB-aru-medium \
  en/en_US/hfc_female/medium/en_US-hfc_female-medium en/en_US/hfc_male/medium/en_US-hfc_male-medium en/en_US/lessac/medium/en_US-lessac-medium \
  en/en_US/kristin/medium/en_US-kristin-medium en/en_US/norman/medium/en_US-norman-medium en/en_US/bryce/medium/en_US-bryce-medium \
  en/en_US/john/medium/en_US-john-medium en/en_US/danny/low/en_US-danny-low en/en_US/kusal/medium/en_US-kusal-medium \
  en/en_US/reza_ibrahim/medium/en_US-reza_ibrahim-medium en/en_US/sam/medium/en_US-sam-medium en/en_US/ljspeech/medium/en_US-ljspeech-medium \
  en/en_GB/alba/medium/en_GB-alba-medium en/en_GB/jenny_dioco/medium/en_GB-jenny_dioco-medium en/en_GB/southern_english_female/low/en_GB-southern_english_female-low; do
  get "$D/voices_train" "$v"; done

# speech: LibriSpeech dev/test + FLEURS cs dev/test (false-trigger corpus), train-clean-100 + FLEURS cs train (backgrounds)
cd "$D/corpus"
for f in dev-clean dev-other test-clean test-other; do [ -d LibriSpeech/$f ] || curl -fsSL https://www.openslr.org/resources/12/$f.tar.gz | tar xz; done
for s in dev test; do [ -d fleurs_cs/$s ] || curl -fsSL "https://huggingface.co/datasets/google/fleurs/resolve/main/data/cs_cz/audio/$s.tar.gz" | tar xz -C fleurs_cs; done
cd "$D/corpus/train_bg"
[ -d LibriSpeech/train-clean-100 ] || curl -fsSL https://www.openslr.org/resources/12/train-clean-100.tar.gz | tar xz
[ -d fleurs_cs/train ] || curl -fsSL "https://huggingface.co/datasets/google/fleurs/resolve/main/data/cs_cz/audio/train.tar.gz" | tar xz -C fleurs_cs
echo "done. Kokoro (test set only) is read from ~/models/kokoro (section 5)."
