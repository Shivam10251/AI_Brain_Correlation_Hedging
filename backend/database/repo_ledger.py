"""
Daily ledger: per-account, per-trading-day P&L (Phase 2).

Why this table exists
---------------------
Every prop-firm rule is a money figure over a day: 3% daily loss, the
$10,000 floor, "best day <= 50% of total profit". Phase 0 §2.4 found
that no per-day currency figure existed anywhere, so none of those
rules could be evaluated even in principle.

The trading day
---------------
`trading_day` is the BROKER SERVER's calendar day, not UTC. The firm
computes its daily limit on server time, so a UTC day would misalign
the window by the server offset - up to three hours of trades landing
in the wrong bucket, which is exactly the kind of error that shows up
as an unexplained breach.

MT5 reports both bar times and deal times as server wall-clock stamped
as if it were an epoch, so a deal's server date can be read straight
off its timestamp.

Maintenance
-----------
The row is updated incrementally as trades close, so the current day's
figure is always fresh, and then RECONCILED against MT5's own deal
history every cycle. On disagreement MT5 wins and the drift is logged:
the mirror is never allowed to quietly diverge from the broker.
"""

from datetime import datetime, timedelta, timezone

from ._common import _insert, _rows, utc_now
from .connection import get_connection, write_lock


# ---------------------------------------------------------------------
# Trading-day arithmetic
# ---------------------------------------------------------------------

def server_now(offset_minutes=None):
    """Current time on the broker's clock."""

    now = datetime.now(timezone.utc)

    if not offset_minutes:
        return now

    return now + timedelta(minutes=offset_minutes)


def trading_day(offset_minutes=None, moment=None):
    """
    The broker-server calendar day, as 'YYYY-MM-DD'.

    `moment` is a UTC datetime; defaults to now.
    """

    base = moment or datetime.now(timezone.utc)

    if offset_minutes:
        base = base + timedelta(minutes=offset_minutes)

    return base.date().isoformat()


def day_from_server_epoch(epoch_seconds):
    """
    The server day of an MT5 timestamp.

    MT5 deal/tick times are server wall clock expressed as an epoch, so
    reading them as UTC yields the server date directly - no offset is
    applied here, and applying one would double-count it.
    """

    if not epoch_seconds:
        return None

    return datetime.fromtimestamp(
        epoch_seconds, tz=timezone.utc
    ).date().isoformat()


def day_bounds_utc(day, offset_minutes=None):
    """
    (start, end) as UTC datetimes for a server trading day.

    Used to bound the MT5 history query. Generous by design - the exact
    membership test is done per-deal on its own server date.
    """

    start_server = datetime.fromisoformat(day + "T00:00:00+00:00")

    end_server = start_server + timedelta(days=1)

    shift = timedelta(minutes=offset_minutes or 0)

    return start_server - shift, end_server - shift


# ---------------------------------------------------------------------
# Read / create
# ---------------------------------------------------------------------

LEDGER_SUM_FIELDS = (
    "realized_pnl", "gross_pnl", "commission", "swap",
    "trade_count", "win_count", "loss_count",
)


def get_ledger(account_id, day):
    row = get_connection().execute(
        "SELECT * FROM daily_ledger WHERE account_id = ? AND trading_day = ?",
        (account_id, day),
    ).fetchone()

    return dict(row) if row else None


def get_ledgers(account_id=None, limit=90):
    sql = "SELECT * FROM daily_ledger"
    params = []

    if account_id is not None:
        sql += " WHERE account_id = ?"
        params.append(account_id)

    sql += " ORDER BY trading_day DESC LIMIT ?"
    params.append(limit)

    return _rows(get_connection().execute(sql, params))


def ensure_ledger(account_id, day, equity=None, balance=None):
    """
    Return today's row, creating it if this is the first activity of
    the trading day.

    `equity`/`balance` seed the day's opening marks, which the daily
    loss limit is measured against.
    """

    existing = get_ledger(account_id, day)

    if existing:
        return existing

    _insert("daily_ledger", {
        "created_at": utc_now(),
        "updated_at": utc_now(),
        "account_id": account_id,
        "trading_day": day,
        "start_equity": equity,
        "start_balance": balance,
        "max_equity": equity,
        "min_equity": equity,
        "eod_equity": equity,
        "eod_balance": balance,
    })

    return get_ledger(account_id, day)


# ---------------------------------------------------------------------
# Write
# ---------------------------------------------------------------------

def _update_ledger(account_id, day, fields):
    if not fields:
        return 0

    assignments = ", ".join(f'"{k}" = ?' for k in fields)

    conn = get_connection()

    with write_lock:
        cursor = conn.execute(
            f"UPDATE daily_ledger SET {assignments}, updated_at = ? "
            f"WHERE account_id = ? AND trading_day = ?",
            [*fields.values(), utc_now(), account_id, day],
        )

        return cursor.rowcount


