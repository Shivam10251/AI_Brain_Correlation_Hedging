"""
MT5 order execution.

Changes from the original:

  * Every attempt carries a client_order_id, stamped into the MT5
    order comment. That tag is what makes execution idempotent: after a
    crash we can ask MT5 "did this specific attempt reach you?" instead
    of guessing and re-sending. (Phase 13)

  * execute_trade no longer raises on rejection. It returns a
    structured result so the caller can persist the rejection - the old
    behaviour discarded rejected trades entirely.

  * Price / SL / TP / volume / magic maths is UNCHANGED.

Phase 1 changes:

  * Filling mode is READ from the symbol's broker profile instead of
    being hardcoded to IOC. A broker that only accepts FOK previously
    rejected every single order with no obvious cause.

  * Deviation is expressed in basis points of price and converted to
    instrument-correct points, then clamped to the broker's own
    trade_stops_level. The old fixed 20 points meant 2 pips on EURUSD
    and $0.20 on BTCUSD (Phase 0 §2.6).

Both fall back to the previous behaviour when no broker profile has
been captured yet, so the module still works before first startup.
"""

import uuid

import MetaTrader5 as mt5

from backend import config
from backend.config import SL_PERCENT, TP_PERCENT


# MT5 truncates order comments (31 chars on most builds), so the tag is
# kept short: "AIQ-" + 12 hex characters = 16 characters.
COMMENT_PREFIX = "AIQ-"

SHORT_ID_LENGTH = 12


def new_client_order_id():
    """Unique id for one execution attempt."""

    return uuid.uuid4().hex


def _symbol_profile(symbol):
    """
    The stored broker specification for this symbol, or None.

    Read lazily and defensively: execution must never fail because the
    profile table is missing or the database is momentarily locked.
    """

    try:
        from backend.core.runtime import bot_state
        from backend.database import repo_meta

        account_id = bot_state.get("account_id")

        if account_id is None:
            return None

        return repo_meta.get_symbol_profile(account_id, symbol)

    except Exception:                               # noqa: BLE001
        return None


def resolve_execution_params(symbol, price, symbol_info=None):
    """
    Decide filling mode and deviation for one order.

    Prefers the stored broker profile; falls back to reading
    symbol_info live; falls back again to the legacy constants. Returns
    (type_filling, deviation_points, source) where `source` records
    which path was taken, for the audit trail.
    """

    from backend.market.broker_profile import (
        deviation_points,
        resolve_filling_mode,
    )

    profile = _symbol_profile(symbol)

    if profile and profile.get("filling_order_type") is not None:
        return (
            int(profile["filling_order_type"]),
            deviation_points(profile, price),
            "profile",
        )

    # No stored profile yet - read the terminal directly rather than
    # guessing IOC.
    info = symbol_info if symbol_info is not None else mt5.symbol_info(symbol)

    if info is not None:
        _, order_type = resolve_filling_mode(getattr(info, "filling_mode", 0))

        live = {
            "point": getattr(info, "point", None),
            "trade_stops_level": getattr(info, "trade_stops_level", 0),
        }

        return int(order_type), deviation_points(live, price), "symbol_info"

    return mt5.ORDER_FILLING_IOC, config.DEVIATION, "fallback"


def comment_for(client_order_id):
    """The MT5 comment tag derived from a client order id."""

    return f"{COMMENT_PREFIX}{client_order_id[:SHORT_ID_LENGTH]}"


def _result_payload(result):
    """MT5's result object as a plain dict, for the audit trail."""

    if result is None:
        return None

    return {
        "retcode": getattr(result, "retcode", None),
        "comment": getattr(result, "comment", None),
        "order": getattr(result, "order", None),
        "deal": getattr(result, "deal", None),
        "volume": getattr(result, "volume", None),
        "price": getattr(result, "price", None),
        "request_id": getattr(result, "request_id", None),
    }


