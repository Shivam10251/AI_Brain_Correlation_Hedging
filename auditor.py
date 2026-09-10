"""
The self-learning core.

Periodically the auditor reads the losing trades out of memory.json,
hands them to DeepSeek acting as a Chief Risk Officer, and asks one
question: what do these losses have in common?

What comes back is a set of rules — each naming a symbol, a setup, and
a confidence penalty — which ai_brain.py then injects into every future
prompt. A setup that has lost repeatedly gets marked down before it is
ever scored again.

Three deliberate constraints:

  * It only ever looks at LOSSES. Asking what winners have in common
    invites the model to invent a winning formula out of noise; asking
    what losers have in common is a narrower, answerable question.

  * Penalties are clamped to 15-30 points. An unclamped model can and
    will suggest a 90-point penalty, which silently disables a symbol
    forever on a sample of four trades.

  * Rules carry their sample size and evidence. A rule you cannot
    audit is indistinguishable from a superstition.
"""

import json
import os
from datetime import datetime, timedelta, timezone

import requests

import config
import memory_store


def _now():
    return datetime.now(timezone.utc)


# =============================================================
# The rules document
# =============================================================

EMPTY_DOCUMENT = {
    "rules": [],
    "last_audit_at": None,
    "trades_analyzed": 0,
}


def load_rules_document():
    """
    Read new_rules.json, or a well-formed empty document.

    Never raises. A corrupt rules file means the bot trades without
    learned penalties, which is the original behaviour - strictly
    better than refusing to trade at all.
    """

    if not os.path.exists(config.RULES_FILE):
        return dict(EMPTY_DOCUMENT)

    try:
        with open(config.RULES_FILE, "r", encoding="utf-8") as handle:
            document = json.load(handle)

    except (json.JSONDecodeError, OSError) as error:
        print(f"[AUDITOR] new_rules.json unreadable ({error}); ignoring")
        return dict(EMPTY_DOCUMENT)

    if not isinstance(document, dict):
        return dict(EMPTY_DOCUMENT)

    document.setdefault("rules", [])
    document.setdefault("last_audit_at", None)
    document.setdefault("trades_analyzed", 0)

    return document


def save_rules_document(document):
    with open(config.RULES_FILE, "w", encoding="utf-8") as handle:
        json.dump(document, handle, indent=2, default=str)


# =============================================================
# Cooldown
# =============================================================

def _audit_is_on_cooldown(document):
    last = document.get("last_audit_at")

    if not last:
        return False, None

    try:
        last_at = datetime.fromisoformat(str(last))
    except ValueError:
        return False, None

    if last_at.tzinfo is None:
        last_at = last_at.replace(tzinfo=timezone.utc)

    next_allowed = last_at + timedelta(hours=config.AUDIT_COOLDOWN_HOURS)

    if _now() < next_allowed:
        return True, next_allowed.isoformat()

    return False, None


def audit_is_due(new_trade_count=0):
    """
    Should an audit run?

    Two triggers: enough newly closed trades to be worth re-reading, or
    a cooldown that has simply expired with losses on the books.
    """

    document = load_rules_document()

    on_cooldown, _ = _audit_is_on_cooldown(document)

    if on_cooldown:
        return False

    if new_trade_count >= config.AUDIT_TRADE_THRESHOLD:
        return True

    return document.get("last_audit_at") is None and bool(
        memory_store.closed_trades()
    )


# =============================================================
# Building the evidence summary
# =============================================================

def _summarize_loss(record):
    """One losing trade, flattened to what the auditor can reason about."""

    context = record.get("market_context") or {}

    h1 = context.get("h1_data") or {}
    daily = context.get("daily_data") or {}

    correlated = context.get("correlated_prices") or {}

    return {
        "symbol": record.get("symbol"),
        "side": record.get("side"),
        "entry_price": record.get("entry_price"),
        "stop_loss": record.get("stop_loss"),
        "take_profit": record.get("take_profit"),
        "exit_price": record.get("exit_price"),
        "realized_pl": record.get("realized_pl"),
        "h1_relative_volume": h1.get("relative_volume"),
        "h1_atr": h1.get("atr_14"),
        "h1_rsi": h1.get("rsi_14"),
        "h1_structure": h1.get("structure"),
        "daily_relative_volume": daily.get("relative_volume"),
        "daily_atr": daily.get("atr_14"),
        "daily_rsi": daily.get("rsi_14"),
        "price_vs_daily_ema": daily.get("price_vs_ema"),
        "correlated_prices": correlated,
    }


