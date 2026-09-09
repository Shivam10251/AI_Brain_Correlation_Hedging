"""
Risk profiles: the per-account rulebook (Phase 3).

A profile is data, not code. The5ers' 3% daily limit and the demo
account's 2% are the same CHECK with a different number, so changing
firm should never mean changing a code path.

Profiles live as YAML under Specs/risk/ so they are reviewable in a
diff, and are snapshotted into the risk_profiles table on load so a
decision can always be traced to the exact limits that were in force
when it was made.

Validation is deliberately loud. A profile whose limits contradict
each other - a per-trade risk larger than the whole daily allowance,
say - would let the engine approve a trade that cannot be survived,
so loading it is refused rather than clamped.
"""

import hashlib
import json
from pathlib import Path

import yaml

from backend import config


PROFILE_DIR = Path(__file__).resolve().parent.parent.parent / "Specs" / "risk"

DEFAULT_PROFILE_ID = "mt5-demo"


# ---------------------------------------------------------------------
# Defaults
#
# Every key the engine reads has a default here, so a profile file that
# predates a new check still loads. The defaults are the PERMISSIVE
# end - a missing key must not silently tighten a limit and make the
# engine refuse trades for a reason nobody wrote down.
# ---------------------------------------------------------------------

DEFAULTS = {
    "profile_id": DEFAULT_PROFILE_ID,
    "name": "unnamed",
    "description": "",
    "account_size": None,

    "min_ai_score": 0,

    "max_open_positions_per_symbol": 0,      # 0 = unlimited
    "allow_hedging": False,

    "risk_pct": 0.25,
    "max_risk_per_trade_pct": 0.25,

    "daily_loss_limit_pct": 0.0,             # 0 = no daily limit
    "daily_loss_buffer_pct": 0.0,
    "max_losses_per_day": 0,                 # 0 = unlimited

    "max_loss_floor": None,
    "max_loss_floor_buffer": 0.0,

    "max_spread_pct_of_stop": 0.0,           # 0 = no spread gate

    "max_net_exposure_r": 0.0,               # 0 = no exposure cap
    "exposure_weights": {},

    "news_blackout_pre_min": 0,
    "news_blackout_post_min": 0,
    "news_min_importance": "high",

    "min_holding_bars": 0,
    "flatten_before_weekend_min": 0,
    "trading_windows": [],
}


class ProfileError(ValueError):
    """A profile that cannot be trusted to gate real money."""


# ---------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------

def validate(profile):
    """
    Reject a profile whose limits contradict each other.

    Raises ProfileError listing every problem, rather than the first,
    so a misconfigured file is fixed in one pass.
    """

    problems = []

    def number(key):
        value = profile.get(key)
        return None if value is None else float(value)

    for key in (
        "min_ai_score", "risk_pct", "max_risk_per_trade_pct",
        "daily_loss_limit_pct", "daily_loss_buffer_pct",
        "max_spread_pct_of_stop", "max_net_exposure_r",
        "max_loss_floor_buffer", "news_blackout_pre_min",
        "news_blackout_post_min", "min_holding_bars",
    ):
        value = number(key)

        if value is not None and value < 0:
            problems.append(f"{key} must not be negative (got {value})")

    if not 0 <= float(profile.get("min_ai_score") or 0) <= 100:
        problems.append("min_ai_score must be between 0 and 100")

    risk = number("max_risk_per_trade_pct") or 0.0
    daily = number("daily_loss_limit_pct") or 0.0
    buffer_pct = number("daily_loss_buffer_pct") or 0.0

    # The core contradiction: one trade that can breach the whole day.
    if daily > 0 and risk > daily:
        problems.append(
            f"max_risk_per_trade_pct ({risk}) exceeds daily_loss_limit_pct "
            f"({daily}): a single trade could breach the daily limit"
        )

    if daily > 0 and buffer_pct >= daily:
        problems.append(
            f"daily_loss_buffer_pct ({buffer_pct}) must be smaller than "
            f"daily_loss_limit_pct ({daily}), else no trade is ever allowed"
        )

    if daily > 0 and risk > 0:
        usable = daily - buffer_pct

        if usable > 0 and risk > usable:
            problems.append(
                f"max_risk_per_trade_pct ({risk}) exceeds the usable daily "
                f"allowance ({usable}) after the buffer"
            )

    losses = profile.get("max_losses_per_day") or 0

    if losses and daily > 0 and risk > 0:
        # Not fatal, but worth refusing: the loss counter would never
        # bite because the daily limit always fires first.
        if losses * risk > daily * 3:
            problems.append(
                f"max_losses_per_day ({losses}) x max_risk_per_trade_pct "
                f"({risk}) is far beyond daily_loss_limit_pct ({daily}); "
                f"the loss counter can never take effect"
            )

    floor = profile.get("max_loss_floor")
    size = profile.get("account_size")

    if floor is not None and size is not None and float(floor) >= float(size):
        problems.append(
            f"max_loss_floor ({floor}) must be below account_size ({size})"
        )

    if profile.get("allow_hedging"):
        problems.append(
            "allow_hedging must be false: both target firms prohibit it, "
            "and the engine has no netting-aware position model"
        )

    weights = profile.get("exposure_weights") or {}

    if not isinstance(weights, dict):
        problems.append("exposure_weights must be a mapping")
    else:
        for symbol, weight in weights.items():
            try:
                float(weight)
            except (TypeError, ValueError):
                problems.append(
                    f"exposure_weights[{symbol}] is not a number: {weight!r}"
                )

    if problems:
        raise ProfileError(
            f"Risk profile {profile.get('profile_id')!r} is invalid:\n  - "
            + "\n  - ".join(problems)
        )

    return profile


