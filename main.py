"""
FastAPI server and the autonomous trading engine.

The loop is the whole system in one place:

    for each symbol
        fetch market data
        ask the brain (which already carries the auditor's rules)
        guard against duplicates and hedges
        execute, and record the full market context
    reconcile closed trades against MT5
    if new evidence arrived, run an audit
    sleep

Every stage is wrapped so that one bad symbol costs one symbol, not the
scan.
"""

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import MetaTrader5 as mt5
from fastapi import FastAPI, Request
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

import auditor
import config
import data_engine
import memory_store
from ai_brain import get_ai_decision, load_learned_rules
from data_engine import (
    fetch_correlated_asset_prices,
    fetch_multi_timeframe_data,
)
from execution import close_position, execute_trade, get_open_positions


# =============================================================
# State
# =============================================================

bot_state = {
    "is_running": False,
    "interval": config.SCAN_INTERVAL_SECONDS,
    "risk_percent": config.DEFAULT_RISK_PERCENT,
    "equity": 0.0,
    "last_logic": "",
    "last_confidence": 0,
    "last_signal": "HOLD",
    "last_entry_price": None,
    "last_stop_loss": None,
    "last_take_profit": None,
    "trade_history": [],
    "open_positions": [],
    "learned_rules": [],
    "last_audit_at": None,
}


def _now():
    return datetime.now(timezone.utc).isoformat()


def _refresh_rules():
    document = auditor.load_rules_document()

    bot_state["learned_rules"] = [
        rule for rule in document.get("rules", [])
        if rule.get("status") == "ACTIVE"
    ]

    bot_state["last_audit_at"] = document.get("last_audit_at")

    return bot_state["learned_rules"]


# =============================================================
# Lifespan
# =============================================================

@asynccontextmanager
async def lifespan(app: FastAPI):

    print("[SYSTEM] starting AI Hedge Fund...")

    try:
        account = data_engine.initialize_mt5()
        bot_state["equity"] = float(account.equity)

    except Exception as error:                      # noqa: BLE001
        # Not fatal. The dashboard must still serve so an operator can
        # see WHY it will not trade.
        print(f"[SYSTEM] MT5 unavailable at startup: {error}")

    try:
        reconciled = memory_store.reconcile_closed_trades()

        if reconciled:
            print(f"[SYSTEM] reconciled {reconciled} trade(s) from history")

    except Exception as error:                      # noqa: BLE001
        print(f"[SYSTEM] startup reconciliation failed: {error}")

    _refresh_rules()

    print(f"[SYSTEM] {len(bot_state['learned_rules'])} learned rule(s) active")

    try:
        if auditor.audit_is_due():
            print("[AUDITOR] an audit is due at startup; running it")
            result = auditor.run_audit()
            print(f"[AUDITOR] {result.get('status')}: {result.get('message')}")
            _refresh_rules()

    except Exception as error:                      # noqa: BLE001
        print(f"[AUDITOR] startup audit failed: {error}")

    task = asyncio.create_task(trading_loop())

    print("[SYSTEM] trading loop started. Engine is STOPPED until you start it.")

    yield

    task.cancel()

    data_engine.shutdown_mt5()


app = FastAPI(title="AI Hedge Fund Bot", version="2.0.0", lifespan=lifespan)

templates = Jinja2Templates(directory="templates")


# =============================================================
# Request models
# =============================================================

class ControlRequest(BaseModel):
    action: str | None = None
    interval: int | None = None
    risk_percent: float | None = None


class CloseRequest(BaseModel):
    ticket: int


class AuditRequest(BaseModel):
    force: bool = False


# =============================================================
# Endpoints
# =============================================================

@app.get("/")
async def home(request: Request):
    return templates.TemplateResponse(
        request=request, name="index.html", context={"request": request}
    )


@app.get("/api/status")
async def get_status():
    """Live state. The dashboard polls this every three seconds."""

    try:
        account = mt5.account_info()

        if account is not None:
            bot_state["equity"] = float(account.equity)

        bot_state["open_positions"] = get_open_positions()

    except Exception as error:                      # noqa: BLE001
        print(f"[SYSTEM] status refresh failed: {error}")

    _refresh_rules()

    return bot_state


@app.post("/api/control")
async def control_bot(request: ControlRequest):
    """Start/stop the engine and adjust interval or risk."""

    if request.action is not None:
        action = request.action.lower()

        if action == "start":
            bot_state["is_running"] = True
            print("[SYSTEM] engine STARTED")

        elif action == "stop":
            bot_state["is_running"] = False
            print("[SYSTEM] engine STOPPED")

        else:
            return {
                "status": "error",
                "message": "action must be 'start' or 'stop'",
            }

    if request.interval is not None:
        if request.interval < 1:
            return {
                "status": "error",
                "message": "interval must be at least 1 second",
            }

        bot_state["interval"] = int(request.interval)

    if request.risk_percent is not None:
        if not 0 < request.risk_percent <= 100:
            return {
                "status": "error",
                "message": "risk_percent must be between 0 and 100",
            }

        bot_state["risk_percent"] = float(request.risk_percent)

    return {"status": "success", "bot_state": bot_state}


@app.post("/api/close")
async def close_one(request: CloseRequest):
    """Close a single position by ticket, from the dashboard."""

    try:
        result = await asyncio.to_thread(close_position, request.ticket)

    except Exception as error:                      # noqa: BLE001
        return {"status": "error", "message": str(error)}

    bot_state["open_positions"] = get_open_positions()

    return {"status": "success", "result": result}


