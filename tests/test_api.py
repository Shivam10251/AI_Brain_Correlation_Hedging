"""
HTTP-layer check: the app boots, the lifespan recovery runs, and every
dashboard endpoint serves data from the database.

Requires httpx (a FastAPI TestClient dependency, dev only):

    pip install httpx

Run from the project root:

    python tests/test_api.py
"""

import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tests.fake_mt5 as fake_mt5                   # noqa: E402

fake_mt5.install()

TEMP_DIR = tempfile.mkdtemp(prefix="quantbot-api-")

os.environ["DB_PATH"] = os.path.join(TEMP_DIR, "api.db")
os.environ["DEEPSEEK_API_KEY"] = "test-key"
os.environ["SYMBOLS"] = "EURUSDm"


PASSED = []
FAILED = []


def check(name, condition, detail=""):
    if condition:
        PASSED.append(name)
        print(f"  PASS  {name}")
    else:
        FAILED.append(f"{name} {detail}")
        print(f"  FAIL  {name} {detail}")


from fastapi.testclient import TestClient            # noqa: E402

import main                                          # noqa: E402
from database import repository as repo              # noqa: E402


print("\n=== API smoke test ===")

with TestClient(main.app) as client:

    # ------------------------------------------------------------
    # Seed a little history through the repository, exactly as the
    # engine would.
    # ------------------------------------------------------------

    decision_id = repo.insert_decision({
        "symbol": "EURUSDm",
        "timeframe": "D1+H1",
        "model": "deepseek-chat",
        "status": "ok",
        "market_regime": "trending",
        "setup": "breakout",
        "ai_score": 82,
        "ai_signal": "BUY",
        "final_decision": "BUY",
        "reasoning": "Structure is bullish. Momentum confirms.",
    })

    trade_id = repo.insert_trade_intent({
        "client_order_id": "api-test-1",
        "symbol": "EURUSDm",
        "direction": "BUY",
        "volume": 0.01,
        "decision_id": decision_id,
        "reason": "Structure is bullish. Momentum confirms.",
        "ai_score": 82,
        "market_regime": "trending",
        "setup": "breakout",
    })

    repo.update_trade(
        trade_id,
        execution_status="CLOSED",
        entry_price=1.1000,
        exit_price=1.1040,
        stop_loss=1.0978,
        closed_at=repo.utc_now(),
        opened_at=repo.utc_now(),
        pnl=40.0,
        result="WIN",
        r_multiple=1.82,
    )

    repo.insert_equity_snapshot({"equity": 10_000.0, "balance": 10_000.0})
    repo.insert_equity_snapshot({"equity": 10_040.0, "balance": 10_040.0})

    # ------------------------------------------------------------
    # Endpoints
    # ------------------------------------------------------------

    response = client.get("/")
    check("dashboard renders", response.status_code == 200, str(response.status_code))
    check("no stray markdown fence in HTML", "```" not in response.text)
    check("score label is not a percentage", "AI Signal Score" in response.text)

    response = client.get("/api/status")
    payload = response.json()

    check("status endpoint responds", response.status_code == 200)

    for key in ("is_running", "interval", "equity", "last_logic",
                "last_confidence", "trade_history"):
        check(f"legacy status key '{key}' preserved", key in payload)

    check(
        "per-symbol decisions exposed",
        len(payload.get("decisions_by_symbol", [])) == 1,
    )

    response = client.get("/api/history/trades")
    trades = response.json()["trades"]

    check("trade history served", len(trades) == 1, f"got {len(trades)}")
    check("trade carries P&L", trades[0]["pnl"] == 40.0)
    check("trade carries its decision id", trades[0]["decision_id"] == decision_id)

    response = client.get("/api/history/equity")
    snapshots = response.json()["snapshots"]

    check("equity history served", len(snapshots) == 2, f"got {len(snapshots)}")
    check(
        "equity points are oldest-first",
        snapshots[0]["equity"] < snapshots[-1]["equity"],
    )

    response = client.get("/api/history/decisions")
    check("decision history served", len(response.json()["decisions"]) == 1)

    response = client.get("/api/history/events")
    check("event feed served", len(response.json()["events"]) >= 1)

    response = client.get("/api/analytics")
    metrics = response.json()["overall"]

    check("analytics served", metrics["total_trades"] == 1)
    check("analytics P&L correct", metrics["total_pnl"] == 40.0)

    response = client.get("/api/analytics/breakdown/setup")
    check("breakdown served", "breakout" in response.json()["groups"])

    response = client.get("/api/positions")
    check("positions endpoint responds", response.status_code == 200)

    response = client.get("/api/health")
    health = response.json()

    check("health endpoint responds", health["status"] == "ok")
    check("engine lock held by this process", health["owns_engine_lock"] is True)

    # ------------------------------------------------------------
    # Control: toggles a flag, never restarts the loop
    # ------------------------------------------------------------

    response = client.post("/api/control", json={"action": "start"})
    check("engine starts", response.json()["bot_state"]["is_running"] is True)

    check(
        "run state persisted to database",
        repo.get_state("is_running") is True,
    )

    response = client.post("/api/control", json={"interval": 60})
    check("interval updated", response.json()["bot_state"]["interval"] == 60)
    check("interval persisted", repo.get_state("interval") == 60)

    response = client.post("/api/control", json={"action": "stop"})
    check("engine stops", response.json()["bot_state"]["is_running"] is False)

    response = client.post("/api/control", json={"action": "nonsense"})
    check("bad action rejected", response.json()["status"] == "error")

    response = client.post("/api/control", json={"interval": 0})
    check("bad interval rejected", response.json()["status"] == "error")


print("\n" + "=" * 60)
print(f"PASSED: {len(PASSED)}   FAILED: {len(FAILED)}")

if FAILED:
    print("\nFailures:")
    for failure in FAILED:
        print(f"  - {failure}")

print("=" * 60)

from database.connection import close_connection     # noqa: E402

close_connection()
shutil.rmtree(TEMP_DIR, ignore_errors=True)

sys.exit(1 if FAILED else 0)
