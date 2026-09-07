"""
Persistent experience memory (Phase 8).

IMPORTANT - what this is and is not:

  * This does NOT retrain or fine-tune DeepSeek. The model weights are
    untouched and DeepSeek is stateless between calls.
  * What persists is OUR record of what happened. Each finished trade
    is distilled into an `experiences` row, and the most relevant rows
    are retrieved and pasted into the next prompt as context.

That is retrieval-augmented, experience-based adaptation. Any claim
stronger than that would be false.

Retrieval is deliberately narrow - we never send the whole database.
Rows are matched on symbol, market regime, setup, direction and
volatility, then progressively broadened only if nothing matches.
"""

import config
from database import repository as repo


# ---------------------------------------------------------------------
# Bucketing
# ---------------------------------------------------------------------

def score_bucket(score):
    """Group AI scores so retrieval isn't fragmented across 0-100."""

    if score is None:
        return None

    if score >= 80:
        return "80-100"

    if score >= 60:
        return "60-79"

    if score >= 40:
        return "40-59"

    return "0-39"


# ---------------------------------------------------------------------
# Distillation: finished trade -> experience row
# ---------------------------------------------------------------------

def build_lesson(trade):
    """
    One plain sentence summarising the outcome.

    Kept factual and free of causal claims - a single trade never
    proves why it worked.
    """

    outcome = trade.get("result") or "UNKNOWN"

    r_multiple = trade.get("r_multiple")

    r_text = (
        f"{r_multiple:+.2f}R"
        if isinstance(r_multiple, (int, float))
        else "R unknown"
    )

    return (
        f"{trade.get('direction')} {trade.get('symbol')} "
        f"in a {trade.get('market_regime') or 'unclassified'} regime "
        f"on a {trade.get('setup') or 'unclassified'} setup "
        f"with AI score {trade.get('ai_score')} "
        f"resulted in {outcome} ({r_text})."
    )


def record_experience(trade, market_state=None):
    """
    Called by the reconciler when a trade closes.

    Returns the new experience id, or None if this trade already has
    one (the UNIQUE constraint on trade_id makes this idempotent).
    """

    if not config.MEMORY_ENABLED:
        return None

    holding_minutes = None

    opened_at = trade.get("opened_at")
    closed_at = trade.get("closed_at")

    if opened_at and closed_at:
        try:
            from datetime import datetime

            delta = (
                datetime.fromisoformat(closed_at)
                - datetime.fromisoformat(opened_at)
            )

            holding_minutes = round(delta.total_seconds() / 60.0, 2)

        except (TypeError, ValueError):
            holding_minutes = None

    volatility = None
    session = None

    if market_state:
        volatility = market_state.get("volatility_bucket")
        session = market_state.get("session")

    return repo.insert_experience({
        "trade_id": trade["id"],
        "symbol": trade["symbol"],
        "timeframe": trade.get("timeframe"),
        "market_regime": trade.get("market_regime"),
        "setup": trade.get("setup"),
        "direction": trade.get("direction"),

        "ai_score": trade.get("ai_score"),
        "score_bucket": score_bucket(trade.get("ai_score")),
        "volatility_bucket": volatility,
        "session": session,
        "news_condition": trade.get("news_condition"),

        "entry_price": trade.get("entry_price"),
        "exit_price": trade.get("exit_price"),
        "r_multiple": trade.get("r_multiple"),
        "pnl": trade.get("pnl"),
        "outcome": trade.get("result"),
        "holding_minutes": holding_minutes,

        "lesson": build_lesson(trade),

        "context": {
            "stop_loss": trade.get("stop_loss"),
            "take_profit": trade.get("take_profit"),
            "volume": trade.get("volume"),
            "decision_id": trade.get("decision_id"),
        },
    })


# ---------------------------------------------------------------------
# Retrieval
# ---------------------------------------------------------------------

def retrieve_relevant(symbol, features=None, limit=None):
    """
    Fetch the experiences most relevant to the current market.

    Strategy: try the most specific filter set first and progressively
    drop constraints until we have something. Never returns more than
    `limit` rows.
    """

    if not config.MEMORY_ENABLED:
        return []

    limit = limit or config.MEMORY_MAX_EXPERIENCES

    features = features or {}

    regime = features.get("market_regime")
    volatility = features.get("volatility_bucket")

    # Most specific -> least specific.
    attempts = [
        {"symbol": symbol, "market_regime": regime,
         "volatility_bucket": volatility},

        {"symbol": symbol, "market_regime": regime},

        {"symbol": symbol},

        {"market_regime": regime},

        {},
    ]

    collected = []
    seen_ids = set()

    for filters in attempts:

        # Skip filter sets whose values are all missing.
        active = {k: v for k, v in filters.items() if v}

        if filters and not active:
            continue

        rows = repo.find_experiences(limit=limit, **active)

        for row in rows:
            if row["id"] in seen_ids:
                continue

            seen_ids.add(row["id"])
            collected.append(row)

            if len(collected) >= limit:
                return collected

    return collected


def summarise(experiences):
    """
    Aggregate stats over the retrieved rows.

    Reported as raw counts, not as a claim about edge - the sample is
    almost always tiny.
    """

    if not experiences:
        return None

    wins = sum(1 for e in experiences if e.get("outcome") == "WIN")
    losses = sum(1 for e in experiences if e.get("outcome") == "LOSS")

    r_values = [
        e["r_multiple"]
        for e in experiences
        if e.get("r_multiple") is not None
    ]

    return {
        "count": len(experiences),
        "wins": wins,
        "losses": losses,
        "average_r": (
            round(sum(r_values) / len(r_values), 3) if r_values else None
        ),
    }


def build_prompt_context(experiences):
    """
    Render experiences as compact text for the DeepSeek prompt.

    Returns None when there is no history, so the prompt stays
    identical to the pre-memory version on a fresh database.
    """

    if not experiences:
        return None

    stats = summarise(experiences)

    lines = [
        "=== YOUR PAST TRADES IN COMPARABLE CONDITIONS ===",
        (
            f"Sample: {stats['count']} closed trade(s) - "
            f"{stats['wins']} win / {stats['losses']} loss"
            + (
                f", average {stats['average_r']:+.2f}R"
                if stats["average_r"] is not None else ""
            )
        ),
        (
            "This is a small sample and is NOT proof of an edge. "
            "Treat it as weak evidence, not as a rule."
        ),
        "",
    ]

    for experience in experiences:

        r_multiple = experience.get("r_multiple")

        r_text = (
            f"{r_multiple:+.2f}R"
            if isinstance(r_multiple, (int, float))
            else "n/a"
        )

        lines.append(
            f"- {experience.get('symbol')} "
            f"{experience.get('direction')} | "
            f"regime={experience.get('market_regime') or 'n/a'} | "
            f"setup={experience.get('setup') or 'n/a'} | "
            f"vol={experience.get('volatility_bucket') or 'n/a'} | "
            f"score={experience.get('ai_score')} | "
            f"outcome={experience.get('outcome') or 'n/a'} ({r_text})"
        )

    return "\n".join(lines)
