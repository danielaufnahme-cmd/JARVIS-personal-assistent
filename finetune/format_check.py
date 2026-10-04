"""Stage 1b (train venv): our training renderer must match what llama-server actually renders, byte for byte.

    finetune/.venv-train/bin/python finetune/format_check.py

Starts a CPU-only llama-server with the served Qwen3.5-2B GGUF on a private port, sends every request the Agent
made in format_dump.py's scripted conversation to /apply-template (tools + enable_thinking false, exactly as
jarvis/llm.py sends them), and compares with `tokenizer.apply_chat_template` + the GGUF's template. Also checks
that a request + the model's answer renders as an exact prefix-extension (so one training sequence per turn has
the same tokens the model saw at inference). Writes data/format/check.json; exits 1 on any difference.
"""

from __future__ import annotations

import difflib
import json
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ftlib import guard  # noqa: E402
from ftlib.paths import BASE_2B_GGUF, FORMAT_PORT, LLAMA_SRC, d, setup_logging  # noqa: E402

log = setup_logging("format")


def ensure_template() -> None:
    path = d("format", "chat_template.jinja")
    if path.is_file():
        return
    sys.path.insert(0, str(LLAMA_SRC / "gguf-py"))
    from gguf import GGUFReader  # type: ignore[import-not-found]

    r = GGUFReader(str(BASE_2B_GGUF))
    f = r.fields["tokenizer.chat_template"]
    path.write_text(bytes(f.parts[f.data[0]]).decode("utf-8"), encoding="utf-8")


def server_render(port: int, messages: list, tools: list | None) -> str:
    body = {"messages": messages, "chat_template_kwargs": {"enable_thinking": False}}
    if tools:
        body["tools"] = tools
    req = urllib.request.Request(f"http://127.0.0.1:{port}/apply-template", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())["prompt"]


def main() -> int:
    ensure_template()
    from ftlib.render import render, tokenizer

    calls = json.loads(d("format", "scripted_calls.json").read_text(encoding="utf-8"))
    srv = guard.small_server("format-2b", BASE_2B_GGUF, FORMAT_PORT, cpu_only=True)
    srv.args = ["-c", "8192", "--jinja", "-ngl", "0", "--threads", "4"]
    srv.start()
    results = []
    try:
        for i, call in enumerate(calls):
            ours = render(call["messages"], call["tools"], True)
            theirs = server_render(FORMAT_PORT, call["messages"], call["tools"])
            same = ours == theirs
            results.append({"call": i, "same": same, "chars": len(ours)})
            if not same:
                diff = "".join(difflib.unified_diff(theirs.splitlines(True), ours.splitlines(True), "llama-server",
                                                    "ours", n=1))
                d("format", f"diff-call{i}.txt").write_text(diff, encoding="utf-8")
                log.error("call %d differs from llama-server (see diff-call%d.txt)", i, i)
            else:
                log.info("call %d: identical to llama-server (%d chars, %d tokens)", i, len(ours),
                         len(tokenizer()(ours, add_special_tokens=False)["input_ids"]))
    finally:
        srv.stop()
    # A request plus the model's answer must extend the request's rendering exactly.
    last = calls[-1]
    prompt = render(last["messages"], last["tools"], True)
    full = render(last["messages"] + [{"role": "assistant", "content": "You have three emails; the newest is from "
                                                                       "Alice."}], last["tools"], False)
    prefix_ok = full.startswith(prompt)
    tok = tokenizer()
    a = tok(prompt, add_special_tokens=False)["input_ids"]
    b = tok(full, add_special_tokens=False)["input_ids"]
    token_prefix_ok = b[: len(a)] == a
    first = calls[0]
    mid_full = render(calls[1]["messages"], calls[1]["tools"], True)
    first_prompt = render(first["messages"], first["tools"], True)
    round_prefix_ok = mid_full.startswith(first_prompt)
    report = {"calls": results, "all_identical": all(r["same"] for r in results), "answer_extends_prompt": prefix_ok,
              "token_prefix_ok": token_prefix_ok, "tool_round_extends_prompt": round_prefix_ok,
              "system_plus_tools_tokens": len(tok(render(first["messages"][:1] + [{"role": "user", "content": "x"}],
                                                        first["tools"], False), add_special_tokens=False)["input_ids"])}
    d("format", "check.json").write_text(json.dumps(report, indent=1), encoding="utf-8")
    log.info("format check: %s", json.dumps(report))
    ok = report["all_identical"] and prefix_ok and token_prefix_ok and round_prefix_ok
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
