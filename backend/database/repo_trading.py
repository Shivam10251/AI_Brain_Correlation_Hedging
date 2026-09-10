"""
Decisions and trades.
"""

from ._common import (
    _insert, _json, _rows, _update, current_account_id, utc_now,
)
from .connection import get_connection, write_lock


# =====================================================================
# DECISIONS
# =====================================================================

DECISION_COLUMNS = (
    "created_at", "cycle_id", "symbol", "timeframe", "model", "status",
    "daily_trend", "h1_trend", "momentum", "market_structure",
    "market_regime", "setup", "ai_score", "ai_signal", "final_decision",
    "override_reason", "reasoning", "daily_analysis", "h1_analysis",
    "features_json", "news_json", "experience_ids", "raw_response",
    "latency_ms", "error",
    # Phase 1 - reproducibility
    "prompt_hash", "temperature", "strategy_version_id",
    # Phase 2 - account attribution
    "account_id",
    # Phase 3 - risk verdict
    "risk_checks_json", "risk_profile_id",
    # Phase 4 - feature provenance
    "feature_version", "bar_time_utc",
    "trade_id",
)


def insert_decision(decision):
    """
    Persist one AI analysis. `decision` is the dict returned by
    ai_brain.get_ai_decision plus engine-side fields.
    """

    data = {
        "created_at": decision.get("created_at") or utc_now(),
        "cycle_id": decision.get("cycle_id"),
        "symbol": decision.get("symbol"),
        "timeframe": decision.get("timeframe"),
        "model": decision.get("model"),
        "status": decision.get("status", "ok"),

        "daily_trend": decision.get("daily_trend"),
        "h1_trend": decision.get("h1_trend"),
        "momentum": decision.get("momentum"),
        "market_structure": decision.get("market_structure"),
        "market_regime": decision.get("market_regime"),
        "setup": decision.get("setup"),
        "ai_score": decision.get("ai_score"),

        "ai_signal": decision.get("ai_signal"),
        "final_decision": decision.get("final_decision"),
        "override_reason": decision.get("override_reason"),

        "reasoning": decision.get("reasoning"),
        "daily_analysis": decision.get("daily_analysis"),
        "h1_analysis": decision.get("h1_analysis"),

        "features_json": _json(decision.get("features")),
        "news_json": _json(decision.get("news")),
        "experience_ids": _json(decision.get("experience_ids")),
        "raw_response": decision.get("raw_response"),
        "latency_ms": decision.get("latency_ms"),
        "error": decision.get("error"),

        # Phase 1: which exact strategy produced this row. Present on
        # failed decisions too, so a calibration study can see the
        # denominator and not just the successes.
        "prompt_hash": decision.get("prompt_hash"),
        "temperature": decision.get("temperature"),
        "strategy_version_id": decision.get("strategy_version_id"),
        "account_id": decision.get("account_id") or current_account_id(),

        # Phase 3: the full risk verdict, so a refusal explains itself
        # from the row without needing the logs.
        "risk_checks_json": _json(decision.get("risk_checks")),
        "risk_profile_id": decision.get("risk_profile_id"),

        # Phase 4: feature provenance
        "feature_version": decision.get("feature_version"),
        "bar_time_utc": decision.get("bar_time_utc"),

        "trade_id": decision.get("trade_id"),
    }

    return _insert("decisions", data)


def update_decision(decision_id, **fields):
    return _update("decisions", decision_id, fields)


def get_decisions(limit=100, symbol=None, since_id=None):
    """Most recent decisions first."""

    sql = "SELECT * FROM decisions WHERE 1=1"
    params = []

    if symbol:
        sql += " AND symbol = ?"
        params.append(symbol)

    if since_id:
        sql += " AND id > ?"
        params.append(since_id)

    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)

    return _rows(get_connection().execute(sql, params))


def get_latest_decision_per_symbol():
    """
    The newest decision for each symbol.

    This is what the dashboard's AI panel should render - the previous
    implementation kept a single global slot that every symbol in the
    cycle overwrote.
    """

    sql = """
        SELECT d.*
        FROM decisions d
        JOIN (
            SELECT symbol, MAX(id) AS max_id
            FROM decisions
            GROUP BY symbol
        ) latest
          ON d.id = latest.max_id
        ORDER BY d.created_at DESC
    """

    return _rows(get_connection().execute(sql))


# =====================================================================
# TRADES
# =====================================================================

