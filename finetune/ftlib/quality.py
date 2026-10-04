"""The automatic filter (build/16 step 4): which teacher traces are good enough to train on, and why not."""

from __future__ import annotations

from typing import Any


def turn_problems(label: dict[str, Any], t: dict[str, Any]) -> list[str]:
    s = t["score"]
    out: list[str] = []
    if "something went wrong" in t["reply"]:
        return ["agent error"]
    detail = s.get("detail") or ""
    if any(k in detail for k in ("bad JSON", "unknown tool", "missing ", "unknown argument", "is not a", "not in [")):
        out.append("schema: " + detail[:120])
    elif not s["tool_ok"]:
        out.append("tool: " + detail[:120])
    if s["safety_ok"] is False:
        out.append("safety: " + detail[:120])
    st = s["style"]
    if not t.get("code_deep_route"):
        if st["sentences"] == 0:
            out.append("style: empty reply")
        elif st["sentences"] > int(label.get("max_sentences", 3)):
            out.append(f"style: {st['sentences']} sentences (max {label.get('max_sentences', 3)})")
        if not st["no_apology"]:
            out.append("style: apology")
        if not st["no_markdown"]:
            out.append("style: markdown")
    if st.get("claims"):
        out.append("claim without tool: " + ",".join(st["claims"]))
    if not t["calls"] and not t.get("code_deep_route"):
        out.append("no model call")
    return out


def trace_problems(item: dict[str, Any], trace: dict[str, Any]) -> list[str]:
    probs: list[str] = []
    for label, t in zip(item["turns"], trace["turns"], strict=True):
        probs += turn_problems(label, t)
    return probs


def reason_key(problem: str) -> str:
    """A short bucket for the rejection statistics."""
    head, _, rest = problem.partition(": ")
    if head == "tool":
        if "unasked action" in rest:
            return "tool: unasked action"
        if "want no tool" in rest:
            return "tool: called a tool, want none"
        if "should ask" in rest:
            return "tool: didn't ask a question"
        if "made a draft" in rest:
            return "tool: made a draft instead of asking"
        if "called [] " in rest or "called nothing" in rest:
            return "tool: no tool call"
        if "want " in rest and "called" in rest:
            return "tool: wrong tool"
        return "tool: wrong arguments"
    if head == "style":
        return "style: too long" if "sentences" in rest else f"style: {rest.split()[0]}"
    return head
