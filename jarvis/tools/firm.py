"""Firm tools (section 18): firm_summary and firm_metric for Geonix Wrench.

Both only read the firm service's cache (refreshed in the background every 15 min; a tool call fetches only if
the numbers are older than that, and never more than once a minute). They return numbers, not prose, plus a
ready spoken line. A firm that isn't connected (no token, endpoint not deployed, token rejected) returns a
dead-end status (not_configured / unavailable / disabled) so the agent answers in one line without more tools.

The provider only sends aggregates (no names or emails), and every string in a result is either ours or
validated (a 3-letter currency, an ISO timestamp), so no provider text reaches the model.
"""

from __future__ import annotations

from typing import Any

from jarvis.tools.registry import Tool, ToolContext, params

METRIC_ALIASES = {
    "monthly_earnings": "monthly_earnings", "monthly": "monthly_earnings", "earnings": "monthly_earnings",
    "revenue": "monthly_earnings", "mrr": "monthly_earnings", "money": "monthly_earnings", "income": "monthly_earnings",
    "this_month": "monthly_earnings", "month": "monthly_earnings",
    "total_earned": "total_earned", "total": "total_earned", "total_earnings": "total_earned",
    "total_revenue": "total_earned", "all_time": "total_earned", "lifetime": "total_earned",
    "subscribers": "subscribers", "subscriptions": "subscribers", "subs": "subscribers", "customers": "subscribers",
    "users": "subscribers", "active": "subscribers", "active_users": "subscribers", "shops": "subscribers",
    "paying": "subscribers", "seats": "subscribers",
    "job_cards": "job_cards", "jobcards": "job_cards", "job_card": "job_cards", "jobs": "job_cards",
    "pdfs": "job_cards", "pdf": "job_cards", "features": "job_cards", "usage": "job_cards",
    "signups": "signups", "sign_ups": "signups", "registrations": "signups", "new_users": "signups",
}


def _service(ctx: ToolContext) -> Any:
    if getattr(ctx, "firm", None) is not None:
        return ctx.firm
    from jarvis.config import Config
    from jarvis.integrations.firm import firm_service

    return firm_service(ctx.cfg if ctx.cfg is not None else Config())


async def _numbers(ctx: ToolContext) -> tuple[Any, dict[str, Any] | None, dict[str, Any] | None]:
    """(service, firm summary, dead-end result). Exactly one of the last two is None."""
    service = _service(ctx)
    await service.refresh()
    dead = service.dead_end()
    if dead is not None:
        return service, None, dead
    return service, service.summary(), None


def _freshness(firm: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {"as_of": firm["as_of"], "as_of_day": firm["as_of_day"], "stale": bool(firm["stale"])}
    if firm["stale"]:
        when = firm["as_of"] if firm["as_of_day"] == "Today" else f"{firm['as_of_day']} {firm['as_of']}"
        out["note"] = f"These are the last known numbers, from {when}; the latest refresh failed. Say they're from then."
    return out


async def _firm_summary(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    from jarvis.integrations.firm import money_text, summary_line

    _, firm, dead = await _numbers(ctx)
    if dead is not None:
        return dead
    assert firm is not None
    cur = firm["currency"]
    result: dict[str, Any] = {
        "status": "ok",
        "firm": firm["name"],
        "currency": cur,
        "monthly_earnings": firm["monthly_earnings"],
        "monthly_earnings_say": money_text(firm["monthly_earnings"], cur),
        "total_earned": firm["total_earned"],
        "total_earned_say": money_text(firm["total_earned"], cur),
        "subscribers": firm["subscribers"],
        "job_cards": firm["job_cards"],
        "signups": firm["signups"],
        **_freshness(firm),
        "say": summary_line(firm),
    }
    if firm["total_earned"] is None:
        result["total_earned_note"] = "Total earned is unknown right now; don't guess it."
    return result


async def _firm_metric(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    from jarvis.integrations.firm import METRICS, money_text

    raw = str(args.get("name") or "").strip().lower().replace("-", "_").replace(" ", "_")
    name = METRIC_ALIASES.get(raw, raw)
    if name not in METRICS:
        return {"error": f"name must be one of: {', '.join(METRICS)}"}
    _, firm, dead = await _numbers(ctx)
    if dead is not None:
        return dead
    assert firm is not None
    cur = firm["currency"]
    value = firm[name]
    result: dict[str, Any] = {"status": "ok", "firm": firm["name"], "metric": name, "value": value}
    if name in ("monthly_earnings", "total_earned"):
        result["currency"] = cur
        result["say"] = money_text(value, cur) if value is not None else None
        if value is None:
            result["note"] = "This number is unknown right now; say so, don't guess."
    elif name == "subscribers":
        result["note"] = ("individual = Individual plan subscribers; shops = Team/shop plans; shop_seats = seats "
                          "across those shops; total = individual + shops (paying accounts).")
    elif name == "job_cards":
        result["note"] = "Job cards = PDFs created. last_7_days = this week."
    elif name == "signups":
        result["note"] = "last_7_days = new signups this week."
    result.update(_freshness(firm))
    return result


TOOLS = [
    Tool(
        "firm_summary",
        "The user's business, Geonix Wrench (the firm): monthly earnings, total earned, subscribers (individual "
        "and shops, with seats), job cards / PDFs created (total and this week) and signups, with as_of and a stale "
        "flag. Use it for 'how's the firm / Geonix doing', 'the firm update', 'how much did we make'. Read-only.",
        params(),
        _firm_summary,
    ),
    Tool(
        "firm_metric",
        "One number of the user's business (Geonix Wrench): monthly_earnings (money this month), total_earned "
        "(all time), subscribers (paying: individual + shops, seats), job_cards (PDFs created, total and this "
        "week) or signups. Read-only.",
        params({"name": {"type": "string", "enum": ["monthly_earnings", "total_earned", "subscribers", "job_cards",
                                                    "signups"]}}, ["name"]),
        _firm_metric,
    ),
]
