"""
Data access for the trading bot.

Plain functions over sqlite3 - no ORM, no session objects. Every write
goes through the module write lock from connection.py.

This module is the single public surface. The implementation is split
by domain so no file grows unwieldy:

    _common.py       shared insert/update/serialise helpers
    repo_trading.py  decisions + trades
    repo_context.py  equity, market state, experiences, events, engine state

Call sites use `repo.<function>` regardless of which file it lives in.
"""

from ._common import utc_now

from .repo_trading import (
    count_open_trades_for_symbol,
    get_decisions,
    get_latest_decision_per_symbol,
    get_open_trades,
    get_pending_trades,
    get_trade,
    get_trade_by_client_order_id,
    get_trades,
    insert_decision,
    insert_trade_intent,
    set_trade_status,
    update_decision,
    update_trade,
)

from .repo_context import (
    count_experiences,
    find_experiences,
    get_equity_snapshots,
    get_events,
    get_latest_equity,
    get_latest_market_state_per_symbol,
    get_market_states,
    get_state,
    insert_equity_snapshot,
    insert_event,
    insert_experience,
    insert_market_state,
    set_state,
)

__all__ = [
    "utc_now",

    # decisions
    "insert_decision",
    "update_decision",
    "get_decisions",
    "get_latest_decision_per_symbol",

    # trades
    "insert_trade_intent",
    "update_trade",
    "set_trade_status",
    "get_trade",
    "get_trade_by_client_order_id",
    "get_trades",
    "get_open_trades",
    "get_pending_trades",
    "count_open_trades_for_symbol",

    # equity
    "insert_equity_snapshot",
    "get_equity_snapshots",
    "get_latest_equity",

    # market state
    "insert_market_state",
    "get_latest_market_state_per_symbol",
    "get_market_states",

    # experiences
    "insert_experience",
    "find_experiences",
    "count_experiences",

    # events
    "insert_event",
    "get_events",

    # engine state
    "set_state",
    "get_state",
]