AUDITOR_SYSTEM_PROMPT = """
You are the Chief Risk Officer and Quantitative Auditor of a systematic
hedge fund. You are reviewing a batch of LOSING trades produced by an
automated strategy.

Your job is NOT to explain each loss individually. It is to find the
RECURRING STRUCTURAL PATTERNS that produced them, so the trading model
can be penalised for entering those setups again.

Look specifically for:

  * Low-volume breakdowns and breakouts - entries taken when relative
    volume was below roughly 1.0, meaning the move lacked
    participation and was likely to fail.
  * Overextended ATR / volatility traps - entries taken when ATR was
    elevated relative to its own recent norm, where the stop sat
    inside ordinary noise.
  * Adverse cross-asset correlation - losses that cluster when the
    correlated instruments were configured a particular way, for
    example a broad dollar bid against every pair simultaneously.

Discipline:

  * A pattern needs at least 2 supporting trades. Do not generalise
    from a single loss.
  * confidence_reduction_points must be between 15 and 30. Weight it
    by how strong and how well-evidenced the pattern is.
  * If the losses share nothing structural, return an empty rules
    array. Inventing a pattern is worse than reporting none - it will
    suppress valid trades forever.

Return RAW JSON ONLY. No markdown, no code fences, no prose before or
after. Exactly this shape:

{
  "rules": [
    {
      "affected_symbol": "EURUSDm or ALL",
      "setup": "short, specific description of the setup to penalise",
      "confidence_reduction_points": 20,
      "sample_size": 3,
      "evidence": "which trades support this and what they had in common"
    }
  ]
}
""".strip()


# =============================================================
# The audit
# =============================================================