# ---------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------

_cache = {}


def profile_path(profile_id):
    return PROFILE_DIR / f"{profile_id}.yaml"


def load(profile_id=None, force=False):
    """
    Load and validate a profile by id.

    Cached per process; the file cannot change while running unless a
    caller asks for a reload.
    """

    profile_id = profile_id or active_profile_id()

    if not force and profile_id in _cache:
        return _cache[profile_id]

    path = profile_path(profile_id)

    if not path.exists():
        raise ProfileError(
            f"No risk profile {profile_id!r} at {path}. "
            f"Available: {', '.join(available()) or 'none'}"
        )

    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    if not isinstance(raw, dict):
        raise ProfileError(f"{path} did not parse to a mapping")

    profile = {**DEFAULTS, **raw}

    profile["profile_id"] = raw.get("profile_id", profile_id)

    if profile["profile_id"] != profile_id:
        raise ProfileError(
            f"{path} declares profile_id {profile['profile_id']!r} but is "
            f"named {profile_id!r}"
        )

    validate(profile)

    profile["_source_path"] = str(path)
    profile["_checksum"] = checksum(profile)

    _cache[profile_id] = profile

    return profile


def available():
    if not PROFILE_DIR.exists():
        return []

    return sorted(p.stem for p in PROFILE_DIR.glob("*.yaml"))


def checksum(profile):
    """Stable hash of the limits, ignoring bookkeeping keys."""

    payload = {
        k: v for k, v in profile.items()
        if not k.startswith("_") and k not in {"name", "description"}
    }

    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


def active_profile_id():
    """
    Which profile this process runs under.

    RISK_PROFILE in .env, defaulting to the demo profile. Deliberately
    NOT inferred from the account number: pointing a demo profile at a
    funded account has to be a typed decision.
    """

    return getattr(config, "RISK_PROFILE", None) or DEFAULT_PROFILE_ID


def reset_cache():
    _cache.clear()


# ---------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------

def ensure_registered(profile):
    """
    Snapshot the profile into risk_profiles and return its row id.

    A decision must be traceable to the exact limits in force when it
    was made, so an edited YAML registers a NEW row rather than
    mutating the old one.
    """

    from backend.database import repo_meta

    return repo_meta.upsert_risk_profile({
        "profile_id": profile["profile_id"],
        "name": profile.get("name"),
        "checksum": profile["_checksum"],
        "source_path": profile.get("_source_path"),
        "params": {
            k: v for k, v in profile.items() if not k.startswith("_")
        },
    })


# ---------------------------------------------------------------------
# Derived limits
# ---------------------------------------------------------------------

def daily_loss_basis(profile, day_summary, account):
    """
    The figure the daily percentage is taken from.

    The5ers computes 3% of max(previous EOD equity, balance).
    `day_summary.start_equity` IS the previous EOD equity, because it
    is stamped on the day's first cycle.
    """

    candidates = [
        day_summary.get("start_equity"),
        account.get("balance"),
    ]

    values = [float(c) for c in candidates if c is not None]

    if not values:
        return float(account.get("equity") or 0.0)

    return max(values)


def daily_loss_allowance(profile, day_summary, account):
    """
    How much may still be lost today, as a positive number.

    The buffer is subtracted from the limit, so the engine refuses
    before the firm's real line rather than at it.
    """

    limit_pct = float(profile.get("daily_loss_limit_pct") or 0.0)

    if limit_pct <= 0:
        return None                       # no daily limit configured

    buffer_pct = float(profile.get("daily_loss_buffer_pct") or 0.0)

    basis = daily_loss_basis(profile, day_summary, account)

    return basis * max(0.0, limit_pct - buffer_pct) / 100.0


def risk_budget(profile, account):
    """Currency amount one trade may put at risk."""

    pct = float(profile.get("max_risk_per_trade_pct") or 0.0)

    if pct <= 0:
        return None

    balance = float(account.get("balance") or account.get("equity") or 0.0)

    return balance * pct / 100.0


def effective_floor(profile):
    """The equity level the engine refuses to trade below."""

    floor = profile.get("max_loss_floor")

    if floor is None:
        return None

    return float(floor) + float(profile.get("max_loss_floor_buffer") or 0.0)
