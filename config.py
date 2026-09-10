"""
Central configuration, loaded from .env.

Every tunable the system has lives here. Nothing else reads os.getenv,
so there is exactly one place to look when behaviour surprises you.
"""

import os

from dotenv import load_dotenv

import MetaTrader5 as mt5


load_dotenv()


# =============================================================
# Typed readers
#
# A malformed .env should fail loudly at import, not silently trade
# with a default nobody chose.
# =============================================================

def _str(name, default=""):
    value = os.getenv(name)

    return default if value is None or value == "" else value.strip()


def _int(name, default):
    raw = os.getenv(name)

    if raw is None or raw.strip() == "":
        return default

    try:
        return int(float(raw.strip()))
    except ValueError:
        raise ValueError(
            f"{name} must be a whole number, got {raw!r}"
        ) from None


def _float(name, default):
    raw = os.getenv(name)

    if raw is None or raw.strip() == "":
        return default

    try:
        return float(raw.strip())
    except ValueError:
        raise ValueError(
            f"{name} must be a number, got {raw!r}"
        ) from None


# =============================================================
# DeepSeek
# =============================================================

DEEPSEEK_API_KEY = _str("DEEPSEEK_API_KEY")

DEEPSEEK_API_BASE = _str("DEEPSEEK_API_BASE", "https://api.deepseek.com")

DEEPSEEK_MODEL = _str("DEEPSEEK_MODEL", "deepseek-chat")

DEEPSEEK_TIMEOUT = _int("DEEPSEEK_TIMEOUT", 60)


# =============================================================
# MetaTrader 5
# =============================================================

# 0 means "attach to whatever account the terminal is already logged
# into" rather than forcing a login.
MT5_LOGIN = _int("MT5_LOGIN", 0)

MT5_PASSWORD = _str("MT5_PASSWORD")

MT5_SERVER = _str("MT5_SERVER")

# None, not "", so mt5.initialize() can be called without a path at all.
MT5_PATH = _str("MT5_PATH") or None


# =============================================================
# Risk and cadence
# =============================================================

DEFAULT_RISK_PERCENT = _float("DEFAULT_RISK_PERCENT", 1.0)

SCAN_INTERVAL_SECONDS = _int("SCAN_INTERVAL_SECONDS", 30)

CONFIDENCE_THRESHOLD = _int("CONFIDENCE_THRESHOLD", 65)

MAGIC_NUMBER = _int("MAGIC_NUMBER", 100001)

DEVIATION = _int("DEVIATION", 20)


# =============================================================
# Universe
# =============================================================

SYMBOLS = [
    part.strip()
    for part in _str(
        "SYMBOLS",
        "EURUSDm,GBPUSDm,USDJPYm,USDCHFm,USDCADm,"
        "AUDUSDm,NZDUSDm,BTCUSDm,XAUUSDm",
    ).split(",")
    if part.strip()
]


# =============================================================
# Timeframes and indicators
# =============================================================

TIMEFRAMES = [mt5.TIMEFRAME_H1, mt5.TIMEFRAME_D1]

BARS_TO_FETCH = _int("BARS_TO_FETCH", 250)

EMA_PERIOD = 200

RSI_PERIOD = 14

ATR_PERIOD = 14

VOL_MA_PERIOD = 20

# Fallback stop geometry when the model returns stops that are not
# arranged correctly around the entry price.
ATR_SL_MULTIPLIER = _float("ATR_SL_MULTIPLIER", 1.5)

ATR_TP_MULTIPLIER = _float("ATR_TP_MULTIPLIER", 3.0)


# =============================================================
# Self-learning auditor
# =============================================================

AUDIT_TRADE_THRESHOLD = _int("AUDIT_TRADE_THRESHOLD", 10)

AUDIT_COOLDOWN_HOURS = _int("AUDIT_COOLDOWN_HOURS", 12)

# The auditor may never suggest a penalty outside this band. A rule
# that could zero out a score would let one bad week silently disable a
# symbol forever.
MIN_CONFIDENCE_PENALTY = 15

MAX_CONFIDENCE_PENALTY = 30

# How many closed trades one audit looks at.
AUDIT_LOOKBACK_TRADES = 50

# Below this many closed trades there is nothing to learn from.
AUDIT_MIN_TRADES = 3


# =============================================================
# Paths
#
# Resolved from __file__ so the app behaves the same whatever
# directory it is launched from.
# =============================================================

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

MEMORY_FILE = os.path.join(BASE_DIR, "memory.json")

RULES_FILE = os.path.join(BASE_DIR, "new_rules.json")
