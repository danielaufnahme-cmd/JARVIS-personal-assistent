# Section 1: Foundation (LLM runtime and model)

**Read first:** `JARVIS_BUILD_PROMPT.md` §2 (machine), §3 (rules), §5.3 (LLM).

## Goal
The local model `Qwen3.6-35B-A3B` is served through **llama-swap → llama-server (CUDA)** at
`http://127.0.0.1:8401/v1`. It **loads on the first request** and **unloads after 600 s idle**. Its expert weights
sit in RAM and run on the CPU, and the rest runs on the GPU. The speed is measured and written down.

## You own
- Everything under `~/.local/src/llama.cpp/` (source and build), and the binaries you install into `~/.local/bin/`
- `~/models/`
- `~/.config/llama-swap/config.yaml`
- `systemd/llama-swap.service` (and its installed copy in `~/.config/systemd/user/`)
- `scripts/bench_llm.py`
- `docs/tuning.md`

Do not touch `jarvis/`, `ui/` or any other `systemd/` unit. Other agents own those.

## Steps
1. **Build llama.cpp with CUDA, without sudo.** Clone `https://github.com/ggml-org/llama.cpp` into
   `~/.local/src/llama.cpp`. Configure it with `cmake -B build -G Ninja -DGGML_CUDA=ON -DCMAKE_CUDA_ARCHITECTURES=86
   -DCMAKE_BUILD_TYPE=Release -DLLAMA_CURL=ON`. CUDA is `/opt/cuda` (nvcc 13.4). If nvcc rejects the system gcc,
   look for an older host compiler (`g++-14`, `g++-13`) and pass `-DCMAKE_CUDA_HOST_COMPILER`. Build with
   `cmake --build build -j 20`. Symlink `llama-server` and `llama-bench` into `~/.local/bin/`.
   Check it: `llama-server --version`, and `--help` must list `--n-cpu-moe`.
2. **llama-swap:** download the release `v258`, asset `llama-swap_258_linux_amd64.tar.gz`, from
   `github.com/mostlygeek/llama-swap`. Install it to `~/.local/bin/llama-swap`. Read its README for the config
   format, the `ttl`, the listen flag, and the **unload and running-model endpoints**. Section 2/3 need those
   endpoints, so write them into `docs/tuning.md`.
3. **Model:** download `unsloth/Qwen3.6-35B-A3B-GGUF` → `Qwen3.6-35B-A3B-UD-IQ4_XS.gguf` (17.7 GB) to `~/models/`:
   `curl -L -C - -o ~/models/Qwen3.6-35B-A3B-UD-IQ4_XS.gguf https://huggingface.co/unsloth/Qwen3.6-35B-A3B-GGUF/resolve/main/Qwen3.6-35B-A3B-UD-IQ4_XS.gguf`.
   Run it in the background and build/configure while it downloads. When it finishes, check that the size is
   17730509792 bytes.
4. **Config** `~/.config/llama-swap/config.yaml`: one model named `jarvis`, using the command from §5.3
   (`-ngl 99 --n-cpu-moe <N> --threads 10 --threads-batch 12 -c 16384 --jinja --flash-attn on`), `ttl: 600`.
   llama-swap listens on **127.0.0.1:8401** (localhost only).
5. **systemd user unit** `systemd/llama-swap.service`: `ExecStart=%h/.local/bin/llama-swap --config
   %h/.config/llama-swap/config.yaml --listen 127.0.0.1:8401` (use whatever flag names the README actually gives),
   `Restart=on-failure`. Copy it to `~/.config/systemd/user/`, then run `systemctl --user daemon-reload` and
   `systemctl --user enable --now llama-swap`. This is cheap, because no model loads until the first request.
6. **Check the API:**
   - A plain chat completion works.
   - **Tool calling works:** send one dummy `get_time` tool and check that the response contains a proper `tool_calls` entry.
   - **Thinking can be turned on and off per request.** Try `"chat_template_kwargs": {"enable_thinking": false}`
     in the request body, and check that the output has no `<think>` block and no `reasoning_content`. Then try it
     with `true`. Write down the exact knob that works.
7. **`scripts/bench_llm.py`** (a standalone script using httpx) measures:
   - cold load time (the first request after an unload)
   - warm time to the first token
   - generation tokens/s for a 300-token voice answer and a 1500-token deep answer
   - VRAM (`nvidia-smi --query-gpu=memory.used`) and the RSS of llama-server
8. **Tune `--n-cpu-moe`.** Start at 99 (all experts on the CPU) and step down (for example 99 → 40 → 36 → 32 → …).
   Stop at the setting where **llama-server's VRAM is ≤ 7.5 GB**; that leaves about 1.5 GB for Whisper and about
   1 GB for the desktop. Before each measurement, run `ollama ps`. If the user's Ollama has a model loaded, wait
   for it to unload or write that down; **never stop Ollama**. Put the best value into the config.
9. **Idle unload:** temporarily set `ttl: 60` and check that the model unloads on its own (RSS gone, VRAM freed),
   then set it back to `600`. Also check that the explicit unload endpoint works.

## Acceptance checks
- `curl -s 127.0.0.1:8401/v1/models` lists `jarvis`.
- Tool calling works, and thinking on/off works (the exact request knob is written down).
- `docs/tuning.md` has a table of `--n-cpu-moe` value against VRAM, RAM, tokens/s and time to the first token,
  plus the chosen value, the cold load time, the unload endpoint, the running endpoint, and the thinking knob.
- The ttl unload and the explicit unload are both shown to work.
- `systemctl --user is-active llama-swap` → `active`.

## Report back
Chosen `--n-cpu-moe`, tokens/s, time to the first token, cold load time, VRAM and RAM figures, the endpoints and
knobs, and anything that didn't work.
