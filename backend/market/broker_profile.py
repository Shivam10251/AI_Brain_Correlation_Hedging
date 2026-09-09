"""
Broker and instrument facts, measured from the live terminal (Phase 1).

Phase 0 recorded three assumptions this module replaces with data:

  §2.2  bar times were treated as UTC. MT5 returns them in the BROKER
        SERVER's timezone. If the offset is not zero, every session
        label is wrong and the news engine cannot be aligned at all.

  §2.6  DEVIATION was a fixed 20 points, which is 2 pips on a 5-digit
        FX pair and $0.20 on a 2-digit crypto symbol.

  Doc 3 filling mode was hardcoded to IOC. A broker that only accepts
        FOK rejects every order, silently, forever.

Nothing here is assumed. Every value is read from the terminal and
stored, so the rest of the system can size, place stops and convert
timestamps against real numbers.
"""

import json
import statistics
import time

import MetaTrader5 as mt5

from backend import config
from backend.database import repo_meta
from backend.database import repository as repo


# ---------------------------------------------------------------------
# Margin mode
# ---------------------------------------------------------------------

# mt5.account_info().margin_mode
MARGIN_MODE_LABELS = {
    0: "RETAIL_NETTING",
    1: "EXCHANGE",
    2: "RETAIL_HEDGING",
}


# ---------------------------------------------------------------------
# Filling mode
#
# symbol_info.filling_mode is a BITMASK of what the broker supports.
# The order request needs an ORDER_FILLING_* enum, which is a different
# numbering. Mapping the two up is exactly the bug that made every
# order fail on a FOK-only broker.
# ---------------------------------------------------------------------

SYMBOL_FILLING_FOK = 1
SYMBOL_FILLING_IOC = 2

# Preference order: FOK is the strictest and safest for market orders
# (all-or-nothing, no surprise partial), then IOC, then RETURN.
_FILLING_PREFERENCE = (
    (SYMBOL_FILLING_FOK, "FOK", "ORDER_FILLING_FOK"),
    (SYMBOL_FILLING_IOC, "IOC", "ORDER_FILLING_IOC"),
)


def resolve_filling_mode(filling_mask):
    """
    Choose an order filling type from the broker's support bitmask.

    Returns (label, mt5_order_filling_constant).

    Falls back to RETURN when the mask advertises neither FOK nor IOC,
    which is what an exchange-execution symbol reports.
    """

    mask = int(filling_mask or 0)

    for bit, label, attribute in _FILLING_PREFERENCE:
        if mask & bit:
            return label, getattr(mt5, attribute)

    return "RETURN", getattr(mt5, "ORDER_FILLING_RETURN", 2)


# ---------------------------------------------------------------------
# Server clock offset
# ---------------------------------------------------------------------

# Broker server offsets are whole- or half-hour in practice, so the
# measurement is snapped to 30 minutes. That absorbs tick latency and
# a slow local clock without inventing precision we do not have.
OFFSET_SNAP_MINUTES = 30

OFFSET_SAMPLES = 3


def _measure_offset_once(symbol):
    """
    One offset sample, in minutes, or None if the symbol has no tick.

    tick.time is the broker's server time for the last quote; comparing
    it to our own clock gives the offset.
    """

    tick = mt5.symbol_info_tick(symbol)

    if tick is None:
        return None

    server_epoch = getattr(tick, "time", None)

    if not server_epoch:
        return None

    delta_seconds = server_epoch - time.time()

    snap = OFFSET_SNAP_MINUTES * 60

    return int(round(delta_seconds / snap) * OFFSET_SNAP_MINUTES)


def measure_server_utc_offset(symbols, samples=OFFSET_SAMPLES):
    """
    Measure the broker's UTC offset and assert it is stable.

    Returns (offset_minutes, samples, stable). `offset_minutes` is None
    when nothing could be measured - callers must treat that as
    UNKNOWN, never as zero.

    Sampling across several symbols matters: a symbol whose market is
    closed can carry a stale tick timestamp, which would otherwise be
    read as a wildly wrong offset.
    """

    readings = []

    for symbol in symbols:

        for _ in range(samples):

            value = _measure_offset_once(symbol)

            if value is not None:
                readings.append(value)

    if not readings:
        return None, [], False

    # The mode is robust to one stale-tick outlier; the mean is not.
    try:
        offset = statistics.mode(readings)
    except statistics.StatisticsError:
        offset = statistics.median_low(readings)

    stable = all(value == offset for value in readings)

    return offset, readings, stable


# ---------------------------------------------------------------------
# Capture
# ---------------------------------------------------------------------

def _symbol_facts(symbol):
    """Every broker-specified number we need for one instrument."""

    if not mt5.symbol_select(symbol, True):
        return None

    info = mt5.symbol_info(symbol)

    if info is None:
        return None

    filling_mask = getattr(info, "filling_mode", 0)

    label, order_type = resolve_filling_mode(filling_mask)

    def value(name, default=None):
        return getattr(info, name, default)

    return {
        "symbol": symbol,

        "digits": value("digits"),
        "point": value("point"),
        "contract_size": value("trade_contract_size"),

        "trade_tick_size": value("trade_tick_size"),
        "trade_tick_value": value("trade_tick_value"),
        "trade_tick_value_profit": value("trade_tick_value_profit"),
        "trade_tick_value_loss": value("trade_tick_value_loss"),

        "volume_min": value("volume_min"),
        "volume_step": value("volume_step"),
        "volume_max": value("volume_max"),

        "filling_mode": filling_mask,
        "filling_mode_chosen": label,
        "filling_order_type": order_type,

        "trade_stops_level": value("trade_stops_level"),
        "trade_freeze_level": value("trade_freeze_level"),
        "spread": value("spread"),
        "trade_mode": value("trade_mode"),

        "swap_long": value("swap_long"),
        "swap_short": value("swap_short"),
    }


