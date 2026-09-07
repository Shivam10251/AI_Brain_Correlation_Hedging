"""
Performance analytics computed from the trades table (Phase 12).

Everything here is derived on demand from stored rows - nothing is
cached or precomputed, so the numbers can never drift from the data.

Note on interpretation: these are descriptive statistics over whatever
trades happen to be in the database. With a small number of closed
trades they describe the sample and nothing more; the `sample_note`
field carries that caveat to the UI.
"""

from .connection import get_connection


# Below this many closed trades, the metrics are not a meaningful
# estimate of forward performance and should be labelled as such.
MIN_MEANINGFUL_SAMPLE = 30


def _closed_trades(symbol=None, setup=None, timeframe=None,
                   market_regime=None, min_score=None, max_score=None,
                   since_iso=None):
    """Closed trades matching the requested slice."""

    sql = (
        "SELECT * FROM trades "
        "WHERE execution_status = 'CLOSED' AND pnl IS NOT NULL"
    )
    params = []

    for column, value in (
        ("symbol", symbol),
        ("setup", setup),
        ("timeframe", timeframe),
        ("market_regime", market_regime),
    ):
        if value:
            sql += f" AND {column} = ?"
            params.append(value)

    if min_score is not None:
        sql += " AND ai_score >= ?"
        params.append(min_score)

    if max_score is not None:
        sql += " AND ai_score <= ?"
        params.append(max_score)

    if since_iso:
        sql += " AND closed_at >= ?"
        params.append(since_iso)

    sql += " ORDER BY closed_at ASC, id ASC"

    return [dict(r) for r in get_connection().execute(sql, params)]


def _max_drawdown(equity_series):
    """
    Peak-to-trough drawdown of a cumulative series.

    Returns (absolute_drawdown, percent_of_peak).
    """

    if not equity_series:
        return 0.0, 0.0

    peak = equity_series[0]
    max_dd = 0.0
    max_dd_pct = 0.0

    for value in equity_series:
        if value > peak:
            peak = value

        drawdown = peak - value

        if drawdown > max_dd:
            max_dd = drawdown

            # Guard against a zero/negative peak on a cumulative P&L
            # series that starts at or below zero.
            max_dd_pct = (drawdown / abs(peak) * 100.0) if peak else 0.0

    return max_dd, max_dd_pct


def _streaks(outcomes):
    """Longest consecutive win and loss runs."""

    best_win = best_loss = current_win = current_loss = 0

    for outcome in outcomes:
        if outcome == "WIN":
            current_win += 1
            current_loss = 0
        elif outcome == "LOSS":
            current_loss += 1
            current_win = 0
        else:
            current_win = current_loss = 0

        best_win = max(best_win, current_win)
        best_loss = max(best_loss, current_loss)

    return best_win, best_loss


def compute_metrics(**filters):
    """
    Full metric set for a slice of closed trades.

    Accepts: symbol, setup, timeframe, market_regime, min_score,
    max_score, since_iso.
    """

    trades = _closed_trades(**filters)

    total = len(trades)

    empty = {
        "total_trades": 0,
        "total_pnl": 0.0,
        "win_rate": None,
        "loss_rate": None,
        "profit_factor": None,
        "expectancy": None,
        "average_r": None,
        "average_win": None,
        "average_loss": None,
        "max_drawdown": 0.0,
        "max_drawdown_pct": 0.0,
        "wins": 0,
        "losses": 0,
        "breakeven": 0,
        "max_consecutive_wins": 0,
        "max_consecutive_losses": 0,
        "sample_note": "No closed trades yet.",
    }

    if total == 0:
        return empty

    pnls = [float(t["pnl"] or 0.0) for t in trades]

    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    breakeven = [p for p in pnls if p == 0]

    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))

    r_values = [
        float(t["r_multiple"])
        for t in trades
        if t["r_multiple"] is not None
    ]

    # Cumulative P&L curve -> drawdown
    cumulative = []
    running = 0.0

    for pnl in pnls:
        running += pnl
        cumulative.append(running)

    max_dd, max_dd_pct = _max_drawdown(cumulative)

    max_win_streak, max_loss_streak = _streaks(
        [t.get("result") for t in trades]
    )

    if total < MIN_MEANINGFUL_SAMPLE:
        note = (
            f"Descriptive only - {total} closed trade(s). "
            f"Not a statistically meaningful sample "
            f"(< {MIN_MEANINGFUL_SAMPLE})."
        )
    else:
        note = f"Computed over {total} closed trades."

    return {
        "total_trades": total,
        "total_pnl": round(sum(pnls), 2),

        "wins": len(wins),
        "losses": len(losses),
        "breakeven": len(breakeven),

        "win_rate": round(len(wins) / total * 100.0, 2),
        "loss_rate": round(len(losses) / total * 100.0, 2),

        # Undefined rather than infinite when there are no losses yet.
        "profit_factor": (
            round(gross_profit / gross_loss, 3) if gross_loss > 0 else None
        ),

        "expectancy": round(sum(pnls) / total, 4),

        "average_r": round(sum(r_values) / len(r_values), 3) if r_values else None,

        "average_win": round(gross_profit / len(wins), 2) if wins else None,
        "average_loss": round(sum(losses) / len(losses), 2) if losses else None,

        "gross_profit": round(gross_profit, 2),
        "gross_loss": round(gross_loss, 2),

        "max_drawdown": round(max_dd, 2),
        "max_drawdown_pct": round(max_dd_pct, 2),

        "max_consecutive_wins": max_win_streak,
        "max_consecutive_losses": max_loss_streak,

        "sample_note": note,
    }


def daily_pnl(limit_days=90):
    """P&L grouped by close date, oldest first."""

    rows = get_connection().execute(
        "SELECT substr(closed_at, 1, 10) AS day, "
        "       ROUND(SUM(pnl), 2) AS pnl, "
        "       COUNT(*) AS trades "
        "FROM trades "
        "WHERE execution_status = 'CLOSED' AND pnl IS NOT NULL "
        "GROUP BY day "
        "ORDER BY day DESC "
        "LIMIT ?",
        (limit_days,),
    ).fetchall()

    return list(reversed([dict(r) for r in rows]))


def breakdown(dimension):
    """
    Metrics grouped by one dimension: symbol, setup, timeframe,
    market_regime, or ai_score bucket.
    """

    allowed = {"symbol", "setup", "timeframe", "market_regime"}

    if dimension == "ai_score":
        rows = get_connection().execute(
            "SELECT DISTINCT "
            "  CASE WHEN ai_score >= 80 THEN '80-100' "
            "       WHEN ai_score >= 60 THEN '60-79' "
            "       WHEN ai_score >= 40 THEN '40-59' "
            "       ELSE '0-39' END AS bucket "
            "FROM trades "
            "WHERE execution_status = 'CLOSED' AND ai_score IS NOT NULL"
        ).fetchall()

        results = {}

        for row in rows:
            bucket = row["bucket"]

            low, high = bucket.split("-")

            results[bucket] = compute_metrics(
                min_score=int(low), max_score=int(high)
            )

        return results

    if dimension not in allowed:
        raise ValueError(f"Unsupported breakdown dimension: {dimension}")

    rows = get_connection().execute(
        f"SELECT DISTINCT {dimension} AS value FROM trades "
        f"WHERE execution_status = 'CLOSED' AND {dimension} IS NOT NULL"
    ).fetchall()

    return {
        row["value"]: compute_metrics(**{dimension: row["value"]})
        for row in rows
    }