def record_equity_marks(account_id, day, equity=None, balance=None,
                        floating_pnl=None):
    """
    Update the day's equity high/low water marks and worst floating
    loss.

    These cannot be derived from deal history - an open position that
    went deeply negative and recovered leaves no trace in the deals -
    so they are sampled every cycle and are NOT touched by
    reconciliation.
    """

    ledger = ensure_ledger(account_id, day, equity, balance)

    fields = {}

    if equity is not None:

        if ledger["max_equity"] is None or equity > ledger["max_equity"]:
            fields["max_equity"] = equity

        if ledger["min_equity"] is None or equity < ledger["min_equity"]:
            fields["min_equity"] = equity

        fields["eod_equity"] = equity

    if balance is not None:
        fields["eod_balance"] = balance

    if floating_pnl is not None and floating_pnl < 0:

        worst = ledger["max_floating_loss"] or 0.0

        # Stored as a positive magnitude.
        if abs(floating_pnl) > worst:
            fields["max_floating_loss"] = round(abs(floating_pnl), 2)

    return _update_ledger(account_id, day, fields)


def apply_closed_trade(account_id, day, pnl, commission=0.0, swap=0.0,
                       equity=None, balance=None):
    """
    Fold one settled trade into the day's totals.

    `pnl` is NET of costs (that is what the reconciler stores), so
    gross is derived by removing them rather than the other way round.
    """

    ensure_ledger(account_id, day, equity, balance)

    pnl = float(pnl or 0.0)
    commission = float(commission or 0.0)
    swap = float(swap or 0.0)

    conn = get_connection()

    with write_lock:
        conn.execute(
            "UPDATE daily_ledger SET "
            "  realized_pnl = ROUND(realized_pnl + ?, 6), "
            "  gross_pnl    = ROUND(gross_pnl + ?, 6), "
            "  commission   = ROUND(commission + ?, 6), "
            "  swap         = ROUND(swap + ?, 6), "
            "  trade_count  = trade_count + 1, "
            "  win_count    = win_count + ?, "
            "  loss_count   = loss_count + ?, "
            "  updated_at   = ? "
            "WHERE account_id = ? AND trading_day = ?",
            (
                pnl,
                pnl - commission - swap,
                commission,
                swap,
                1 if pnl > 0 else 0,
                1 if pnl < 0 else 0,
                utc_now(),
                account_id,
                day,
            ),
        )

    return get_ledger(account_id, day)


def apply_reconciled_totals(account_id, day, totals, drift):
    """
    Overwrite the day's realised figures with MT5's own numbers.

    MT5 is the source of truth. The equity marks are left alone because
    they are sampled, not derived.
    """

    ensure_ledger(account_id, day)

    return _update_ledger(account_id, day, {
        "realized_pnl": round(totals["realized_pnl"], 6),
        "gross_pnl": round(totals["gross_pnl"], 6),
        "commission": round(totals["commission"], 6),
        "swap": round(totals["swap"], 6),
        "trade_count": totals["trade_count"],
        "win_count": totals["win_count"],
        "loss_count": totals["loss_count"],
        "reconciled_at": utc_now(),
        "drift": round(drift, 6),
    })


# ---------------------------------------------------------------------
# Queries the Phase 3 risk engine will need
# ---------------------------------------------------------------------

def day_summary(account_id, day, floating_pnl=0.0):
    """
    Everything a daily-loss check needs, in one read.

    `exposure` is realised + floating: the number a 3%-daily rule is
    actually measured against.
    """

    ledger = get_ledger(account_id, day)

    if ledger is None:
        return {
            "trading_day": day,
            "realized_pnl": 0.0,
            "floating_pnl": float(floating_pnl or 0.0),
            "exposure": float(floating_pnl or 0.0),
            "trade_count": 0,
            "loss_count": 0,
            "win_count": 0,
            "max_floating_loss": 0.0,
            "start_equity": None,
            "start_balance": None,
            "exists": False,
        }

    realized = float(ledger["realized_pnl"] or 0.0)
    floating = float(floating_pnl or 0.0)

    return {
        "trading_day": day,
        "realized_pnl": realized,
        "floating_pnl": floating,
        "exposure": round(realized + floating, 6),
        "trade_count": ledger["trade_count"],
        "loss_count": ledger["loss_count"],
        "win_count": ledger["win_count"],
        "max_floating_loss": float(ledger["max_floating_loss"] or 0.0),
        "start_equity": ledger["start_equity"],
        "start_balance": ledger["start_balance"],
        "max_equity": ledger["max_equity"],
        "min_equity": ledger["min_equity"],
        "drift": ledger["drift"],
        "reconciled_at": ledger["reconciled_at"],
        "exists": True,
    }