def capture(symbols=None):
    """
    Read the account and every symbol from the terminal, persist both,
    and log anything that changed.

    Returns a summary dict. Never raises into the trading loop: on
    failure it returns {"available": False, "error": ...} so the
    dashboard still starts.
    """

    symbols = list(symbols or config.SYMBOLS)

    account = mt5.account_info()

    if account is None:
        return {
            "available": False,
            "error": f"account_info() returned None: {mt5.last_error()}",
        }

    account_id = getattr(account, "login", None)

    offset, samples, stable = measure_server_utc_offset(symbols)

    margin_mode = getattr(account, "margin_mode", None)

    terminal = mt5.terminal_info()

    terminal_json = None

    if terminal is not None:
        terminal_json = json.dumps(
            {
                "build": getattr(terminal, "build", None),
                "name": getattr(terminal, "name", None),
                "company": getattr(terminal, "company", None),
                "connected": getattr(terminal, "connected", None),
                "trade_allowed": getattr(terminal, "trade_allowed", None),
            },
            default=str,
        )

    profile_id, changes = repo_meta.upsert_broker_profile({
        "account_id": account_id,
        "server": getattr(account, "server", None),
        "company": getattr(account, "company", None),
        "currency": getattr(account, "currency", None),
        "leverage": getattr(account, "leverage", None),
        "margin_mode": margin_mode,
        "margin_mode_label": MARGIN_MODE_LABELS.get(margin_mode, "UNKNOWN"),
        "server_utc_offset_min": offset,
        "offset_samples_json": json.dumps(samples),
        "offset_stable": 1 if stable else 0,
        "trade_allowed": (
            1 if getattr(terminal, "trade_allowed", False) else 0
        ),
        "terminal_json": terminal_json,
    })

    captured = []
    missing = []

    for symbol in symbols:

        facts = _symbol_facts(symbol)

        if facts is None:
            missing.append(symbol)
            continue

        repo_meta.upsert_symbol_profile({
            "account_id": account_id,
            **facts,
        })

        captured.append({
            "symbol": symbol,
            "digits": facts["digits"],
            "filling": facts["filling_mode_chosen"],
            "stops_level": facts["trade_stops_level"],
            "volume_step": facts["volume_step"],
        })

    summary = {
        "available": True,
        "profile_id": profile_id,
        "account_id": account_id,
        "server": getattr(account, "server", None),
        "currency": getattr(account, "currency", None),
        "margin_mode": margin_mode,
        "margin_mode_label": MARGIN_MODE_LABELS.get(margin_mode, "UNKNOWN"),
        "server_utc_offset_min": offset,
        "offset_stable": stable,
        "offset_samples": samples,
        "symbols": captured,
        "unavailable_symbols": missing,
        "changes": {k: list(v) for k, v in changes.items()},
    }

    # ------------------------------------------------------------
    # Startup log - this is a Phase 1 acceptance criterion.
    # ------------------------------------------------------------

    repo.insert_event(
        (
            f"Broker profile captured: account {account_id} on "
            f"{summary['server']} · offset "
            f"{'UNKNOWN' if offset is None else f'{offset:+d} min'}"
            f"{'' if stable else ' (UNSTABLE)'} · "
            f"{summary['margin_mode_label']} · "
            + ", ".join(
                f"{s['symbol']}={s['filling']}" for s in captured
            )
        ),
        level="WARN" if (offset is None or not stable or missing) else "INFO",
        category="BROKER",
        data=summary,
    )

    if missing:
        repo.insert_event(
            f"Symbols unavailable on this broker: {', '.join(missing)}. "
            f"They will error every cycle until the names are corrected "
            f"in SYMBOLS.",
            level="ERROR",
            category="BROKER",
            data={"symbols": missing},
        )

    for field, (old, new) in changes.items():
        repo.insert_event(
            f"Broker profile changed: {field} {old!r} -> {new!r}",
            level="WARN",
            category="BROKER",
            data={"field": field, "old": old, "new": new},
        )

    return summary


# ---------------------------------------------------------------------
# Read helpers for the rest of the engine
# ---------------------------------------------------------------------

def deviation_points(symbol_profile, price):
    """
    Convert DEVIATION_BPS into instrument-correct points.

    points = price × (bps / 10_000) / point

    Clamped to at least the broker's trade_stops_level and at most
    10× that floor, so a thin-spread symbol cannot end up with a
    deviation of zero and a wide one cannot accept unlimited slippage.
    """

    if not symbol_profile:
        return config.DEVIATION

    point = symbol_profile.get("point") or 0

    if not point or not price:
        return config.DEVIATION

    raw = (price * (config.DEVIATION_BPS / 10_000.0)) / point

    points = int(round(raw))

    stops_level = int(symbol_profile.get("trade_stops_level") or 0)

    floor = max(1, stops_level)

    # Clamp to [stops_level, 10 x stops_level]. When the broker
    # publishes no stops level there is nothing instrument-specific to
    # clamp against, so the bps figure stands on its own - imposing a
    # fixed point ceiling here would reintroduce exactly the
    # instrument-blindness this function exists to remove.
    if stops_level:
        return max(floor, min(points, stops_level * 10))

    return max(floor, points)
