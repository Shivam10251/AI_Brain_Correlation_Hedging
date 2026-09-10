"""
Trade memory: what was traded, in what market, and how it ended.

memory.json is the auditor's entire evidence base. A record is written
the moment an order is confirmed, carrying the full market context that
existed at entry - not just the price, but the volatility, the
participation, and what every other instrument was doing.

That context is the point. Knowing a trade lost tells you nothing you
can act on; knowing it lost on 0.4x relative volume, for the fifth
time, is a rule.

Records move through two states:

    CONFIRMED   the order filled; outcome unknown
    CLOSED      MT5's deal history says how it ended
"""

import json
import os
from datetime import datetime, timezone

import MetaTrader5 as mt5

import config


# MT5 deal entry codes. 1 (DEAL_ENTRY_OUT) is the leg that closes a
# position; 0 is the one that opens it.
DEAL_ENTRY_OUT = 1


def _now():
    return datetime.now(timezone.utc).isoformat()


# =============================================================
# Reading and writing
# =============================================================

def load_trade_memory():
    """
    Every stored record, oldest first.

    A missing or corrupt file returns an empty list rather than
    raising: losing the audit trail must not also stop the engine
    trading.
    """

    if not os.path.exists(config.MEMORY_FILE):
        return []

    try:
        with open(config.MEMORY_FILE, "r", encoding="utf-8") as handle:
            data = json.load(handle)

    except (json.JSONDecodeError, OSError) as error:
        print(f"[LEARNING] memory.json unreadable ({error}); starting empty")
        return []

    return data if isinstance(data, list) else []


def _save_trade_memory(records):
    with open(config.MEMORY_FILE, "w", encoding="utf-8") as handle:
        json.dump(records, handle, indent=2, default=str)


def append_trade_memory(record):
    """
    Store one confirmed fill with its full market context.

    Called immediately after the broker confirms, so a crash on the
    next line still leaves the trade recorded.
    """

    records = load_trade_memory()

    record.setdefault("status", "CONFIRMED")
    record.setdefault("recorded_at", _now())

    records.append(record)

    _save_trade_memory(records)

    print(
        f"[LEARNING] recorded {record.get('symbol')} "
        f"{record.get('side')} deal={record.get('deal')}"
    )

    return record


# =============================================================
# Reconciliation
# =============================================================

def _find_exit_deal(record):
    """
    Ask MT5 whether this position has closed.

    Looks the position up by its position ticket and takes the exit
    leg. MT5 is the source of truth here - the bot never assumes an
    outcome it has not been told.
    """

    position_id = (
        record.get("position")
        or record.get("order")
        or record.get("deal")
    )

    if not position_id:
        return None

    try:
        deals = mt5.history_deals_get(position=int(position_id))
    except Exception as error:                      # noqa: BLE001
        print(f"[LEARNING] deal lookup failed for {position_id}: {error}")
        return None

    if not deals:
        return None

    for deal in deals:
        if getattr(deal, "entry", None) == DEAL_ENTRY_OUT:
            return deal

    return None


def _classify(profit):
    if profit > 0:
        return "WIN"

    if profit < 0:
        return "LOSS"

    return "BREAKEVEN"


def reconcile_closed_trades():
    """
    Turn CONFIRMED records into CLOSED ones using MT5 deal history.

    Returns the number newly reconciled. That count is what triggers an
    audit: new evidence, not merely elapsed time, is what makes another
    audit worth running.
    """

    records = load_trade_memory()

    if not records:
        return 0

    reconciled = 0

    for record in records:

        if record.get("status") != "CONFIRMED":
            continue

        if record.get("exit_deal"):
            continue

        exit_deal = _find_exit_deal(record)

        if exit_deal is None:
            continue

        profit = float(getattr(exit_deal, "profit", 0.0) or 0.0)
        commission = float(getattr(exit_deal, "commission", 0.0) or 0.0)
        swap = float(getattr(exit_deal, "swap", 0.0) or 0.0)

        # Net, not gross. A trade that made $2 gross and paid $3 in
        # commission is a loss, and the auditor must see it as one.
        net = round(profit + commission + swap, 2)

        record.update({
            "status": "CLOSED",
            "exit_deal": int(exit_deal.ticket),
            "exit_price": float(exit_deal.price),
            "exit_time": datetime.fromtimestamp(
                int(exit_deal.time), tz=timezone.utc
            ).isoformat(),
            "gross_profit": round(profit, 2),
            "commission": round(commission, 2),
            "swap": round(swap, 2),
            "realized_pl": net,
            "outcome": _classify(net),
            "reconciled_at": _now(),
        })

        reconciled += 1

        print(
            f"[LEARNING] closed {record.get('symbol')} "
            f"{record.get('side')} -> {record['outcome']} "
            f"{net:+.2f}"
        )

    if reconciled:
        _save_trade_memory(records)

    return reconciled


def closed_trades(limit=None):
    """Closed records with a realised P/L, newest last."""

    closed = [
        record for record in load_trade_memory()
        if record.get("status") == "CLOSED"
        and record.get("realized_pl") is not None
    ]

    if limit:
        return closed[-limit:]

    return closed
