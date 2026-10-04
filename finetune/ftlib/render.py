"""Rendering conversations into training text with the SAME chat template llama-server uses (train venv).

The template is the one embedded in the served Qwen3.5-2B GGUF (format stage), applied with
`tokenizer.apply_chat_template(messages, tools=…, enable_thinking=False)`. format_check.py proves that for real
Agent requests the result is byte-identical to llama-server's /apply-template.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from ftlib.paths import BASE_HF, d

_TOK = None
ASSIST_OPEN = "<|im_start|>assistant\n<think>\n\n</think>\n\n"
IM_END = "<|im_end|>"


def tokenizer() -> Any:
    global _TOK
    if _TOK is None:
        from transformers import AutoTokenizer

        tok = AutoTokenizer.from_pretrained(str(BASE_HF))
        tmpl = d("format", "chat_template.jinja")
        if tmpl.is_file():
            tok.chat_template = tmpl.read_text(encoding="utf-8")
        _TOK = tok
    return _TOK


def hf_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """OpenAI-style messages (tool-call arguments as JSON text, as the Agent sends them) -> what the HF template
    wants (arguments as a mapping, in the same key order llama-server parses them in)."""
    out = copy.deepcopy(messages)
    for m in out:
        for tc in m.get("tool_calls") or []:
            fn = tc.get("function", tc)
            if isinstance(fn.get("arguments"), str):
                try:
                    fn["arguments"] = json.loads(fn["arguments"] or "{}")
                except json.JSONDecodeError:
                    fn["arguments"] = {}
        if m.get("content") is None:
            m["content"] = ""
    return out


def render(messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None, generation_prompt: bool) -> str:
    return tokenizer().apply_chat_template(hf_messages(messages), tools=tools, tokenize=False,
                                           add_generation_prompt=generation_prompt, enable_thinking=False)


def assistant_spans(text: str, prompt_len: int) -> list[tuple[int, int]]:
    """Character spans to train on: every assistant message that starts after `prompt_len` minus the header
    (the header is the generation prompt the server adds), up to and including <|im_end|>."""
    spans = []
    pos = prompt_len
    while True:
        start = text.find(ASSIST_OPEN, pos)
        if start < 0:
            break
        body = start + len(ASSIST_OPEN)
        end = text.find(IM_END, body)
        if end < 0:
            break
        spans.append((body, end + len(IM_END)))
        pos = end
    return spans


def tokenize_with_labels(text: str, spans: list[tuple[int, int]]) -> tuple[list[int], list[int]]:
    tok = tokenizer()
    enc = tok(text, add_special_tokens=False, return_offsets_mapping=True)
    ids = enc["input_ids"]
    labels = [-100] * len(ids)
    for i, (a, b) in enumerate(enc["offset_mapping"]):
        if any(s <= a and b <= e for s, e in spans) and b > a:
            labels[i] = ids[i]
    return ids, labels


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))
