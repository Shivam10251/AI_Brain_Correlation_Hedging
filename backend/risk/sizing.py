"""
Position sizing (Phase 5).

Phase 0 weakness #4: the bot traded a fixed 0.01 lot regardless of
account, instrument or stop distance. On a $100k evaluation that
cannot reach a profit target; on a small account it is an unbounded
fraction of equity. Neither is a decision anybody made - it was a
default nobody revisited.

Sizing inverts the Phase 2 risk formula:

    volume = target_risk / (stop_ticks * trade_tick_value)

then applies the broker's own constraints, in this order:

  1. floor to volume_step   - MT5 rejects any other increment
  2. clamp to volume_min/max
  3. margin check           - order_calc_margin against free margin

Flooring rather than rounding is deliberate: rounding up would put
MORE at stake than the profile allows, which is the one direction the
risk engine must never be surprised in.

The result is always accompanied by the risk it actually implies, so
the caller stores the real figure rather than the target it asked for.
"""

import math

from backend.risk import money


# Never commit more than this share of free margin to one position.
# A fill that consumes all free margin leaves nothing for the adverse
# excursion before the stop, and MT5 starts closing positions itself.
MARGIN_SAFETY_FACTOR = 0.8


def _floor_to_step(volume, step):
    """
    Round DOWN to the broker's volume increment.

    Down, not nearest: rounding up would risk more than the profile
    allows.
    """

    if not step or step <= 0:
        return volume

    steps = math.floor(round(volume / step, 9))

    return round(steps * step, 10)


def compute_volume(target_risk, stop_distance, symbol_profile,
                   max_volume=None):
    """
    The volume that risks `target_risk` over `stop_distance`.

    Returns a dict describing what was decided and why, rather than a
    bare number - "refused because the minimum lot risks more than the
    budget" is information the caller needs to log.
    """

    result = {
        "volume": None,
        "raw_volume": None,
        "risk_amount": None,
        "target_risk": target_risk,
        "reason": None,
        "clamped": None,
    }

    if not target_risk or not stop_distance or not symbol_profile:
        result["reason"] = "missing target risk, stop distance or profile"
        return result

    raw = money.volume_for_risk(target_risk, stop_distance, symbol_profile)

    if raw is None or raw <= 0:
        result["reason"] = "broker specification does not allow sizing"
        return result

    result["raw_volume"] = raw

    step = symbol_profile.get("volume_step") or 0.01
    minimum = symbol_profile.get("volume_min") or step
    maximum = symbol_profile.get("volume_max") or None

    volume = _floor_to_step(raw, step)

    if max_volume:
        maximum = min(maximum, max_volume) if maximum else max_volume

    if volume < minimum:
        # The smallest tradeable size already risks more than allowed.
        # Report it: silently trading the minimum would breach the
        # per-trade budget the risk engine is enforcing.
        implied = money.risk_amount(stop_distance, minimum, symbol_profile)

        result.update({
            "volume": None,
            "risk_amount": implied,
            "reason": (
                f"minimum volume {minimum} risks "
                f"{implied if implied is None else round(implied, 2)} "
                f"which exceeds the target of {round(target_risk, 2)}"
            ),
            "clamped": "below_minimum",
        })

        return result

    if maximum and volume > maximum:
        volume = _floor_to_step(maximum, step)
        result["clamped"] = "at_maximum"

    result["volume"] = volume

    result["risk_amount"] = money.risk_amount(
        stop_distance, volume, symbol_profile
    )

    return result


def margin_is_available(symbol, direction, volume, price, account,
                        order_type=None):
    """
    Would this order fit inside free margin, with a safety factor?

    Returns (ok, detail). A terminal that cannot answer is treated as
    OK: MT5 will reject the order itself if margin is genuinely short,
    and refusing every trade because order_calc_margin is unavailable
    would be worse than letting the broker decide.
    """

    import MetaTrader5 as mt5

    detail = {"required": None, "free": account.get("margin_free")}

    try:
        if order_type is None:
            order_type = (
                mt5.ORDER_TYPE_BUY if direction == "BUY"
                else mt5.ORDER_TYPE_SELL
            )

        required = mt5.order_calc_margin(order_type, symbol, volume, price)

    except Exception:                               # noqa: BLE001
        required = None

    if required is None:
        detail["reason"] = "order_calc_margin unavailable; deferring to broker"
        return True, detail

    free = float(account.get("margin_free") or 0.0)

    budget = free * MARGIN_SAFETY_FACTOR

    detail.update({
        "required": required,
        "free": free,
        "budget": round(budget, 2),
    })

    if required > budget:
        detail["reason"] = (
            f"margin {required:.2f} exceeds {MARGIN_SAFETY_FACTOR:.0%} of "
            f"free margin ({budget:.2f})"
        )

        return False, detail

    return True, detail


def plan(symbol, direction, entry_price, stop_distance, profile, account,
         symbol_profile):
    """
    Full sizing decision for one order.

    Combines the profile's risk budget, the broker's volume
    constraints, and the margin check into one answer the engine can
    act on directly.
    """

    from backend.risk import profiles as risk_profiles

    target = risk_profiles.risk_budget(profile, account)

    if target is None:
        return {
            "volume": None,
            "reason": "profile declares no per-trade risk budget",
        }

    sized = compute_volume(
        target,
        stop_distance,
        symbol_profile,
        max_volume=profile.get("max_volume"),
    )

    if not sized.get("volume"):
        return sized

    ok, margin = margin_is_available(
        symbol, direction, sized["volume"], entry_price, account
    )

    sized["margin"] = margin

    if not ok:
        sized["volume"] = None
        sized["reason"] = margin.get("reason")
        sized["clamped"] = "margin"

    return sized
