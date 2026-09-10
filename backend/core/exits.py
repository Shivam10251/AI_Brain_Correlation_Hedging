"""
Exit manager (Phase 5).

Before this, the ONLY way a position ever closed was the broker hitting
its SL or TP (Phase 0 §A8). There was no close, no flatten, no reversal
handling - and when the AI flipped direction the engine opened the
opposite position, leaving both legs open. That is hedging, which both
target firms prohibit, and it was Phase 0 weakness #5.

Three exits live here:

  reversal   the AI flips direction with conviction while a position is
             open -> close it. Do NOT open the other side in the same
             cycle: the close has to settle and be reconciled first, so
             the new entry is judged on a clean book by the risk
             engine rather than racing it.

  flatten    a profile-driven window (weekend, session end) -> close
             everything. Runs from the loop on a clock, independent of
             whether any symbol produced a new bar.

  kill       the kill switch, with flatten requested -> close
             everything, now.

Every exit goes through execution.close_position, so all three inherit
the same comment-tag idempotency that makes opens crash-safe.
"""

from datetime import datetime, timedelta, timezone

from backend import config
from backend.database import repository as repo
from backend.market.execution import close_position, new_client_order_id


# Exit reasons recorded on the trade row.
SL = "SL"
TP = "TP"
REVERSAL = "REVERSAL"
FLATTEN = "FLATTEN"
KILL = "KILL"
MANUAL = "MANUAL"


# =====================================================================
# Closing one position
# =====================================================================

def close_trade(trade, reason, note=None):
    """
    Close one tracked position and record why.

    The exit reason is written BEFORE the close is sent, so a crash
    mid-flight still leaves the intent visible - the reconciler can
    then attribute the settled trade correctly instead of guessing.
    """

    ticket = trade.get("position_ticket")

    if not ticket:
        return {"status": "SKIPPED", "reason": "trade has no position ticket"}

    client_order_id = new_client_order_id()

    repo.update_trade(
        trade["id"],
        exit_reason=reason,
        exit_client_order_id=client_order_id,
    )

    result = close_position(
        ticket, client_order_id=client_order_id, reason=reason
    )

    status = result.get("status")

    if status in {"CLOSED", "ALREADY_CLOSED"}:
        repo.insert_event(
            f"{trade['symbol']} {trade['direction']} closed by {reason}"
            + (f": {note}" if note else "")
            + (
                f" (after {result['partial_fills']} partial fill(s))"
                if result.get("partial_fills") else ""
            ),
            level="INFO",
            category="EXIT",
            symbol=trade["symbol"],
            trade_id=trade["id"],
        )
    else:
        repo.insert_event(
            f"{trade['symbol']} {reason} close {status}: "
            f"{result.get('reason')}",
            level="ERROR",
            category="EXIT",
            symbol=trade["symbol"],
            trade_id=trade["id"],
        )

    return result


# =====================================================================
# Reversal
# =====================================================================

def opposing_positions(symbol, signal):
    """Open positions on `symbol` facing the other way."""

    return [
        trade for trade in repo.get_open_trades()
        if trade.get("symbol") == symbol
        and str(trade.get("direction", "")).upper() != str(signal).upper()
    ]


def handle_reversal(symbol, signal, decision, profile):
    """
    Close an opposing position when the AI flips with conviction.

    Returns (closed_any, detail).

    The threshold is the profile's own score floor: a flip is only
    worth acting on if it would have been worth entering on. A
    low-conviction flip closing a good position would be the worst of
    both.

    Deliberately does NOT enter the new direction here. The close must
    settle and reconcile first; the entry is then judged next bar by
    the risk engine against a book it can actually see.
    """

    if str(signal).upper() not in {"BUY", "SELL"}:
        return False, {"reason": "not a directional signal"}

    opposing = opposing_positions(symbol, signal)

    if not opposing:
        return False, {"reason": "no opposing position"}

    threshold = float((profile or {}).get("min_ai_score") or 0)

    score = decision.get("ai_score")

    if threshold and (score is None or float(score) < threshold):
        return False, {
            "reason": (
                f"flip to {signal} scored {score}, below the {threshold:g} "
                f"floor; holding the existing position"
            ),
            "score": score,
        }

    results = []

    for trade in opposing:
        repo.insert_event(
            f"{symbol}: AI flipped {trade['direction']} -> {signal} "
            f"(score {score}); closing on reversal rather than opening "
            f"the opposite side.",
            level="WARN",
            category="EXIT",
            symbol=symbol,
            trade_id=trade["id"],
        )

        results.append(close_trade(
            trade, REVERSAL, note=f"AI flipped to {signal} at score {score}"
        ))

    closed = any(
        r.get("status") in {"CLOSED", "ALREADY_CLOSED"} for r in results
    )

    return closed, {"closed": len(results), "results": results}


# =====================================================================
# Flatten windows
# =====================================================================

def server_now(offset_minutes=None):
    now = datetime.now(timezone.utc)

    return now + timedelta(minutes=offset_minutes or 0)


def weekend_flatten_due(profile, offset_minutes=None, now=None):
    """
    Is the weekend flatten window open?

    Measured on the BROKER's clock, because the broker's Friday close
    is what actually ends the trading week - a UTC Friday would be the
    wrong moment by the server offset, and on a UTC+3 broker that is
    three hours of unhedged weekend gap risk.

    `flatten_before_weekend_min` is minutes before the market closes,
    which is taken as Friday 23:59 server time.
    """

    minutes_before = int((profile or {}).get("flatten_before_weekend_min") or 0)

    if minutes_before <= 0:
        return False, None

    moment = now or server_now(offset_minutes)

    # Monday is 0; Friday is 4.
    if moment.weekday() != 4:
        return False, None

    close = moment.replace(hour=23, minute=59, second=0, microsecond=0)

    window_opens = close - timedelta(minutes=minutes_before)

    if moment >= window_opens:
        return True, {
            "window_opens": window_opens.isoformat(),
            "server_now": moment.isoformat(),
            "minutes_before_close": minutes_before,
        }

    return False, None


def flatten_all(reason=FLATTEN, note=None):
    """
    Close every position the bot believes it holds.

    Reports what it did rather than assuming success - a flatten that
    partially failed is exactly the situation an operator needs told
    about.
    """

    open_trades = repo.get_open_trades()

    if not open_trades:
        return {"closed": 0, "failed": 0, "results": []}

    results = []

    for trade in open_trades:
        results.append(close_trade(trade, reason, note=note))

    closed = sum(
        1 for r in results
        if r.get("status") in {"CLOSED", "ALREADY_CLOSED"}
    )

    failed = len(results) - closed

    repo.insert_event(
        f"{reason}: closed {closed} position(s)"
        + (f", {failed} FAILED" if failed else ""),
        level="ERROR" if failed else "INFO",
        category="EXIT",
        data={"closed": closed, "failed": failed},
    )

    return {"closed": closed, "failed": failed, "results": results}


def run_scheduled_exits(profile, offset_minutes=None, now=None):
    """
    Time-driven exits, called from the loop every poll.

    Independent of whether any symbol produced a new bar: a flatten
    that only fires when a bar closes is a flatten that misses its
    window.
    """

    due, detail = weekend_flatten_due(profile, offset_minutes, now)

    if not due:
        return {"flattened": False}

    if not repo.get_open_trades():
        return {"flattened": False, "reason": "nothing open"}

    result = flatten_all(
        FLATTEN,
        note=(
            f"weekend flatten, {detail['minutes_before_close']} min before "
            f"the server's Friday close"
        ),
    )

    return {"flattened": True, "detail": detail, **result}
