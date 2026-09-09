"""
Net correlated exposure (Phase 3, check 14).

Phase 0 C3 and weakness #12: the universe is four bets on essentially
one thing. EURUSD and GBPUSD correlate around 0.85-0.95; gold and BTC
both trade as risk/dollar proxies. Four simultaneous 1R longs is a 4R
bet on a single dollar move, not four independent 1R bets - and a 4R
adverse move is a breached daily limit.

This module reduces open positions to one signed number: net exposure
to a falling dollar, measured in R.

    contribution = direction_sign * weight(symbol) * (risk / 1R)

A long EURUSD is short the dollar, so it carries weight +1.0. A long
USDJPY is long the dollar: -1.0. Gold and BTC get fractional weights
because they are proxies, not pure dollar pairs - the exact values are
profile data, deliberately, because they are an assumption to be
tested (Phase 0 E6) rather than a fact.
"""

import re


# Broker suffixes and separators to strip when matching a symbol
# against the profile's weight table: EURUSDm, EURUSD.raw and
# EURUSD_i are all EURUSD.
_SUFFIX = re.compile(r"[._\-]?[a-z]*$")


def normalise_symbol(symbol):
    """
    Reduce a broker symbol to its canonical name.

    'EURUSDm' -> 'EURUSD', 'XAUUSD.raw' -> 'XAUUSD'.
    """

    if not symbol:
        return ""

    upper = str(symbol).upper()

    # Strip a trailing lowercase/punctuation suffix from the ORIGINAL
    # (case matters: the suffix is lowercase, the pair is not).
    stripped = _SUFFIX.sub("", str(symbol))

    return (stripped or upper).upper()


def weight_for(symbol, profile):
    """
    The symbol's dollar-beta weight, or None when it is not mapped.

    An unmapped symbol is reported as unknown rather than assumed to be
    zero: silently treating an unrecognised instrument as
    non-correlated is how a cap stops working.
    """

    weights = profile.get("exposure_weights") or {}

    canonical = normalise_symbol(symbol)

    if canonical in weights:
        return float(weights[canonical])

    # Allow an exact broker-name override too.
    if symbol in weights:
        return float(weights[symbol])

    return None


def position_contribution(symbol, direction, risk_amount, risk_unit,
                          profile):
    """
    One position's signed exposure in R.

    `risk_unit` is what 1R means for this account - the profile's
    per-trade budget. Falls back to counting the position as 1R when no
    risk figure is available, which is conservative: an unmeasurable
    position still consumes cap.
    """

    weight = weight_for(symbol, profile)

    if weight is None:
        return None

    sign = 1.0 if str(direction).upper() == "BUY" else -1.0

    if risk_amount and risk_unit:
        r = float(risk_amount) / float(risk_unit)
    else:
        r = 1.0

    return sign * weight * r


def net_exposure(open_trades, profile, risk_unit):
    """
    Net dollar-beta exposure of everything currently open.

    Returns (net_r, detail) where detail lists each contribution and
    any symbol that could not be mapped.
    """

    net = 0.0
    detail = []
    unmapped = []

    for trade in open_trades or []:

        contribution = position_contribution(
            trade.get("symbol"),
            trade.get("direction"),
            trade.get("risk_amount"),
            risk_unit,
            profile,
        )

        if contribution is None:
            unmapped.append(trade.get("symbol"))
            continue

        net += contribution

        detail.append({
            "symbol": trade.get("symbol"),
            "direction": trade.get("direction"),
            "contribution_r": round(contribution, 4),
        })

    return round(net, 4), {"positions": detail, "unmapped": unmapped}


def projected_exposure(open_trades, profile, risk_unit,
                       symbol, direction, risk_amount):
    """
    Net exposure as it WOULD be if this trade were opened.

    The check has to be forward-looking: refusing only once the cap is
    already breached is refusing one trade too late.
    """

    current, detail = net_exposure(open_trades, profile, risk_unit)

    contribution = position_contribution(
        symbol, direction, risk_amount, risk_unit, profile
    )

    if contribution is None:
        detail["unmapped"] = detail["unmapped"] + [symbol]

        # Unknown instrument: charge it a full R in the traded
        # direction so an unmapped symbol cannot bypass the cap.
        contribution = 1.0 if str(direction).upper() == "BUY" else -1.0

    projected = round(current + contribution, 4)

    detail["current_r"] = current
    detail["new_trade_r"] = round(contribution, 4)
    detail["projected_r"] = projected

    return projected, detail
