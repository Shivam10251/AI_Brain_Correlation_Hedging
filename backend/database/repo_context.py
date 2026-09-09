"""
Equity snapshots, market state, experiences, events and engine state.
"""

import json

from ._common import _insert, _json, _rows, current_account_id, utc_now
from .connection import get_connection, write_lock


# =====================================================================
# EQUITY
# =====================================================================

def insert_equity_snapshot(snapshot):
    data = {
        "created_at": snapshot.get("created_at") or utc_now(),
        "equity": snapshot["equity"],
        "balance": snapshot.get("balance"),
        "margin": snapshot.get("margin"),
        "margin_free": snapshot.get("margin_free"),
        "floating_pnl": snapshot.get("floating_pnl"),
        "open_positions": snapshot.get("open_positions"),
        "account_login": snapshot.get("account_login"),
        "currency": snapshot.get("currency"),
        "account_id": snapshot.get("account_id") or current_account_id(),
    }

    return _insert("equity_snapshots", data)


def get_equity_snapshots(limit=500, since_iso=None):
    """
    Returned OLDEST FIRST so the frontend can plot directly.

    We take the newest `limit` rows and then reverse them, which keeps
    the curve anchored to the present as history grows.
    """

    sql = "SELECT * FROM equity_snapshots"
    params = []

    if since_iso:
        sql += " WHERE created_at >= ?"
        params.append(since_iso)

    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)

    rows = _rows(get_connection().execute(sql, params))

    return list(reversed(rows))


def get_latest_equity():
    row = get_connection().execute(
        "SELECT * FROM equity_snapshots ORDER BY id DESC LIMIT 1"
    ).fetchone()

    return dict(row) if row else None


# =====================================================================
# MARKET STATE
# =====================================================================

def insert_market_state(state):
    data = {
        "created_at": state.get("created_at") or utc_now(),
        "symbol": state["symbol"],
        "bid": state.get("bid"),
        "ask": state.get("ask"),
        "spread": state.get("spread"),
        "last_close": state.get("last_close"),
        "atr": state.get("atr"),
        "atr_pct": state.get("atr_pct"),
        "volatility_bucket": state.get("volatility_bucket"),
        "daily_trend": state.get("daily_trend"),
        "h1_trend": state.get("h1_trend"),
        "market_regime": state.get("market_regime"),
        "session": state.get("session"),
        "features_json": _json(state.get("features")),
        "account_id": state.get("account_id") or current_account_id(),
    }

    return _insert("market_states", data)


def get_latest_market_state_per_symbol():
    sql = """
        SELECT m.*
        FROM market_states m
        JOIN (
            SELECT symbol, MAX(id) AS max_id
            FROM market_states
            GROUP BY symbol
        ) latest
          ON m.id = latest.max_id
    """

    return _rows(get_connection().execute(sql))


def get_market_states(symbol, limit=200):
    return _rows(get_connection().execute(
        "SELECT * FROM market_states WHERE symbol = ? "
        "ORDER BY id DESC LIMIT ?",
        (symbol, limit),
    ))


# =====================================================================
# EXPERIENCES
# =====================================================================

def insert_experience(experience):
    data = {
        "created_at": experience.get("created_at") or utc_now(),
        "trade_id": experience.get("trade_id"),
        "symbol": experience["symbol"],
        "timeframe": experience.get("timeframe"),
        "market_regime": experience.get("market_regime"),
        "setup": experience.get("setup"),
        "direction": experience.get("direction"),
        "ai_score": experience.get("ai_score"),
        "score_bucket": experience.get("score_bucket"),
        "volatility_bucket": experience.get("volatility_bucket"),
        "session": experience.get("session"),
        "news_condition": experience.get("news_condition"),
        "entry_price": experience.get("entry_price"),
        "exit_price": experience.get("exit_price"),
        "r_multiple": experience.get("r_multiple"),
        "pnl": experience.get("pnl"),
        "outcome": experience.get("outcome"),
        "holding_minutes": experience.get("holding_minutes"),
        "lesson": experience.get("lesson"),
        "context_json": _json(experience.get("context")),
    }

    try:
        return _insert("experiences", data)
    except Exception:
        # UNIQUE(trade_id) - the reconciler already distilled this trade.
        return None


def find_experiences(symbol=None, market_regime=None, setup=None,
                     direction=None, volatility_bucket=None, limit=5):
    """
    Retrieve the most relevant historical experiences.

    Deliberately narrow: we never hand the whole database to the model.
    Filters are applied most-specific first by the caller in memory.py.
    """

    sql = "SELECT * FROM experiences WHERE 1=1"
    params = []

    for column, value in (
        ("symbol", symbol),
        ("market_regime", market_regime),
        ("setup", setup),
        ("direction", direction),
        ("volatility_bucket", volatility_bucket),
    ):
        if value:
            sql += f" AND {column} = ?"
            params.append(value)

    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)

    return _rows(get_connection().execute(sql, params))


def count_experiences():
    row = get_connection().execute(
        "SELECT COUNT(*) AS n FROM experiences"
    ).fetchone()

    return row["n"] if row else 0


# =====================================================================
# EVENTS (durable execution feed)
# =====================================================================

def insert_event(message, level="INFO", category=None, symbol=None,
                 trade_id=None, decision_id=None, data=None):
    return _insert("events", {
        "created_at": utc_now(),
        "level": level,
        "category": category,
        "symbol": symbol,
        "message": message,
        "trade_id": trade_id,
        "decision_id": decision_id,
        "data_json": _json(data),
        "account_id": current_account_id(),
    })


def get_events(limit=200, since_id=None, level=None):
    sql = "SELECT * FROM events WHERE 1=1"
    params = []

    if since_id:
        sql += " AND id > ?"
        params.append(since_id)

    if level:
        sql += " AND level = ?"
        params.append(level)

    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)

    return _rows(get_connection().execute(sql, params))


# =====================================================================
# ENGINE STATE
# =====================================================================

def set_state(key, value):
    conn = get_connection()

    with write_lock:
        conn.execute(
            "INSERT INTO engine_state (key, value, updated_at) "
            "VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
            "updated_at = excluded.updated_at",
            (key, json.dumps(value), utc_now()),
        )


def get_state(key, default=None):
    row = get_connection().execute(
        "SELECT value FROM engine_state WHERE key = ?", (key,)
    ).fetchone()

    if row is None:
        return default

    try:
        return json.loads(row["value"])
    except (TypeError, ValueError):
        return default