@app.post("/api/audit")
async def run_audit_now(request: AuditRequest):
    """Run the self-learning audit on demand."""

    try:
        result = await asyncio.to_thread(auditor.run_audit, request.force)

    except Exception as error:                      # noqa: BLE001
        return {"status": "error", "message": str(error)}

    _refresh_rules()

    return {"status": "success", "result": result}


@app.get("/api/rules")
async def get_rules():
    """The full learned-rules document."""

    return auditor.load_rules_document()


# =============================================================
# The loop
# =============================================================

def _existing_position(symbol, signal):
    """
    What is already open on this symbol, relative to the new signal.

    Returns (same_direction, opposite_direction) as lists.
    """

    open_now = get_open_positions(symbol)

    ours = [p for p in open_now if p["magic"] == config.MAGIC_NUMBER]

    same = [p for p in ours if p["side"] == signal]
    opposite = [p for p in ours if p["side"] != signal]

    return same, opposite


async def _process_symbol(symbol):
    """One symbol, one decision, at most one order."""

    market_data = await asyncio.to_thread(fetch_multi_timeframe_data, symbol)

    bot_state["equity"] = market_data["equity"]

    decision = await asyncio.to_thread(get_ai_decision, market_data, symbol)

    signal = decision["signal"]
    confidence = decision["confidence_score"]

    bot_state["last_signal"] = signal
    bot_state["last_logic"] = decision.get("logic", "")
    bot_state["last_confidence"] = confidence

    print(f"[AI] {symbol} -> {signal} @ {confidence}")

    if signal not in {"BUY", "SELL"}:
        return

    same, opposite = await asyncio.to_thread(
        _existing_position, symbol, signal
    )

    # --- duplicate guard -----------------------------------------
    # Without this the bot re-enters the same direction on every scan,
    # compounding one opinion into an unbounded position.
    if same:
        print(
            f"[DUPLICATE PREVENTED] {symbol}: already {signal} on "
            f"ticket {same[0]['ticket']}; not stacking another."
        )
        return

    # --- reversal guard ------------------------------------------
    # Opening the opposite side while a position is live is a hedge:
    # margin on both legs, spread paid twice, and a net exposure of
    # roughly zero. Close first, then enter.
    if opposite:
        for position in opposite:
            print(
                f"[REVERSAL] {symbol}: closing {position['side']} "
                f"ticket {position['ticket']} before entering {signal}"
            )

            result = await asyncio.to_thread(close_position, position["ticket"])

            if result.get("status") != "CLOSED":
                print(
                    f"[REVERSAL] {symbol}: close failed "
                    f"({result.get('message')}); skipping the entry so we "
                    f"do not end up hedged."
                )
                return

    # --- execute --------------------------------------------------
    correlated = await asyncio.to_thread(
        fetch_correlated_asset_prices, symbol, config.SYMBOLS
    )

    fill = await asyncio.to_thread(
        execute_trade,
        symbol,
        signal,
        decision["stop_loss"],
        decision["take_profit"],
        bot_state["risk_percent"],
    )

    bot_state["last_entry_price"] = fill["entry_price"]
    bot_state["last_stop_loss"] = fill["stop_loss"]
    bot_state["last_take_profit"] = fill["take_profit"]

    # The market context is stored WITH the trade. This is the whole
    # basis of the audit later - without it a loss is just a number.
    record = dict(fill)

    record.update({
        "time": _now(),
        "confidence_score": confidence,
        "logic": decision.get("logic", ""),
        "market_context": {
            "bid": market_data["bid"],
            "ask": market_data["ask"],
            "spread": market_data["spread"],
            "h1_data": market_data["h1_data"],
            "daily_data": market_data["daily_data"],
            "correlated_prices": correlated,
        },
    })

    await asyncio.to_thread(memory_store.append_trade_memory, record)

    bot_state["trade_history"].append({
        "time": record["time"],
        "symbol": symbol,
        "side": signal,
        "entry_price": fill["entry_price"],
        "stop_loss": fill["stop_loss"],
        "take_profit": fill["take_profit"],
        "volume": fill["volume"],
        "risk_percent": fill["risk_percent"],
        "deal": fill["deal"],
        "confidence": confidence,
        "status": "CONFIRMED",
    })

    # Keep the in-memory feed bounded; memory.json is the real record.
    bot_state["trade_history"] = bot_state["trade_history"][-100:]


async def trading_loop():
    """The autonomous engine. Runs forever; trades only when started."""

    while True:

        if not bot_state["is_running"]:
            await asyncio.sleep(1)
            continue

        for symbol in config.SYMBOLS:

            if not bot_state["is_running"]:
                break

            try:
                await _process_symbol(symbol)

            except Exception as error:              # noqa: BLE001
                print(f"[ERROR] {symbol}: {error}")

        # --- after a complete scan --------------------------------
        try:
            newly_closed = await asyncio.to_thread(
                memory_store.reconcile_closed_trades
            )

            if newly_closed:
                print(f"[LEARNING] {newly_closed} trade(s) newly closed")

                if auditor.audit_is_due(newly_closed):
                    result = await asyncio.to_thread(auditor.run_audit)

                    print(
                        f"[AUDITOR] {result.get('status')}: "
                        f"{result.get('message')}"
                    )

                    _refresh_rules()

        except Exception as error:                  # noqa: BLE001
            print(f"[LEARNING] post-scan reconciliation failed: {error}")

        await asyncio.sleep(bot_state["interval"])


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