def _call_deepseek(payload_summary):
    """Ask the model for rules. Returns the parsed rules list."""

    url = f"{config.DEEPSEEK_API_BASE.rstrip('/')}/chat/completions"

    headers = {
        "Authorization": f"Bearer {config.DEEPSEEK_API_KEY}",
        "Content-Type": "application/json",
    }

    user_prompt = (
        "Here are the losing trades to audit, as JSON.\n\n"
        f"{json.dumps(payload_summary, indent=2, default=str)}\n\n"
        "Identify the recurring structural patterns and return the "
        "rules JSON."
    )

    payload = {
        "model": config.DEEPSEEK_MODEL,
        "messages": [
            {"role": "system", "content": AUDITOR_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.1,
        "response_format": {"type": "json_object"},
    }

    response = requests.post(
        url, headers=headers, json=payload, timeout=config.DEEPSEEK_TIMEOUT
    )

    response.raise_for_status()

    content = response.json()["choices"][0]["message"]["content"]

    parsed = json.loads(content)

    rules = parsed.get("rules")

    return rules if isinstance(rules, list) else []


def _sanitize_rule(rule):
    """
    Clamp and validate one proposed rule.

    Returns None for anything unusable. The model is a source of
    suggestions, not of authority - nothing it proposes reaches the
    trading prompt without passing through here.
    """

    if not isinstance(rule, dict):
        return None

    setup = str(rule.get("setup") or "").strip()

    if not setup:
        return None

    try:
        penalty = int(round(float(rule.get("confidence_reduction_points", 0))))
    except (TypeError, ValueError):
        return None

    penalty = max(
        config.MIN_CONFIDENCE_PENALTY,
        min(config.MAX_CONFIDENCE_PENALTY, penalty),
    )

    try:
        sample_size = int(rule.get("sample_size") or 0)
    except (TypeError, ValueError):
        sample_size = 0

    # One trade is an anecdote.
    if sample_size < 2:
        return None

    return {
        "affected_symbol": str(rule.get("affected_symbol") or "ALL").strip(),
        "setup": setup,
        "confidence_reduction_points": penalty,
        "sample_size": sample_size,
        "evidence": str(rule.get("evidence") or "").strip(),
        "status": "ACTIVE",
        "discovered_at": _now().isoformat(),
    }


def _merge_rules(existing, discovered):
    """
    Fold new rules into the existing set.

    A rule matching an existing symbol+setup updates it rather than
    duplicating; repeated confirmation should sharpen a rule, not
    stack three copies of it into a 60-point penalty.
    """

    merged = list(existing)

    index = {
        (r.get("affected_symbol"), r.get("setup", "").lower()): i
        for i, r in enumerate(merged)
    }

    added = 0
    updated = 0

    for rule in discovered:

        key = (rule["affected_symbol"], rule["setup"].lower())

        if key in index:
            position = index[key]

            previous = merged[position]

            rule["discovered_at"] = previous.get(
                "discovered_at", rule["discovered_at"]
            )
            rule["updated_at"] = _now().isoformat()

            merged[position] = rule

            updated += 1
        else:
            merged.append(rule)
            index[key] = len(merged) - 1
            added += 1

    return merged, added, updated


def run_audit(force=False):
    """
    Reconcile, then learn from whatever losses are on the books.

    Returns a status dict. Never raises - a failed audit must leave the
    engine trading exactly as it was.
    """

    newly_closed = memory_store.reconcile_closed_trades()

    document = load_rules_document()

    if not force:
        on_cooldown, next_allowed = _audit_is_on_cooldown(document)

        if on_cooldown:
            return {
                "status": "cooldown",
                "message": (
                    f"Audited within the last "
                    f"{config.AUDIT_COOLDOWN_HOURS}h. Next audit "
                    f"allowed at {next_allowed}."
                ),
                "newly_closed": newly_closed,
            }

    trades = memory_store.closed_trades(limit=config.AUDIT_LOOKBACK_TRADES)

    if len(trades) < config.AUDIT_MIN_TRADES:
        return {
            "status": "insufficient_trades",
            "message": (
                f"{len(trades)} closed trades on record; "
                f"{config.AUDIT_MIN_TRADES} needed before a pattern "
                f"means anything."
            ),
            "trades_analyzed": len(trades),
            "newly_closed": newly_closed,
        }

    losses = [t for t in trades if t.get("outcome") == "LOSS"]

    if not losses:
        document["last_audit_at"] = _now().isoformat()
        document["trades_analyzed"] = len(trades)

        save_rules_document(document)

        return {
            "status": "no_losses",
            "message": (
                f"No losses among the last {len(trades)} closed trades. "
                f"Nothing to learn from."
            ),
            "trades_analyzed": len(trades),
            "newly_closed": newly_closed,
        }

    summary = [_summarize_loss(record) for record in losses]

    print(f"[AUDITOR] auditing {len(losses)} losing trades...")

    try:
        raw_rules = _call_deepseek(summary)

    except requests.exceptions.RequestException as error:
        return {
            "status": "error",
            "message": f"DeepSeek request failed: {error}",
            "newly_closed": newly_closed,
        }

    except (KeyError, ValueError, json.JSONDecodeError) as error:
        return {
            "status": "error",
            "message": f"Auditor returned something unusable: {error}",
            "newly_closed": newly_closed,
        }

    discovered = [
        clean for clean in (_sanitize_rule(r) for r in raw_rules) if clean
    ]

    merged, added, updated = _merge_rules(document.get("rules", []), discovered)

    document["rules"] = merged
    document["last_audit_at"] = _now().isoformat()
    document["trades_analyzed"] = len(trades)

    save_rules_document(document)

    print(
        f"[AUDITOR] {added} new rule(s), {updated} updated, "
        f"{len(merged)} active in total"
    )

    return {
        "status": "completed",
        "message": (
            f"Audited {len(losses)} losses out of {len(trades)} closed "
            f"trades. {added} new rule(s), {updated} updated."
        ),
        "trades_analyzed": len(trades),
        "losses_analyzed": len(losses),
        "rules_added": added,
        "rules_updated": updated,
        "rules": merged,
        "newly_closed": newly_closed,
    }
