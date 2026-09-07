"""
End-to-end check of persistence + restart recovery, with MT5 and
DeepSeek both faked.

Run from the project root:

    python tests/test_restart_recovery.py

It covers Phase 15 steps 1-10 and 13-14. Steps 11-12 (closing only the
browser) are a manual browser check and step 9's live MT5 reconciliation
must be repeated against a real terminal.
"""

import os
import shutil
import sys
import tempfile

# The bot modules live in the project root.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tests.fake_mt5 as fake_mt5                   # noqa: E402

fake_mt5.install()


# ---------------------------------------------------------------------
# Isolated database + deterministic config
# ---------------------------------------------------------------------

TEMP_DIR = tempfile.mkdtemp(prefix="quantbot-test-")

os.environ["DB_PATH"] = os.path.join(TEMP_DIR, "test.db")
os.environ["DEEPSEEK_API_KEY"] = "test-key"
os.environ["SYMBOLS"] = "EURUSDm,GBPUSDm"
os.environ["EQUITY_SNAPSHOT_MIN_SECONDS"] = "0"
os.environ["PENDING_TRADE_TIMEOUT_SECONDS"] = "0"


PASSED = []
FAILED = []


def check(name, condition, detail=""):
    if condition:
        PASSED.append(name)
        print(f"  PASS  {name}")
    else:
        FAILED.append(f"{name} {detail}")
        print(f"  FAIL  {name} {detail}")


# ---------------------------------------------------------------------
# Fake DeepSeek
# ---------------------------------------------------------------------

class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


SIGNAL_SCRIPT = []


def fake_post(url, headers=None, json=None, timeout=None):
    """Return the next scripted AI response."""

    import json as json_module

    content = SIGNAL_SCRIPT.pop(0) if SIGNAL_SCRIPT else {
        "daily_trend": "bullish",
        "h1_trend": "bullish",
        "momentum": "strong",
        "market_structure": "higher_highs",
        "market_regime": "trending",
        "setup": "breakout",
        "score": 82,
        "signal": "HOLD",
        "daily_analysis": "Daily is constructive.",
        "h1_analysis": "H1 is aligned.",
        "reasoning": "Test reasoning sentence one. Test reasoning sentence two.",
    }

    body = (
        content
        if isinstance(content, str)
        else json_module.dumps(content)
    )

    return FakeResponse({
        "choices": [{"message": {"content": body}}]
    })


def boot():
    """
    Simulate a fresh process: reimport every bot module so nothing
    carries over in memory. This is what makes the restart test real.
    """

    for name in [
        "backend", "backend.config",
        "backend.database", "backend.database.connection",
        "backend.database.repository", "backend.database.analytics",
        "backend.ai", "backend.ai.brain", "backend.ai.memory",
        "backend.market", "backend.market.news",
        "backend.market.data_engine", "backend.market.execution",
        "backend.core", "backend.core.engine", "backend.core.runtime",
        "backend.core.reconciler",
    ]:
        sys.modules.pop(name, None)

    import requests

    requests.post = fake_post

    from backend.core import engine as engine_module

    from backend.database import initialize_database

    initialize_database()

    engine_module.connect_mt5()

    return engine_module


def run_cycle(engine_module, symbols=None):
    """Run one full cycle synchronously."""

    import asyncio

    from backend import config as config_module

    async def cycle():
        for symbol in (symbols or config_module.SYMBOLS):
            await engine_module.process_symbol(symbol, "test-cycle")

    asyncio.run(cycle())


# =====================================================================
# SESSION 1
# =====================================================================

print("\n=== SESSION 1: generate decisions, trades and equity ===")

engine = boot()

check("database initialises", os.path.exists(os.environ["DB_PATH"]))

from backend.database import repository as repo   # noqa: E402

