import asyncio
from datetime import datetime, timezone

from fastapi import FastAPI
from pydantic import BaseModel

import MetaTrader5 as mt5

import config
from data_engine import fetch_multi_timeframe_data
from ai_brain import get_ai_decision
from execution import execute_trade


app = FastAPI(
    title="AI Hedge Fund Bot",
    version="1.0.0"
)


# ============================================================
# BOT STATE
# ============================================================

bot_state = {
    "is_running": False,
    "interval": 30,
    "equity": 0.0,
    "last_logic": "",
    "last_confidence": 0,
    "trade_history": []
}


# ============================================================
# REQUEST MODEL
# ============================================================

class ControlRequest(BaseModel):
    action: str | None = None
    interval: int | None = None


# ============================================================
# API: CONTROL BOT
# ============================================================

@app.post("/api/control")
async def control_bot(request: ControlRequest):
    """
    Start/stop the trading bot and optionally change
    the trading interval.
    """

    if request.action is not None:
        action = request.action.lower()

        if action == "start":
            bot_state["is_running"] = True

        elif action == "stop":
            bot_state["is_running"] = False

        else:
            return {
                "status": "error",
                "message": "Action must be 'start' or 'stop'"
            }

    if request.interval is not None:
        if request.interval < 1:
            return {
                "status": "error",
                "message": "Interval must be at least 1 second"
            }

        bot_state["interval"] = request.interval

    return {
        "status": "success",
        "bot_state": bot_state
    }


# ============================================================
# API: BOT STATUS
# ============================================================

@app.get("/api/status")
async def get_status():
    """
    Return the current bot state.
    """

    return bot_state


# ============================================================
# TRADING LOOP
# ============================================================

async def trading_loop():
    """
    Main AI trading loop.

    Sequentially processes every symbol in config.SYMBOLS.
    """

    while True:

        if bot_state["is_running"]:

            for symbol in config.SYMBOLS:

                # Bot may have been stopped while processing
                if not bot_state["is_running"]:
                    break

                try:
                    # ------------------------------------------------
                    # 1. Fetch market data
                    # ------------------------------------------------
                    market_data = fetch_multi_timeframe_data(symbol)

                    # Update account equity
                    bot_state["equity"] = market_data["equity"]

                    # ------------------------------------------------
                    # 2. Get AI decision
                    # ------------------------------------------------
                    decision = get_ai_decision(
                        market_data,
                        symbol
                    )

                    signal = decision["signal"]
                    confidence = decision["confidence_score"]
                    logic = decision["logic"]

                    # Update latest AI information
                    bot_state["last_logic"] = logic
                    bot_state["last_confidence"] = confidence

                    # ------------------------------------------------
                    # 3. Execute trade if BUY / SELL
                    # ------------------------------------------------
                    if signal in {"BUY", "SELL"}:

                        result = execute_trade(
                            symbol,
                            signal
                        )

                        # ------------------------------------------------
                        # 4. Record executed trade
                        # ------------------------------------------------
                        trade_info = {
                            "time": datetime.now(
                                timezone.utc
                            ).isoformat(),

                            "asset": symbol,

                            "signal": signal,

                            "logic": logic
                        }

                        bot_state["trade_history"].append(
                            trade_info
                        )

                        print(
                            f"[TRADE] {symbol} | "
                            f"{signal} | "
                            f"Confidence: {confidence}"
                        )

                    else:
                        print(
                            f"[HOLD] {symbol} | "
                            f"Confidence: {confidence}"
                        )

                except Exception as e:

                    print(
                        f"[ERROR] {symbol}: {e}"
                    )

            # --------------------------------------------------------
            # Wait before next complete cycle
            # --------------------------------------------------------
            await asyncio.sleep(
                bot_state["interval"]
            )

        else:
            # Bot is stopped, don't burn CPU
            await asyncio.sleep(1)


# ============================================================
# STARTUP / SHUTDOWN
# ============================================================

@app.on_event("startup")
async def startup_event():

    # Initialize MetaTrader 5
    if not mt5.initialize():
        raise RuntimeError(
            f"MT5 initialization failed: {mt5.last_error()}"
        )

    print("MT5 initialized successfully.")

    # Start trading loop in background
    asyncio.create_task(
        trading_loop()
    )

    print("AI Hedge Fund trading loop started.")


@app.on_event("shutdown")
async def shutdown_event():

    mt5.shutdown()

    print("MT5 connection closed.")