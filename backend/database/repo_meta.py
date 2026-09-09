"""
Strategy versions, broker profiles and symbol profiles (Phase 1).

These three tables answer "what exactly produced this row?" - which
strategy, against which broker, with which instrument specification.
Everything here is written once at startup (or on change) and read many
times, so the functions are upsert-shaped rather than append-only.
"""

import json

from ._common import _insert, _json, _rows, utc_now
from .connection import get_connection, write_lock


# =====================================================================
# STRATEGY VERSIONS
# =====================================================================

def insert_strategy_version(descriptor):
    """Register a strategy version. Returns the new row id."""

    return _insert("strategy_versions", {
        "created_at": utc_now(),
        "strategy_id": descriptor["strategy_id"],
        "version": descriptor["version"],
        "prompt_hash": descriptor["prompt_hash"],
        "model_alias": descriptor.get("model_alias"),
        "temperature": descriptor.get("temperature"),
        "params_json": _json(descriptor.get("params")),
        "parent_version_id": descriptor.get("parent_version_id"),
    })


def get_strategy_version(strategy_id, version):
    row = get_connection().execute(
        "SELECT * FROM strategy_versions "
        "WHERE strategy_id = ? AND version = ?",
        (strategy_id, version),
    ).fetchone()

    return dict(row) if row else None


def get_strategy_version_by_id(version_id):
    row = get_connection().execute(
        "SELECT * FROM strategy_versions WHERE id = ?", (version_id,)
    ).fetchone()

    return dict(row) if row else None


def get_strategy_versions(limit=100):
    return _rows(get_connection().execute(
        "SELECT * FROM strategy_versions ORDER BY id DESC LIMIT ?",
        (limit,),
    ))


# =====================================================================
# BROKER PROFILE (one row per account)
# =====================================================================

BROKER_FIELDS = (
    "server", "company", "currency", "leverage",
    "margin_mode", "margin_mode_label",
    "server_utc_offset_min", "offset_samples_json", "offset_stable",
    "trade_allowed", "terminal_json",
)


def upsert_broker_profile(profile):
    """
    Insert or update the profile for one account.

    Returns (row_id, changes) where `changes` maps field -> (old, new)
    for every value that moved. Startup logs those, because a changed
    server_utc_offset_min (DST) or margin_mode is operationally
    significant, not routine.
    """

    account_id = profile["account_id"]

    existing = get_broker_profile(account_id)

    data = {field: profile.get(field) for field in BROKER_FIELDS}

    conn = get_connection()

    if existing is None:
        row_id = _insert("broker_profiles", {
            "created_at": utc_now(),
            "updated_at": utc_now(),
            "account_id": account_id,
            **data,
        })

        return row_id, {}

    changes = {
        field: (existing.get(field), value)
        for field, value in data.items()
        if existing.get(field) != value
        and field not in {"offset_samples_json", "terminal_json"}
    }

    assignments = ", ".join(f'"{f}" = ?' for f in data)

    with write_lock:
        conn.execute(
            f"UPDATE broker_profiles SET {assignments}, updated_at = ? "
            f"WHERE account_id = ?",
            [*data.values(), utc_now(), account_id],
        )

    return existing["id"], changes


def get_broker_profile(account_id):
    row = get_connection().execute(
        "SELECT * FROM broker_profiles WHERE account_id = ?", (account_id,)
    ).fetchone()

    return dict(row) if row else None


def get_broker_profiles():
    return _rows(get_connection().execute(
        "SELECT * FROM broker_profiles ORDER BY account_id"
    ))


def get_server_utc_offset_min(account_id, default=None):
    """
    The measured broker offset, or `default` when it has never been
    measured.

    Callers must treat None as "unknown" rather than assuming zero -
    Phase 0 §2.2 is exactly the bug that assumption caused.
    """

    profile = get_broker_profile(account_id)

    if not profile:
        return default

    value = profile.get("server_utc_offset_min")

    return default if value is None else value


# =====================================================================
# SYMBOL PROFILES (one row per account + symbol)
# =====================================================================

SYMBOL_FIELDS = (
    "digits", "point", "contract_size",
    "trade_tick_size", "trade_tick_value",
    "trade_tick_value_profit", "trade_tick_value_loss",
    "volume_min", "volume_step", "volume_max",
    "filling_mode", "filling_mode_chosen", "filling_order_type",
    "trade_stops_level", "trade_freeze_level", "spread", "trade_mode",
    "swap_long", "swap_short",
)


def upsert_symbol_profile(profile):
    """Insert or update one account+symbol specification."""

    account_id = profile["account_id"]
    symbol = profile["symbol"]

    existing = get_symbol_profile(account_id, symbol)

    data = {field: profile.get(field) for field in SYMBOL_FIELDS}

    if existing is None:
        return _insert("symbol_profiles", {
            "created_at": utc_now(),
            "updated_at": utc_now(),
            "account_id": account_id,
            "symbol": symbol,
            **data,
        })

    assignments = ", ".join(f'"{f}" = ?' for f in data)

    conn = get_connection()

    with write_lock:
        conn.execute(
            f"UPDATE symbol_profiles SET {assignments}, updated_at = ? "
            f"WHERE account_id = ? AND symbol = ?",
            [*data.values(), utc_now(), account_id, symbol],
        )

    return existing["id"]


def get_symbol_profile(account_id, symbol):
    row = get_connection().execute(
        "SELECT * FROM symbol_profiles WHERE account_id = ? AND symbol = ?",
        (account_id, symbol),
    ).fetchone()

    return dict(row) if row else None


def get_symbol_profiles(account_id=None):
    if account_id is None:
        return _rows(get_connection().execute(
            "SELECT * FROM symbol_profiles ORDER BY account_id, symbol"
        ))

    return _rows(get_connection().execute(
        "SELECT * FROM symbol_profiles WHERE account_id = ? ORDER BY symbol",
        (account_id,),
    ))


# =====================================================================
# RISK PROFILES (Phase 3)
# =====================================================================

def upsert_risk_profile(profile):
    """
    Snapshot a risk profile, keyed on (profile_id, checksum).

    An edited YAML has a different checksum and so becomes a NEW row.
    Historical decisions keep pointing at the limits that were actually
    in force when they were made - overwriting would silently rewrite
    the rules a past trade was judged against.
    """

    existing = get_risk_profile(profile["profile_id"], profile["checksum"])

    if existing:
        return existing["id"]

    return _insert("risk_profiles", {
        "created_at": utc_now(),
        "profile_id": profile["profile_id"],
        "name": profile.get("name"),
        "checksum": profile["checksum"],
        "source_path": profile.get("source_path"),
        "params_json": _json(profile.get("params")),
    })


def get_risk_profile(profile_id, checksum):
    row = get_connection().execute(
        "SELECT * FROM risk_profiles WHERE profile_id = ? AND checksum = ?",
        (profile_id, checksum),
    ).fetchone()

    return dict(row) if row else None


def get_risk_profile_by_id(row_id):
    row = get_connection().execute(
        "SELECT * FROM risk_profiles WHERE id = ?", (row_id,)
    ).fetchone()

    return dict(row) if row else None


def get_risk_profiles(limit=50):
    return _rows(get_connection().execute(
        "SELECT * FROM risk_profiles ORDER BY id DESC LIMIT ?", (limit,)
    ))


def decode_json(value, default=None):
    """Helper for callers reading the *_json columns back."""

    if not value:
        return default

    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default