# One BUY, then a HOLD.
SIGNAL_SCRIPT.extend([
    {
        "daily_trend": "bullish", "h1_trend": "bullish",
        "momentum": "strong", "market_structure": "higher_highs",
        "market_regime": "trending", "setup": "breakout",
        "score": 82, "signal": "BUY",
        "daily_analysis": "Daily uptrend intact.",
        "h1_analysis": "H1 momentum confirms.",
        "reasoning": "Structure is bullish. Momentum confirms the break.",
    },
    {
        "daily_trend": "neutral", "h1_trend": "neutral",
        "momentum": "neutral", "market_structure": "ranging",
        "market_regime": "ranging", "setup": "none",
        "score": 30, "signal": "HOLD",
        "daily_analysis": "Daily is ambiguous.",
        "h1_analysis": "H1 is flat.",
        "reasoning": "No directional edge. Staying flat.",
    },
])

run_cycle(engine)

decisions = repo.get_decisions(limit=50)
trades = repo.get_trades(limit=50)
equity = repo.get_equity_snapshots(limit=50)
market = repo.get_latest_market_state_per_symbol()

check("decisions persisted", len(decisions) == 2, f"got {len(decisions)}")
check("trade persisted", len(trades) == 1, f"got {len(trades)}")
check("equity snapshots persisted", len(equity) >= 1, f"got {len(equity)}")
check("market states persisted", len(market) == 2, f"got {len(market)}")

check(
    "trade is EXECUTED",
    trades[0]["execution_status"] == "EXECUTED",
    trades[0]["execution_status"],
)

check(
    "trade links to its decision",
    trades[0]["decision_id"] is not None,
)

check(
    "decision links back to its trade",
    any(d["trade_id"] == trades[0]["id"] for d in decisions),
)

check(
    "structured AI fields stored",
    decisions[-1]["market_regime"] == "trending"
    and decisions[-1]["setup"] == "breakout",
)

check(
    "HOLD decision recorded with no trade",
    any(d["ai_signal"] == "HOLD" and d["trade_id"] is None for d in decisions),
)

session_one_trade_id = trades[0]["id"]
position_ticket = trades[0]["position_ticket"]

check("position ticket captured", position_ticket is not None)


# ---------------------------------------------------------------------
# Invalid AI response must never trade
# ---------------------------------------------------------------------

print("\n=== Invalid AI output is rejected safely ===")

SIGNAL_SCRIPT.extend([
    "this is not json at all",
    {"signal": "BUY", "score": 900, "reasoning": "out of range"},
])

trades_before = len(repo.get_trades(limit=100))

run_cycle(engine, symbols=["EURUSDm", "GBPUSDm"])

trades_after = len(repo.get_trades(limit=100))

check(
    "malformed AI response created no trade",
    trades_after == trades_before,
    f"{trades_before} -> {trades_after}",
)

invalid = [d for d in repo.get_decisions(limit=50) if d["status"] == "invalid"]

check("invalid decisions recorded", len(invalid) == 2, f"got {len(invalid)}")

check(
    "invalid decision was overridden to HOLD with a reason",
    all(
        d["final_decision"] == "HOLD" and d["override_reason"]
        for d in invalid
    ),
)


# =====================================================================
# SESSION 2: RESTART
# =====================================================================

print("\n=== SESSION 2: restart the process ===")

decisions_before = len(repo.get_decisions(limit=500))
trades_before = len(repo.get_trades(limit=500))
equity_before = len(repo.get_equity_snapshots(limit=500))

from backend.database.connection import close_connection   # noqa: E402

close_connection()

engine = boot()

from backend.database import repository as repo   # noqa: E402

restored = engine.restore_state()

check(
    "decisions survived restart",
    len(repo.get_decisions(limit=500)) == decisions_before,
)

check(
    "trades survived restart",
    len(repo.get_trades(limit=500)) == trades_before,
)

check(
    "equity history survived restart",
    len(repo.get_equity_snapshots(limit=500)) == equity_before,
)

check(
    "dashboard cache rebuilt from database",
    restored["trades"] == trades_before,
    f"{restored['trades']} vs {trades_before}",
)

check(
    "engine does NOT auto-resume trading",
    restored["resumed_running"] is False,
)


# ---------------------------------------------------------------------
# Reconciliation: open position still open
# ---------------------------------------------------------------------

