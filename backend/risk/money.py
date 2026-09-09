"""
Money risk: turning a stop distance into account currency (Phase 2).

Phase 0 §2.4 recorded the gap this closes. `trades.risk_amount` existed
in the schema but was never populated, and R-multiples were computed in
price terms only. Every prop-firm rule - 3% daily, the $10k floor,
consistency - is denominated in MONEY, so the system could not evaluate
a single one of them.

The conversion
--------------
MT5 gives, per symbol:

    trade_tick_size    the smallest price increment
    trade_tick_value   what one tick is worth, for ONE LOT, in the
                       ACCOUNT's currency

so:

    ticks  = |entry - stop| / trade_tick_size
    risk   = ticks * trade_tick_value * volume

No FX conversion is needed on top: MT5 has already expressed
trade_tick_value in the deposit currency.

Sanity, on the demo broker's specs:

    EURUSDm  0.01 lot, 22 pip stop  -> 220 ticks x $1.00 x 0.01 = $2.20
    BTCUSDm  0.01 lot, $128 stop    -> 12800 ticks x $0.01 x 0.01 = $1.28
    XAUUSDm  0.01 lot, $4.80 stop   -> 4800 ticks x $0.10 x 0.01 = $4.80

Each matches the hand calculation for that contract size, which is the
check the unit tests encode.

Two distances, deliberately
---------------------------
Phase 0 §2.5 flagged that the stop distance ignores spread. There are
two different quantities here and conflating them is the actual bug:

  stop_distance_price      |entry - stop|
      What you LOSE if the stop is hit. A BUY fills at ask and closes
      at bid; MT5 triggers the stop when bid <= sl and closes there, so
      the realised loss is exactly entry(ask) - sl. This is the correct
      basis for risk_amount, and it makes a full stop-out equal -1R.

  stop_distance_effective  |marking price - stop|
      How far the market must MOVE against you to trigger the stop,
      measured from the price the position is marked at (bid for a
      long, ask for a short). This is `stop_distance_price - spread`.
      It governs the PROBABILITY of being stopped, not the size of the
      loss, and it is why a nominal 1:2 reward:risk is optimistic.

Both are stored. Risk sizing uses the first; Phase 6 needs the second
to measure the spread drag honestly.
"""


def stop_distance(entry_price, stop_loss):
    """
    |entry - stop| in price terms: what a stop-out actually costs.

    None when either price is missing, so callers can tell "no stop"
    apart from "zero distance".
    """

    if entry_price is None or stop_loss is None:
        return None

    return abs(float(entry_price) - float(stop_loss))


def effective_stop_distance(entry_price, stop_loss, direction, spread):
    """
    Distance from the MARKING price to the stop.

    A long is marked at bid (entry was ask) and a short at ask (entry
    was bid), so in both directions this is the nominal distance minus
    the spread.

    Falls back to the nominal distance when the spread is unknown.
    Never returns a negative number: a spread wider than the stop means
    the position is already through its stop, which is reported as 0.
    """

    nominal = stop_distance(entry_price, stop_loss)

    if nominal is None:
        return None

    if not spread:
        return nominal

    return max(0.0, nominal - abs(float(spread)))


def risk_amount(distance_price, volume, symbol_profile):
    """
    Convert a price distance into account currency.

    `symbol_profile` is a row from symbol_profiles (Phase 1), or any
    mapping carrying trade_tick_size and trade_tick_value.

    Returns None when the specification is missing or unusable, rather
    than a wrong number - a silently wrong risk figure is worse than an
    absent one, because the Phase 3 risk engine will refuse to trade on
    None but would happily approve a bad value.
    """

    if distance_price is None or volume is None or not symbol_profile:
        return None

    tick_size = symbol_profile.get("trade_tick_size")
    tick_value = symbol_profile.get("trade_tick_value")

    if not tick_size or tick_value is None:
        return None

    try:
        ticks = abs(float(distance_price)) / float(tick_size)

        return round(ticks * float(tick_value) * float(volume), 6)

    except (TypeError, ValueError, ZeroDivisionError):
        return None


def volume_for_risk(target_risk, distance_price, symbol_profile):
    """
    Inverse of risk_amount: the volume that puts `target_risk` at stake.

    Returned RAW - unrounded and unclamped. Phase 5 owns rounding to
    volume_step and clamping to the broker's volume limits, because
    that also needs a margin check.
    """

    if not target_risk or not distance_price or not symbol_profile:
        return None

    tick_size = symbol_profile.get("trade_tick_size")
    tick_value = symbol_profile.get("trade_tick_value")

    if not tick_size or not tick_value:
        return None

    try:
        ticks = abs(float(distance_price)) / float(tick_size)

        risk_per_lot = ticks * float(tick_value)

        if risk_per_lot <= 0:
            return None

        return float(target_risk) / risk_per_lot

    except (TypeError, ValueError, ZeroDivisionError):
        return None


def r_multiple(pnl, risk_amount_value):
    """
    Realised R in MONEY terms.

    Preferred over the price-based version because `pnl` already nets
    commission and swap, so this answers "how many multiples of what I
    risked did I actually keep", which is the question that matters.

    None when there is no usable risk figure - callers fall back to the
    price-based calculation for rows written before Phase 2.
    """

    if pnl is None or not risk_amount_value:
        return None

    try:
        return round(float(pnl) / abs(float(risk_amount_value)), 4)
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def describe(entry_price, stop_loss, direction, volume, spread,
             symbol_profile):
    """
    Everything Phase 2 stores about one trade's risk, in one call.

    Returned as a dict so the engine can splat it onto the trade intent
    without repeating the field names at the call site.
    """

    nominal = stop_distance(entry_price, stop_loss)

    effective = effective_stop_distance(
        entry_price, stop_loss, direction, spread
    )

    return {
        "stop_distance_price": nominal,
        "stop_distance_effective": effective,
        "spread_at_entry": spread,
        "risk_amount": risk_amount(nominal, volume, symbol_profile),
    }