def insert_trade_intent(trade):
    """
    Write the PENDING row BEFORE the order reaches MT5.

    If the process dies mid-send, this row is what lets the reconciler
    find the orphan instead of silently double-trading on restart.
    """

    data = {
        "client_order_id": trade["client_order_id"],
        "created_at": trade.get("created_at") or utc_now(),
        "symbol": trade["symbol"],
        "direction": trade["direction"],
        "volume": trade.get("volume"),
        "requested_price": trade.get("requested_price"),
        "stop_loss": trade.get("stop_loss"),
        "take_profit": trade.get("take_profit"),

        # Phase 2: money at risk, computed BEFORE the order is sent.
        "risk_amount": trade.get("risk_amount"),
        "stop_distance_price": trade.get("stop_distance_price"),
        "stop_distance_effective": trade.get("stop_distance_effective"),
        "spread_at_entry": trade.get("spread_at_entry"),
        "stop_model": trade.get("stop_model"),

        "account_id": trade.get("account_id") or current_account_id(),
        "reason": trade.get("reason"),
        "decision_id": trade.get("decision_id"),
        "execution_status": "PENDING",
        "magic": trade.get("magic"),
        "market_regime": trade.get("market_regime"),
        "setup": trade.get("setup"),
        "ai_score": trade.get("ai_score"),
        "timeframe": trade.get("timeframe"),
        "news_condition": trade.get("news_condition"),
    }

    return _insert("trades", data)


def update_trade(trade_id, **fields):
    if "raw_result" in fields and not isinstance(
        fields["raw_result"], (str, type(None))
    ):
        fields["raw_result"] = _json(fields["raw_result"])

    return _update("trades", trade_id, fields)


def set_trade_status(trade_id, status):
    """Status must be settable to any value, including back to PENDING."""

    conn = get_connection()

    with write_lock:
        conn.execute(
            "UPDATE trades SET execution_status = ? WHERE id = ?",
            (status, trade_id),
        )


def get_trade(trade_id):
    row = get_connection().execute(
        "SELECT * FROM trades WHERE id = ?", (trade_id,)
    ).fetchone()

    return dict(row) if row else None


def get_trade_by_client_order_id(client_order_id):
    row = get_connection().execute(
        "SELECT * FROM trades WHERE client_order_id = ?", (client_order_id,)
    ).fetchone()

    return dict(row) if row else None


def get_trades(limit=200, symbol=None, since_id=None, statuses=None):
    sql = "SELECT * FROM trades WHERE 1=1"
    params = []

    if symbol:
        sql += " AND symbol = ?"
        params.append(symbol)

    if since_id:
        sql += " AND id > ?"
        params.append(since_id)

    if statuses:
        sql += f" AND execution_status IN ({','.join('?' * len(statuses))})"
        params.extend(statuses)

    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)

    return _rows(get_connection().execute(sql, params))


def get_open_trades():
    """Trades we believe are still live in MT5."""

    return _rows(get_connection().execute(
        "SELECT * FROM trades "
        "WHERE execution_status = 'EXECUTED' AND closed_at IS NULL "
        "ORDER BY id ASC"
    ))


def get_open_position_tickets():
    """
    Every position ticket the database still considers open.

    Used by the reconciler's adoption pass. It must not be bounded by
    a row limit: the previous implementation scanned only the most
    recent 1,000 trades, so an older still-open position dropped out
    of the known set and was re-adopted on every cycle (Phase 0 §2.7).
    """

    return _rows(get_connection().execute(
        "SELECT id, symbol, position_ticket FROM trades "
        "WHERE position_ticket IS NOT NULL AND closed_at IS NULL"
    ))


def get_pending_trades(older_than_iso=None):
    """
    PENDING rows are execution attempts with unknown outcome - either
    in flight right now, or orphaned by a crash.
    """

    sql = "SELECT * FROM trades WHERE execution_status = 'PENDING'"
    params = []

    if older_than_iso:
        sql += " AND created_at < ?"
        params.append(older_than_iso)

    sql += " ORDER BY id ASC"

    return _rows(get_connection().execute(sql, params))


def count_open_trades_for_symbol(symbol):
    row = get_connection().execute(
        "SELECT COUNT(*) AS n FROM trades "
        "WHERE symbol = ? AND execution_status = 'EXECUTED' "
        "AND closed_at IS NULL",
        (symbol,),
    ).fetchone()

    return row["n"] if row else 0
