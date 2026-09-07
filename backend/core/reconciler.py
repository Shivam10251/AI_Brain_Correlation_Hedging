"""
Reconciliation between the database and MetaTrader 5.

MT5 IS THE SOURCE OF TRUTH for account and position state. This module
never writes to MT5 - it only reads MT5 and corrects our mirror, in
three passes:

  1. resolve_pending_trades       - attempts with an unknown outcome
                                    (crash between order_send and the
                                    database write)
  2. close_finished_trades        - positions MT5 no longer has open,
                                    settled from deal history, which
                                    also produces the AI experience
  3. adopt_untracked_positions    - live positions carrying our magic
                                    number that have no database row

Pass 1 is what prevents duplicate orders after a restart: an ambiguous
attempt is resolved by ASKING MT5, never by re-sending.
"""

from datetime import datetime, timedelta, timezone

import MetaTrader5 as mt5

from backend import config
from backend.ai import memory
from backend.database import repository as repo
from backend.market.execution import comment_for


DEAL_ENTRY_IN = 0
DEAL_ENTRY_OUT = 1


def _iso(unix_seconds):
    if not unix_seconds:
        return None

    return datetime.fromtimestamp(
        unix_seconds, tz=timezone.utc
    ).isoformat()


def _classify(pnl):
    if pnl is None:
        return None

    if pnl > 0:
        return "WIN"

    if pnl < 0:
        return "LOSS"

    return "BREAKEVEN"


def _r_multiple(trade, exit_price):
    """
    Realised R, measured in price terms against the original stop.

    None when we have no stop distance to measure against.
    """

    entry = trade.get("entry_price")
    stop = trade.get("stop_loss")

    if entry is None or stop is None or exit_price is None:
        return None

    risk = abs(entry - stop)

    if risk == 0:
        return None

    if trade.get("direction") == "BUY":
        return round((exit_price - entry) / risk, 4)

    return round((entry - exit_price) / risk, 4)


# =====================================================================
# PASS 1: PENDING attempts with an unknown outcome
# =====================================================================

def resolve_pending_trades():
    """
    Resolve PENDING rows by asking MT5 whether the attempt landed.

    A PENDING row means: we wrote our intent, then either the process
    died or order_send returned nothing. Re-sending would risk a
    duplicate, so we search MT5's deal history for our comment tag.
    """

    cutoff = (
        datetime.now(timezone.utc)
        - timedelta(seconds=config.PENDING_TRADE_TIMEOUT_SECONDS)
    ).isoformat()

    pending = repo.get_pending_trades(older_than_iso=cutoff)

    resolved = 0

    for trade in pending:

        deal = _find_deal_for_trade(trade)

        if deal:
            # The order DID reach MT5. Adopt it instead of re-sending.
            repo.update_trade(
                trade["id"],
                execution_status="EXECUTED",
                opened_at=deal["opened_at"],
                entry_price=deal["entry_price"],
                volume=deal["volume"],
                order_ticket=deal["order_ticket"],
                deal_ticket=deal["deal_ticket"],
                position_ticket=deal["position_ticket"],
            )

            repo.insert_event(
                f"Recovered orphaned order for {trade['symbol']} "
                f"from MT5 history (position {deal['position_ticket']})",
                level="WARN",
                category="RECONCILE",
                symbol=trade["symbol"],
                trade_id=trade["id"],
            )

        else:
            # MT5 never saw it. Safe to mark as failed.
            repo.update_trade(
                trade["id"],
                execution_status="FAILED",
                reason=(
                    (trade.get("reason") or "")
                    + " | unresolved after restart; MT5 has no matching deal"
                ).strip(" |"),
            )

            repo.insert_event(
                f"Pending order for {trade['symbol']} never reached MT5 "
                f"- marked FAILED",
                level="WARN",
                category="RECONCILE",
                symbol=trade["symbol"],
                trade_id=trade["id"],
            )

        resolved += 1

    return resolved


def _find_deal_for_trade(trade):
    """Locate the opening deal for a trade by its comment tag."""

    tag = comment_for(trade["client_order_id"])

    # Search generously around the attempt time.
    try:
        created = datetime.fromisoformat(trade["created_at"])
    except (TypeError, ValueError):
        created = datetime.now(timezone.utc) - timedelta(days=1)

    deals = mt5.history_deals_get(
        created - timedelta(minutes=10),
        datetime.now(timezone.utc) + timedelta(minutes=1),
    )

    if not deals:
        return None

    for deal in deals:

        comment = getattr(deal, "comment", "") or ""

        if tag not in comment:
            continue

        if getattr(deal, "entry", None) != DEAL_ENTRY_IN:
            continue

        return {
            "deal_ticket": deal.ticket,
            "order_ticket": deal.order,
            "position_ticket": getattr(deal, "position_id", None) or deal.order,
            "entry_price": deal.price,
            "volume": deal.volume,
            "opened_at": _iso(getattr(deal, "time", None)),
        }

    return None


# =====================================================================
# PASS 2: close trades MT5 no longer holds open
# =====================================================================

