"""
Strategy identity and versioning (Phase 1).

The strategy in this system is not a code path - it is a prompt, a
model, a temperature and a set of parameters. Phase 0 §2.9 recorded the
consequence: two runs of "the same" strategy on different days were not
comparable, because nothing recorded what the strategy actually was.

This module fixes that. It computes a stable hash over the rendered
system prompt plus the user-prompt TEMPLATE (not the market data, which
changes every bar), registers that hash as a row in strategy_versions,
and hands the resulting id to every decision.

Hash stability contract
-----------------------
The hash must change when, and only when, the strategy changes:

  * it covers the system prompt and the user-prompt skeleton
  * it EXCLUDES candle CSVs, feature values, experiences and news, all
    of which differ per call
  * it includes the model alias, temperature, and the parameters that
    materially shape a decision

Changing the prompt text therefore produces a new hash, which produces
a new version row, which every subsequent decision references.
"""

import hashlib
import json

from backend import config
from backend.ai.prompts import build_system_prompt, build_user_prompt


# The strategy this codebase implements: an LLM reading multi-timeframe
# bars. Bump VERSION by hand when the prompt or params change meaning.
STRATEGY_ID = "llm-mtf"

STRATEGY_VERSION = "1.0.0"


# A fixed symbol used only to render the prompt for hashing. The prompt
# text is symbol-parameterised, so hashing with a real symbol would make
# the hash differ per instrument. This placeholder keeps one hash for
# one strategy.
_HASH_SYMBOL = "__TEMPLATE__"


# Market data placeholders. These are constant, so the rendered
# template is identical on every call - only the prompt skeleton is
# being hashed, never the market state.
_HASH_MARKET_DATA = {
    "daily_csv": "__DAILY_CSV__",
    "hourly_csv": "__HOURLY_CSV__",
    "features": None,
}


def _params_snapshot():
    """
    The config values that materially change a decision.

    Deliberately narrow: adding an unrelated key here would churn the
    hash and orphan the version history for no reason.
    """

    return {
        "model_alias": config.DEEPSEEK_MODEL,
        "temperature": config.DEEPSEEK_TEMPERATURE,
        "timeframes": "D1x10+H1x24",
        "sl_percent": config.SL_PERCENT,
        "tp_percent": config.TP_PERCENT,
        "lot_size": config.LOT_SIZE,
        "min_score_to_trade": config.MIN_SCORE_TO_TRADE,
        "max_open_positions_per_symbol": config.MAX_OPEN_POSITIONS_PER_SYMBOL,
        "memory_enabled": config.MEMORY_ENABLED,
        "memory_max_experiences": config.MEMORY_MAX_EXPERIENCES,
        "news_enabled": config.NEWS_ENABLED,
    }


def render_prompt_template():
    """
    The exact text that gets hashed.

    Rendered with placeholders so it is byte-identical between calls
    and between processes.
    """

    system = build_system_prompt(_HASH_SYMBOL)

    user = build_user_prompt(
        _HASH_SYMBOL,
        _HASH_MARKET_DATA,
        experience_context=None,
        news_context=None,
    )

    return system, user


def compute_prompt_hash():
    """
    sha256 over the system prompt, the user-prompt template and the
    parameter snapshot.

    Stable across processes: json.dumps is sorted, and no clock, path
    or random value contributes.
    """

    system, user = render_prompt_template()

    payload = json.dumps(
        {
            "strategy_id": STRATEGY_ID,
            "system_prompt": system,
            "user_prompt_template": user,
            "params": _params_snapshot(),
        },
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )

    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def current_descriptor():
    """Everything needed to identify or register the running strategy."""

    return {
        "strategy_id": STRATEGY_ID,
        "version": STRATEGY_VERSION,
        "prompt_hash": compute_prompt_hash(),
        "model_alias": config.DEEPSEEK_MODEL,
        "temperature": config.DEEPSEEK_TEMPERATURE,
        "params": _params_snapshot(),
    }


# ---------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------

# Resolved once per process; the prompt cannot change while running.
_cached_version_id = None


def ensure_registered(force=False):
    """
    Make sure the running strategy exists in strategy_versions and
    return its id.

    Three cases:

      1. (strategy_id, version) exists with the SAME hash
         -> reuse it.
      2. (strategy_id, version) exists with a DIFFERENT hash
         -> the prompt changed without a version bump. Register a new
            row versioned `<version>+hash.<8 chars>` rather than
            silently reusing an id that means something else.
      3. nothing exists
         -> insert it.

    Case 2 is the important one: it makes an unversioned prompt edit
    visible in the data instead of corrupting an existing version's
    trade population.
    """

    global _cached_version_id

    # Imported here so this module stays importable without a database
    # (the hashing functions are used by tests that never open one).
    from backend.database import repo_meta

    if _cached_version_id is not None and not force:

        # Confirm the id still resolves in the CURRENT database before
        # handing it out. decisions.strategy_version_id is a foreign
        # key: returning a stale id from a previous database would make
        # every insert_decision() raise IntegrityError, and the decision
        # would be lost rather than merely unversioned.
        if repo_meta.get_strategy_version_by_id(_cached_version_id):
            return _cached_version_id

        _cached_version_id = None

    descriptor = current_descriptor()

    existing = repo_meta.get_strategy_version(
        descriptor["strategy_id"], descriptor["version"]
    )

    if existing and existing["prompt_hash"] == descriptor["prompt_hash"]:
        _cached_version_id = existing["id"]
        return _cached_version_id

    if existing:
        # The prompt moved but the version label did not. Do not
        # overwrite: derive a distinct label so both populations stay
        # separable in analytics.
        descriptor["version"] = (
            f"{descriptor['version']}+hash.{descriptor['prompt_hash'][:8]}"
        )

        drifted = repo_meta.get_strategy_version(
            descriptor["strategy_id"], descriptor["version"]
        )

        if drifted:
            _cached_version_id = drifted["id"]
            return _cached_version_id

        descriptor["parent_version_id"] = existing["id"]

    _cached_version_id = repo_meta.insert_strategy_version(descriptor)

    return _cached_version_id


def reset_cache():
    """Drop the memoised id. Used by tests that swap databases."""

    global _cached_version_id

    _cached_version_id = None