print("\n=== Reconciliation with live MT5 state ===")

from backend.core.reconciler import run_full_reconciliation   # noqa: E402

summary = run_full_reconciliation()

check(
    "still-open position stays open",
    repo.get_trade(session_one_trade_id)["execution_status"] == "EXECUTED",
)

check("nothing spuriously adopted", summary["adopted"] == 0, str(summary))

open_trades = repo.get_open_trades()

check("open trade tracked", len(open_trades) == 1, f"got {len(open_trades)}")


# ---------------------------------------------------------------------
# Close the position in MT5 -> settle -> experience
# ---------------------------------------------------------------------

print("\n=== Trade close produces P&L and an AI experience ===")

fake_mt5.close_position(position_ticket, profit=25.0)

summary = run_full_reconciliation()

closed = repo.get_trade(session_one_trade_id)

check("trade closed by reconciler", closed["execution_status"] == "CLOSED")
check("P&L recorded", closed["pnl"] is not None, str(closed["pnl"]))
check("result classified", closed["result"] == "WIN", str(closed["result"]))
check("R multiple computed", closed["r_multiple"] is not None)
check("exit price recorded", closed["exit_price"] is not None)

experiences = repo.find_experiences(limit=10)

check("experience created", len(experiences) == 1, f"got {len(experiences)}")

if experiences:
    check(
        "experience carries the trade context",
        experiences[0]["market_regime"] == "trending"
        and experiences[0]["setup"] == "breakout"
        and experiences[0]["outcome"] == "WIN",
    )

# Idempotency: a second pass must not duplicate anything.
run_full_reconciliation()

check(
    "reconciliation is idempotent",
    len(repo.find_experiences(limit=10)) == 1,
)


# ---------------------------------------------------------------------
# Memory retrieval
# ---------------------------------------------------------------------

print("\n=== AI memory retrieval ===")

from backend.ai import memory as memory_module   # noqa: E402

retrieved = memory_module.retrieve_relevant(
    "EURUSDm", {"market_regime": "trending", "volatility_bucket": "normal"}
)

check("relevant experiences retrieved", len(retrieved) >= 1)

context = memory_module.build_prompt_context(retrieved)

check("prompt context built", context is not None and "EURUSDm" in context)

check(
    "context stays bounded",
    len(memory_module.retrieve_relevant("EURUSDm", {})) <= 5,
)


# ---------------------------------------------------------------------
# Orphaned PENDING trade is resolved, not re-sent
# ---------------------------------------------------------------------

print("\n=== Crash recovery: orphaned PENDING order ===")

from backend.market.execution import comment_for, new_client_order_id   # noqa: E402

orphan_id = new_client_order_id()

orphan_trade_id = repo.insert_trade_intent({
    "client_order_id": orphan_id,
    "symbol": "EURUSDm",
    "direction": "BUY",
    "volume": 0.01,
    "reason": "simulated crash mid-send",
    "magic": 100001,
})

# The order DID reach MT5 before the crash.
import time as time_module   # noqa: E402

fake_mt5.world.deals.append(fake_mt5._Deal(
    ticket=999001,
    order=999002,
    position_id=999003,
    symbol="EURUSDm",
    entry=fake_mt5.DEAL_ENTRY_IN,
    price=1.1001,
    volume=0.01,
    profit=0.0,
    commission=0.0,
    swap=0.0,
    comment=comment_for(orphan_id),
    time=int(time_module.time()),
))

positions_before = len(fake_mt5.positions_get())

run_full_reconciliation()

recovered = repo.get_trade(orphan_trade_id)

check(
    "orphaned order adopted from MT5 history",
    recovered["execution_status"] == "EXECUTED",
    recovered["execution_status"],
)

check(
    "orphan recovery sent no new order",
    len(fake_mt5.positions_get()) == positions_before,
)

# And one that never reached MT5.
lost_id = new_client_order_id()

lost_trade_id = repo.insert_trade_intent({
    "client_order_id": lost_id,
    "symbol": "GBPUSDm",
    "direction": "SELL",
    "volume": 0.01,
    "reason": "never sent",
    "magic": 100001,
})

