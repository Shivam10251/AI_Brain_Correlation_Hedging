"""
Stop placement as a VERSIONED parameter (Phase 5).

Phase 0 B4 proposed ATR-scaled stops over the fixed 0.20%/0.40%. The
important part is not which is better - that is E3, and it is answered
by replaying stored decisions in Phase 8 - but that the choice is a
strategy VERSION rather than an edit.

Two models, never both at once:

  v1.0.0  percent   sl = entry * (1 -/+ SL_PERCENT)
                    tp = entry * (1 +/- TP_PERCENT)
                    The Phase 0 baseline. Frozen for the Phase 6
                    calibration run so the study measures one thing.

  v1.1.0  atr       sl = entry -/+ k_sl * ATR14(H1)
                    tp = entry +/- k_tp * ATR14(H1)
                    Same nominal reward:risk, but the distance scales
                    with the instrument's own volatility instead of
                    being a fixed fraction of price - a 0.20% stop is
                    a very different trade on EURUSD than on BTC.

Both respect the broker's trade_stops_level: an order whose stop sits
inside the broker's minimum distance is rejected outright, which on the
old code path looked like an unexplained execution failure.
"""

PERCENT = "percent"

ATR = "atr"

MODELS = (PERCENT, ATR)


def _round_to_digits(value, digits):
    if value is None:
        return None

    if digits is None:
        return value

    return round(value, int(digits))


def minimum_distance(symbol_profile, price):
    """
    The broker's minimum stop distance in PRICE terms.

    trade_stops_level is quoted in points, so it has to be multiplied
    by the symbol's point size to be comparable with a price distance.
    """

    if not symbol_profile:
        return 0.0

    point = symbol_profile.get("point") or 0.0
    level = symbol_profile.get("trade_stops_level") or 0

    return float(point) * float(level)


def compute(entry_price, direction, *, model=PERCENT, sl_percent=0.002,
            tp_percent=0.004, atr=None, k_sl=1.0, k_tp=2.0,
            symbol_profile=None):
    """
    Stop loss and take profit for one order.

    Returns a dict carrying both prices, the distance actually used and
    which model produced them, so the trade row records the geometry
    rather than leaving it to be re-derived later.

    Falls back from ATR to percent when no ATR is available, and says
    so in `model_used` - silently trading a different stop model than
    the one the strategy version declares would corrupt the very
    comparison the versioning exists to enable.
    """

    if entry_price is None or direction not in {"BUY", "SELL"}:
        return {
            "stop_loss": None, "take_profit": None,
            "stop_distance": None, "model_used": None,
            "clamped": False,
        }

    entry = float(entry_price)

    model_used = model

    if model == ATR and atr:
        sl_distance = float(k_sl) * float(atr)
        tp_distance = float(k_tp) * float(atr)
    else:
        if model == ATR:
            model_used = PERCENT      # no ATR available; be explicit

        sl_distance = entry * float(sl_percent)
        tp_distance = entry * float(tp_percent)

    # Broker minimum. A stop inside this is rejected by MT5, which on
    # the old path surfaced as an unexplained execution failure.
    floor = minimum_distance(symbol_profile, entry)

    clamped = False

    if floor and sl_distance < floor:
        sl_distance = floor
        clamped = True

    if floor and tp_distance < floor:
        tp_distance = floor
        clamped = True

    if direction == "BUY":
        stop_loss = entry - sl_distance
        take_profit = entry + tp_distance
    else:
        stop_loss = entry + sl_distance
        take_profit = entry - tp_distance

    digits = (symbol_profile or {}).get("digits")

    return {
        "stop_loss": _round_to_digits(stop_loss, digits),
        "take_profit": _round_to_digits(take_profit, digits),
        "stop_distance": sl_distance,
        "take_profit_distance": tp_distance,
        "model_used": model_used,
        "clamped": clamped,
        "broker_min_distance": floor,
    }


def model_for_strategy(params):
    """
    Which stop model a strategy version declares.

    Reads the frozen params snapshot, so a replay uses the model that
    version actually traded rather than whatever is configured today.
    """

    if not params:
        return PERCENT

    model = str(params.get("stop_model") or PERCENT).lower()

    return model if model in MODELS else PERCENT