def find_deal_by_client_order_id(client_order_id, lookback_seconds=3600):
    """
    Ask MT5 whether an attempt actually reached the server.

    Used by the reconciler to resolve PENDING rows orphaned by a crash
    between order_send and the database write. Returns a dict with the
    deal details, or None if MT5 never saw it.
    """

    from datetime import datetime, timedelta, timezone

    tag = comment_for(client_order_id)

    now = datetime.now(timezone.utc)

    deals = mt5.history_deals_get(
        now - timedelta(seconds=lookback_seconds),
        now + timedelta(seconds=60),
    )

    if not deals:
        return None

    for deal in deals:

        if getattr(deal, "comment", "") and tag in deal.comment:

            return {
                "deal_ticket": deal.ticket,
                "order_ticket": deal.order,
                "position_ticket": getattr(deal, "position_id", None),
                "entry_price": deal.price,
                "volume": deal.volume,
                "symbol": deal.symbol,
                "profit": getattr(deal, "profit", 0.0),
            }

    return None


def _resolve_position_ticket(result):
    """
    Map the filled deal back to its position id.

    result.order is an ORDER ticket; positions are keyed by position id,
    and the two are not always the same number.
    """

    deal_ticket = getattr(result, "deal", None)

    if deal_ticket:
        deals = mt5.history_deals_get(ticket=deal_ticket)

        if deals:
            position_id = getattr(deals[0], "position_id", None)

            if position_id:
                return position_id

    # Fall back to the order ticket, which matches for simple fills.
    return getattr(result, "order", None)