def close_finished_trades():
    """
    Settle every open trade whose position is gone from MT5.

    P&L, exit price and close time all come from MT5's deal history -
    we never estimate them.
    """

    open_trades = repo.get_open_trades()

    closed = 0

    for trade in open_trades:

        position_ticket = trade.get("position_ticket")

        if not position_ticket:
            continue

        positions = mt5.positions_get(ticket=position_ticket)

        if positions:
            # Still open in MT5 - nothing to settle.
            continue

        settlement = _settle_from_history(position_ticket)

        if settlement is None:
            # Position is gone but history isn't available yet; try
            # again on the next pass rather than guessing.
            continue

        exit_price = settlement["exit_price"]

        r_multiple = _r_multiple(trade, exit_price)

        repo.update_trade(
            trade["id"],
            execution_status="CLOSED",
            closed_at=settlement["closed_at"],
            exit_price=exit_price,
            pnl=settlement["pnl"],
            commission=settlement["commission"],
            swap=settlement["swap"],
            result=_classify(settlement["pnl"]),
            r_multiple=r_multiple,
        )

        settled = repo.get_trade(trade["id"])

        repo.insert_event(
            f"{settled['symbol']} {settled['direction']} closed "
            f"{settled['result']} "
            f"P&L {settlement['pnl']:+.2f}"
            + (f" ({r_multiple:+.2f}R)" if r_multiple is not None else ""),
            level="INFO",
            category="TRADE_CLOSED",
            symbol=settled["symbol"],
            trade_id=settled["id"],
        )

        # ------------------------------------------------------
        # Distil the finished trade into an AI experience.
        # ------------------------------------------------------
        market_states = repo.get_latest_market_state_per_symbol()

        market_state = next(
            (m for m in market_states if m["symbol"] == settled["symbol"]),
            None,
        )

        memory.record_experience(settled, market_state)

        closed += 1

    return closed


def _settle_from_history(position_ticket):
    """Aggregate every deal belonging to one position."""

    deals = mt5.history_deals_get(position=position_ticket)

    if not deals:
        return None

    total_profit = 0.0
    total_commission = 0.0
    total_swap = 0.0

    exit_price = None
    closed_at = None

    for deal in deals:

        total_profit += getattr(deal, "profit", 0.0) or 0.0
        total_commission += getattr(deal, "commission", 0.0) or 0.0
        total_swap += getattr(deal, "swap", 0.0) or 0.0

        if getattr(deal, "entry", None) == DEAL_ENTRY_OUT:
            exit_price = deal.price
            closed_at = _iso(getattr(deal, "time", None))

    if exit_price is None:
        return None

    return {
        # Net of costs - this is the number that matters.
        "pnl": round(total_profit + total_commission + total_swap, 2),
        "commission": round(total_commission, 2),
        "swap": round(total_swap, 2),
        "exit_price": exit_price,
        "closed_at": closed_at,
    }


# =====================================================================
# PASS 3: adopt live positions we have no row for
# =====================================================================

def adopt_untracked_positions():
    """
    Insert a row for any live position carrying our magic number that
    the database doesn't know about.

    Without this, a position opened by a previous process would be
    invisible to the dashboard and to the per-symbol position limit.
    """

    positions = mt5.positions_get()

    if not positions:
        return 0

    known = {
        trade["position_ticket"]
        for trade in repo.get_trades(limit=1000)
        if trade.get("position_ticket")
    }

    adopted = 0

    for position in positions:

        if position.magic != config.MAGIC_NUMBER:
            # Not ours - a manual trade. Leave it alone.
            continue

        if position.ticket in known:
            continue

        direction = "BUY" if position.type == mt5.POSITION_TYPE_BUY else "SELL"

        trade_id = repo.insert_trade_intent({
            "client_order_id": f"adopted-{position.ticket}",
            "symbol": position.symbol,
            "direction": direction,
            "volume": position.volume,
            "requested_price": position.price_open,
            "stop_loss": position.sl or None,
            "take_profit": position.tp or None,
            "reason": "Adopted from MT5 on startup (no local record)",
            "magic": position.magic,
        })

        repo.update_trade(
            trade_id,
            execution_status="EXECUTED",
            entry_price=position.price_open,
            position_ticket=position.ticket,
            opened_at=_iso(getattr(position, "time", None)),
        )

        repo.insert_event(
            f"Adopted untracked MT5 position {position.ticket} "
            f"({position.symbol} {direction})",
            level="WARN",
            category="RECONCILE",
            symbol=position.symbol,
            trade_id=trade_id,
        )

        adopted += 1

    return adopted


# =====================================================================
# ENTRY POINT
# =====================================================================

def run_full_reconciliation():
    """
    All three passes, in dependency order.

    Each pass is isolated so one MT5 hiccup cannot abort the others.
    """

    summary = {"resolved": 0, "closed": 0, "adopted": 0, "errors": []}

    for key, function in (
        ("resolved", resolve_pending_trades),
        ("closed", close_finished_trades),
        ("adopted", adopt_untracked_positions),
    ):
        try:
            summary[key] = function()
        except Exception as error:                  # noqa: BLE001
            summary["errors"].append(f"{key}: {error}")
            print(f"[RECONCILE] {key} failed: {error}")

    return summary
