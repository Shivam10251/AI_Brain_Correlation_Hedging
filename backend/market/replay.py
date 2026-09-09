"""
Bar snapshots and offline replay (Phase 4).

Why snapshot at all
-------------------
Phase 8 needs to ask "what would a different stop rule, score threshold
or feature formula have done?" over real history. Without the exact
bars each decision saw, that question can only be answered by refetching
from MT5 - which serves a moving window, may have revised the data, and
costs an API call per decision. Storing 34 rows per decision makes the
replay exact and free.

What is stored
--------------
The RAW MT5 rows: epoch seconds in server time, unrounded prices. Not
the DataFrame, not the CSV the model saw, and not the derived features.
Anything derived can be recomputed; anything rounded cannot be
un-rounded.

The features that were actually stored go in too - not because they are
needed to replay, but so a replay can be CHECKED against them. A replay
engine that silently disagrees with history is worse than none.
"""

import json

import pandas as pd

from backend.market import data_engine


# The MT5 rate fields, in order. Stored explicitly rather than relying
# on numpy's dtype names surviving a round trip.
RATE_FIELDS = (
    "time", "open", "high", "low", "close",
    "tick_volume", "spread", "real_volume",
)


def rates_to_records(rates):
    """
    MT5 rate rows -> a list of plain dicts.

    Accepts a numpy structured array (what MT5 returns) or anything
    iterable of mappings, so tests can hand it plain data.
    """

    if rates is None:
        return []

    records = []

    for row in rates:

        record = {}

        for field in RATE_FIELDS:
            try:
                value = row[field]
            except (KeyError, IndexError, TypeError, ValueError):
                continue

            # numpy scalars do not survive json.dumps.
            record[field] = value.item() if hasattr(value, "item") else value

        records.append(record)

    return records


def records_to_frame(records, offset_minutes=None):
    """
    Rebuild the DataFrame that compute_features expects.

    Mirrors fetch_multi_timeframe_data exactly: a server-time column
    and a UTC `time` column. Any divergence here would make a replay
    disagree with history for a reason that has nothing to do with the
    rule being tested.
    """

    frame = pd.DataFrame(records)

    if frame.empty:
        return frame

    frame["time_server"] = pd.to_datetime(frame["time"], unit="s", utc=True)

    frame["time"] = data_engine.to_utc(frame["time"], offset_minutes)

    return frame


class _Tick:
    """Minimal stand-in for an MT5 tick, rebuilt from a snapshot."""

    def __init__(self, payload):
        payload = payload or {}

        self.bid = payload.get("bid")
        self.ask = payload.get("ask")
        self.time = payload.get("time")


class _SymbolInfo:
    """Minimal stand-in for MT5 symbol_info."""

    def __init__(self, payload):
        payload = payload or {}

        self.name = payload.get("name")
        self.digits = payload.get("digits")
        self.point = payload.get("point")


def tick_to_record(tick):
    if tick is None:
        return None

    return {
        "bid": getattr(tick, "bid", None),
        "ask": getattr(tick, "ask", None),
        "time": getattr(tick, "time", None),
    }


def symbol_info_to_record(symbol_info):
    if symbol_info is None:
        return None

    return {
        "name": getattr(symbol_info, "name", None),
        "digits": getattr(symbol_info, "digits", None),
        "point": getattr(symbol_info, "point", None),
    }


# ---------------------------------------------------------------------
# Replay
# ---------------------------------------------------------------------

def replay_features(snapshot):
    """
    Recompute features from a stored snapshot.

    `snapshot` is a decision_bars row (dict). Returns the features
    dict, which must equal the stored one for an unchanged
    FEATURE_VERSION - that equality is the Phase 4 acceptance
    criterion, and the guarantee Phase 8 is built on.
    """

    offset = snapshot.get("server_utc_offset_min")

    daily = records_to_frame(_decode(snapshot["daily_json"]), offset)
    hourly = records_to_frame(_decode(snapshot["hourly_json"]), offset)

    if daily.empty or hourly.empty:
        raise ValueError("Snapshot has no bars to replay")

    return data_engine.compute_features(
        daily,
        hourly,
        _Tick(_decode(snapshot.get("tick_json"))),
        _SymbolInfo(_decode(snapshot.get("symbol_info_json"))),
        offset_minutes=offset,
    )


def compare_to_stored(snapshot, tolerance=1e-9):
    """
    Replay a snapshot and diff it against the features that were
    stored at the time.

    Returns (matches, differences). Floats are compared with a
    tolerance because a round trip through JSON is not bit-exact; every
    other type must match exactly.
    """

    stored = _decode(snapshot.get("features_json")) or {}

    replayed = replay_features(snapshot)

    differences = {}

    for key in set(stored) | set(replayed):

        old = stored.get(key)
        new = replayed.get(key)

        if isinstance(old, (int, float)) and isinstance(new, (int, float)) \
                and not isinstance(old, bool) and not isinstance(new, bool):

            if abs(float(old) - float(new)) > tolerance:
                differences[key] = (old, new)

            continue

        if old != new:
            differences[key] = (old, new)

    return (not differences), differences


def _decode(value):
    if value is None:
        return None

    if isinstance(value, (dict, list)):
        return value

    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return None