def execute_trade(symbol, signal, client_order_id=None, volume=None):
    """
    Execute a market trade through MetaTrader 5.

    Returns a dict:
        {
            "status": "EXECUTED" | "REJECTED" | "FAILED" | "SKIPPED",
            "client_order_id": str,
            ...
        }

    Never raises for a trading outcome - a rejection is data, not an
    exception.
    """

    signal = signal.upper()

    client_order_id = client_order_id or new_client_order_id()

    volume = config.LOT_SIZE if volume is None else volume

    if signal == "HOLD":
        return {
            "status": "SKIPPED",
            "client_order_id": client_order_id,
            "reason": "AI signal is HOLD",
        }

    if signal not in {"BUY", "SELL"}:
        return {
            "status": "FAILED",
            "client_order_id": client_order_id,
            "reason": f"Invalid signal: {signal}",
        }

    # ---------------------------------------------------------
    # Symbol information
    # ---------------------------------------------------------

    symbol_info = mt5.symbol_info(symbol)

    if symbol_info is None:
        return {
            "status": "FAILED",
            "client_order_id": client_order_id,
            "reason": f"Symbol not found: {symbol} | {mt5.last_error()}",
        }

    if not symbol_info.visible:
        if not mt5.symbol_select(symbol, True):
            return {
                "status": "FAILED",
                "client_order_id": client_order_id,
                "reason": f"Could not select symbol: {symbol}",
            }

        symbol_info = mt5.symbol_info(symbol)

    # ---------------------------------------------------------
    # Current price
    # ---------------------------------------------------------

    tick = mt5.symbol_info_tick(symbol)

    if tick is None:
        return {
            "status": "FAILED",
            "client_order_id": client_order_id,
            "reason": f"No tick data for {symbol} | {mt5.last_error()}",
        }

    # ---------------------------------------------------------
    # Price / SL / TP  (unchanged maths)
    # ---------------------------------------------------------

    if signal == "BUY":

        order_type = mt5.ORDER_TYPE_BUY
        price = tick.ask

        sl = price * (1 - SL_PERCENT)
        tp = price * (1 + TP_PERCENT)

    else:

        order_type = mt5.ORDER_TYPE_SELL
        price = tick.bid

        sl = price * (1 + SL_PERCENT)
        tp = price * (1 - TP_PERCENT)

    # ---------------------------------------------------------
    # CRITICAL: use broker's digits
    # ---------------------------------------------------------

    digits = symbol_info.digits

    price = round(price, digits)
    sl = round(sl, digits)
    tp = round(tp, digits)

    order_comment = comment_for(client_order_id)

    # ---------------------------------------------------------
    # Filling mode + deviation, from the broker's own numbers
    # ---------------------------------------------------------

    type_filling, deviation, params_source = resolve_execution_params(
        symbol, price, symbol_info
    )

    # ---------------------------------------------------------
    # Print what we're attempting
    # ---------------------------------------------------------

    print("\n========================================")
    print("ATTEMPTING MT5 TRADE")
    print("Symbol:", symbol)
    print("Signal:", signal)
    print("Price:", price)
    print("SL:", sl)
    print("TP:", tp)
    print("Digits:", digits)
    print("Volume:", volume)
    print("Filling:", type_filling, f"({params_source})")
    print("Deviation:", deviation, "points")
    print("Tag:", order_comment)
    print("========================================")

    # ---------------------------------------------------------
    # Trade request
    # ---------------------------------------------------------

    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": volume,
        "type": order_type,
        "price": price,
        "sl": sl,
        "tp": tp,
        "deviation": deviation,
        "magic": config.MAGIC_NUMBER,
        "comment": order_comment,
        "type_time": mt5.ORDER_TIME_GTC,

        # Chosen from symbol_info.filling_mode - never hardcoded.
        "type_filling": type_filling,
    }

    # Everything the caller needs to persist the attempt, whatever
    # happens next.
    attempt = {
        "client_order_id": client_order_id,
        "symbol": symbol,
        "direction": signal,
        "volume": volume,
        "requested_price": price,
        "stop_loss": sl,
        "take_profit": tp,
        "magic": config.MAGIC_NUMBER,
        "mt5_comment": order_comment,
        "type_filling": type_filling,
        "deviation": deviation,
        "params_source": params_source,
    }

    # ---------------------------------------------------------
    # Send order
    # ---------------------------------------------------------

    try:
        result = mt5.order_send(request)
    except Exception as error:                      # noqa: BLE001
        return {
            **attempt,
            "status": "FAILED",
            "reason": f"order_send raised: {error}",
        }

    if result is None:

        # Ambiguous: the order may or may not have reached the server.
        # The reconciler resolves this by searching MT5 history for our
        # comment tag rather than re-sending.
        return {
            **attempt,
            "status": "FAILED",
            "reason": (
                f"order_send returned None | "
                f"MT5 error: {mt5.last_error()}"
            ),
            "ambiguous": True,
        }

    # ---------------------------------------------------------
    # ALWAYS inspect the MT5 result
    # ---------------------------------------------------------

    print("\n========================================")
    print("MT5 ORDER RESULT")
    print("Retcode:", result.retcode)
    print("Comment:", result.comment)
    print("Order:", result.order)
    print("Deal:", result.deal)
    print("Volume:", result.volume)
    print("Price:", result.price)
    print("========================================\n")

    payload = _result_payload(result)

    # ---------------------------------------------------------
    # Successful market execution
    # ---------------------------------------------------------

    successful_codes = {
        mt5.TRADE_RETCODE_DONE,
        mt5.TRADE_RETCODE_DONE_PARTIAL,
    }

    if result.retcode not in successful_codes:

        return {
            **attempt,
            "status": "REJECTED",
            "reason": (
                f"MT5 REJECTED TRADE | "
                f"retcode={result.retcode} | "
                f"comment={result.comment}"
            ),
            "mt5_retcode": result.retcode,
            "raw_result": payload,
        }

    return {
        **attempt,
        "status": "EXECUTED",
        "mt5_retcode": result.retcode,
        "order_ticket": result.order,
        "deal_ticket": result.deal,
        "position_ticket": _resolve_position_ticket(result),
        "entry_price": result.price or price,
        "filled_volume": result.volume,
        "raw_result": payload,
    }