run_full_reconciliation()

check(
    "unsent order marked FAILED, not retried",
    repo.get_trade(lost_trade_id)["execution_status"] == "FAILED",
)


# ---------------------------------------------------------------------
# Untracked position adoption
# ---------------------------------------------------------------------

print("\n=== Untracked MT5 position adoption ===")

manual_ticket = fake_mt5._ticket()

fake_mt5.world.positions[manual_ticket] = fake_mt5._Position(
    ticket=manual_ticket,
    symbol="XAUUSDm",
    type=fake_mt5.POSITION_TYPE_BUY,
    volume=0.01,
    price_open=2000.0,
    price_current=2001.0,
    sl=1990.0,
    tp=2010.0,
    profit=1.0,
    magic=100001,
    comment="opened by a previous process",
    time=int(time_module.time()),
)

summary = run_full_reconciliation()

check("untracked position adopted", summary["adopted"] == 1, str(summary))

adopted = [
    t for t in repo.get_trades(limit=100)
    if t["position_ticket"] == manual_ticket
]

check("adopted position has a trade row", len(adopted) == 1)


# ---------------------------------------------------------------------
# Rejected orders are persisted
# ---------------------------------------------------------------------

print("\n=== Rejected orders are recorded, not discarded ===")

fake_mt5.world.reject_orders = True

SIGNAL_SCRIPT.append({
    "daily_trend": "bearish", "h1_trend": "bearish",
    "momentum": "strong", "market_structure": "lower_lows",
    "market_regime": "trending", "setup": "breakdown",
    "score": 75, "signal": "SELL",
    "daily_analysis": "Daily downtrend.",
    "h1_analysis": "H1 confirms.",
    "reasoning": "Bearish structure. Momentum agrees.",
})

run_cycle(engine, symbols=["EURUSDm"])

fake_mt5.world.reject_orders = False

rejected = [
    t for t in repo.get_trades(limit=100)
    if t["execution_status"] == "REJECTED"
]

check("rejection persisted", len(rejected) == 1, f"got {len(rejected)}")

check(
    "rejection carries a reason",
    bool(rejected and rejected[0]["reason"]),
)


# ---------------------------------------------------------------------
# Analytics
# ---------------------------------------------------------------------

print("\n=== Analytics ===")

from backend.database import analytics   # noqa: E402

metrics = analytics.compute_metrics()

check("metrics computed", metrics["total_trades"] == 1, str(metrics["total_trades"]))
check("total P&L computed", metrics["total_pnl"] == 24.9, str(metrics["total_pnl"]))
check("win rate computed", metrics["win_rate"] == 100.0)
check(
    "small sample is flagged",
    "not a statistically meaningful sample" in metrics["sample_note"].lower(),
    metrics["sample_note"],
)

check("daily P&L computed", len(analytics.daily_pnl()) == 1)
check("breakdown by symbol works", "EURUSDm" in analytics.breakdown("symbol"))


# ---------------------------------------------------------------------
# Per-symbol decisions (Phase 9)
# ---------------------------------------------------------------------

print("\n=== Per-symbol decision isolation (Phase 9) ===")

latest = repo.get_latest_decision_per_symbol()

check(
    "one latest decision per symbol",
    len({d["symbol"] for d in latest}) == len(latest) and len(latest) >= 2,
    f"got {len(latest)}",
)

traceable = [
    t for t in repo.get_trades(limit=100)
    if t["decision_id"] is not None
]

check(
    "every real trade traces to a decision",
    len(traceable) >= 2,
    f"got {len(traceable)}",
)


# =====================================================================
# RESULTS
# =====================================================================

print("\n" + "=" * 60)
print(f"PASSED: {len(PASSED)}   FAILED: {len(FAILED)}")

if FAILED:
    print("\nFailures:")
    for failure in FAILED:
        print(f"  - {failure}")

print("=" * 60)

close_connection()
shutil.rmtree(TEMP_DIR, ignore_errors=True)

sys.exit(1 if FAILED else 0)
