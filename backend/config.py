import os

from dotenv import load_dotenv

load_dotenv()


def _env_float(name, default):
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return float(default)


def _env_int(name, default):
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return int(default)


def _env_bool(name, default=False):
    value = os.getenv(name)

    if value is None:
        return default

    return value.strip().lower() in {"1", "true", "yes", "on"}


# ---------------------------------------------------------------------
# Trading symbols
# ---------------------------------------------------------------------

SYMBOLS = [
    "EURUSDm",
    "GBPUSDm",
    "BTCUSDm",
    "XAUUSDm",
]

# Override with a comma-separated SYMBOLS env var if you want to change
# the universe without editing code.
if os.getenv("SYMBOLS"):
    SYMBOLS = [
        s.strip()
        for s in os.getenv("SYMBOLS").split(",")
        if s.strip()
    ]


# ---------------------------------------------------------------------
# Risk management (unchanged defaults)
# ---------------------------------------------------------------------

SL_PERCENT = _env_float("SL_PERCENT", 0.002)   # 0.20%
TP_PERCENT = _env_float("TP_PERCENT", 0.004)   # 0.40%

LOT_SIZE = _env_float("LOT_SIZE", 0.01)

MAGIC_NUMBER = _env_int("MAGIC_NUMBER", 100001)

# Legacy fixed deviation, in points. Retained only as the fallback for
# a symbol whose broker profile has not been captured yet.
DEVIATION = _env_int("DEVIATION", 20)

# Phase 1: deviation expressed in basis points of price, so it means
# the same thing on a 5-digit FX pair and a 2-digit crypto symbol.
# 20 points was 2 pips on EURUSD but $0.20 on BTCUSD - see Phase 0 §2.6.
DEVIATION_BPS = _env_float("DEVIATION_BPS", 5.0)


# ---------------------------------------------------------------------
# Optional risk gates
#
# Both default to OFF so the live trading behaviour is byte-for-byte
# what it was before persistence was added. Turn them on deliberately.
# ---------------------------------------------------------------------

# 0 = unlimited (previous behaviour: a new position every cycle).
MAX_OPEN_POSITIONS_PER_SYMBOL = _env_int("MAX_OPEN_POSITIONS_PER_SYMBOL", 0)

# 0 = no threshold (previous behaviour: any BUY/SELL executes).
MIN_SCORE_TO_TRADE = _env_int("MIN_SCORE_TO_TRADE", 0)


# ---------------------------------------------------------------------
# DeepSeek
#
# FIX: this previously read `DEEPSEEK_API_KEY = "API_KEY"`, i.e. the
# literal string, so every request authenticated with the word
# "API_KEY". The value now comes from the environment. DEEPSEEK_API_KEY
# is preferred; API_KEY is accepted as a fallback for existing .env
# files that use that name.
# ---------------------------------------------------------------------

API_KEY = os.getenv("API_KEY")

DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY") or API_KEY

DEEPSEEK_API_URL = os.getenv(
    "DEEPSEEK_API_URL",
    "https://api.deepseek.com/chat/completions",
)

DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")

DEEPSEEK_TIMEOUT = _env_int("DEEPSEEK_TIMEOUT", 30)

DEEPSEEK_TEMPERATURE = _env_float("DEEPSEEK_TEMPERATURE", 0.1)


# ---------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------

DEFAULT_INTERVAL = _env_int("DEFAULT_INTERVAL", 30)

# ---------------------------------------------------------------------
# Decision cadence (Phase 4)
#
# 'per_bar'  decide for a symbol only when its H1 bar advances.
# 'interval' the original behaviour, kept as an experiment arm (E2).
#
# The data changes hourly. At 30s the bot made ~120 decisions per bar
# on the same 34 candles - Phase 0 weakness #6: model noise, ~11,500
# API calls a day, and a microscalping profile both prop firms
# prohibit. per_bar gives ~96 decisions/day across four symbols.
# ---------------------------------------------------------------------

DECISION_MODE = os.getenv("DECISION_MODE", "per_bar").strip().lower()

if DECISION_MODE not in {"per_bar", "interval"}:
    DECISION_MODE = "per_bar"

# How often per_bar mode LOOKS for a new bar. Not how often it decides.
DECISION_POLL_SECONDS = _env_int("DECISION_POLL_SECONDS", 15)

# Minimum seconds between equity snapshots, so a 30s cycle over four
# symbols doesn't write four near-identical rows.
EQUITY_SNAPSHOT_MIN_SECONDS = _env_int("EQUITY_SNAPSHOT_MIN_SECONDS", 60)

# Whether a backend restart should automatically resume trading.
# Default OFF: a restart restores all HISTORY but leaves the engine
# stopped, so an unattended crash-loop cannot keep firing orders.
RESUME_ENGINE_ON_STARTUP = _env_bool("RESUME_ENGINE_ON_STARTUP", False)

# A PENDING trade row older than this is treated as orphaned by a crash
# and handed to the reconciler.
PENDING_TRADE_TIMEOUT_SECONDS = _env_int("PENDING_TRADE_TIMEOUT_SECONDS", 120)


# ---------------------------------------------------------------------
# AI memory (Phase 8)
# ---------------------------------------------------------------------

MEMORY_ENABLED = _env_bool("MEMORY_ENABLED", True)

# How many past experiences are injected into a prompt. Kept small on
# purpose - we retrieve relevant rows, never the whole database.
MEMORY_MAX_EXPERIENCES = _env_int("MEMORY_MAX_EXPERIENCES", 5)


# ---------------------------------------------------------------------
# News (Phase 11 extension point)
# ---------------------------------------------------------------------

NEWS_ENABLED = _env_bool("NEWS_ENABLED", False)

NEWS_PROVIDER = os.getenv("NEWS_PROVIDER", "null")

NEWS_API_KEY = os.getenv("NEWS_API_KEY")


# ---------------------------------------------------------------------
# Risk profile (Phase 3)
#
# Which rulebook this process runs under. The YAML lives in
# Specs/risk/<id>.yaml.
#
# Deliberately NOT inferred from the account number: pointing the demo
# profile at a funded account has to be a typed decision, not an
# accident of which terminal happened to be open.
# ---------------------------------------------------------------------

RISK_PROFILE = os.getenv("RISK_PROFILE", "mt5-demo")


# ---------------------------------------------------------------------
# API security (Phase 1)
#
# Phase 0 §2.1: POST /api/control started and stopped the engine, and
# could set a 1-second interval, with no authentication, on a process
# bound to 0.0.0.0. On a VPS that is a remote kill switch for anyone
# who can reach the port.
#
# Set API_TOKEN in .env to require a bearer token on every /api/* route
# except /api/health. Leave it unset for a purely local run - the
# server then refuses to bind anything but loopback unless you also set
# ALLOW_INSECURE_BIND=true.
# ---------------------------------------------------------------------

API_TOKEN = os.getenv("API_TOKEN")

AUTH_ENABLED = bool(API_TOKEN)

# Guard against running unauthenticated on a public interface.
ALLOW_INSECURE_BIND = _env_bool("ALLOW_INSECURE_BIND", False)


# ---------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------

# Consumed by database.connection.get_db_path(); defaults to
# database/quantbot.db next to the package.
DB_PATH = os.getenv("DB_PATH")
