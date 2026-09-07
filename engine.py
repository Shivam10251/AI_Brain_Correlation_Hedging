"""
The trading cycle: risk gate, per-symbol pipeline, execution recording
and the main loop.

Process-level concerns (shared state, the MT5 connection, the engine
lock, startup recovery) live in runtime.py.

Traceability contract (Phase 9): for every executed order there is a
chain of rows you can follow -

    market_states  ->  decisions (ai_signal)
                   ->  decisions (final_decision + override_reason)
                   ->  trades (client_order_id, PENDING)
                   ->  trades (EXECUTED/REJECTED + MT5 tickets)
                   ->  trades (CLOSED + P&L)
                   ->  experiences

Nothing overrides the AI silently: if final_decision differs from
ai_signal, override_reason says why, and an event is written.
"""

import asyncio
import uuid
from datetime import datetime, timezone

import config
import news
from ai_brain import get_ai_decision
from data_engine import fetch_multi_timeframe_data
from database import repository as repo
from execution import execute_trade, new_client_order_id

# Re-exported so `from engine import bot_state` and the existing
# engine.<name> call sites keep working after the split.
from runtime import (                               # noqa: F401
    acquire_engine_lock,
    bot_state,
    connect_mt5,
    ensure_mt5,
    heartbeat_lock,
    persist_control_state,
    refresh_cache,
    release_engine_lock,
    restore_state,
)


_last_equity_snapshot_at = None


# =====================================================================
# RISK GATE
# =====================================================================

def apply_risk_checks(symbol, decision):
    """
    Decide what the engine actually does with the AI's signal.

    Returns (final_decision, override_reason). override_reason is None
    when the engine is doing exactly what the AI asked.

    Both gates default to disabled in config, so with a stock .env this
    function is a pass-through and live behaviour is unchanged.
    """

    ai_signal = decision.get("ai_signal", "HOLD")

    # 1. An unusable AI response can never become an order.
    if decision.get("status") != "ok":
        return "HOLD", (
            f"AI response unusable (status={decision.get('status')}): "
            f"{decision.get('error')}"
        )

    if ai_signal not in {"BUY", "SELL"}:
        return ai_signal, None

    # 2. MT5 must be reachable.
    if not bot_state["mt5_connected"]:
        return "HOLD", "MT5 is not connected"

    # 3. Optional score floor.
    if config.MIN_SCORE_TO_TRADE > 0:
        score = decision.get("ai_score") or 0

        if score < config.MIN_SCORE_TO_TRADE:
            return "HOLD", (
                f"AI score {score} below MIN_SCORE_TO_TRADE "
                f"({config.MIN_SCORE_TO_TRADE})"
            )

    # 4. Optional per-symbol position cap.
    if config.MAX_OPEN_POSITIONS_PER_SYMBOL > 0:
        open_count = repo.count_open_trades_for_symbol(symbol)

        if open_count >= config.MAX_OPEN_POSITIONS_PER_SYMBOL:
            return "HOLD", (
                f"{open_count} open position(s) on {symbol} at the "
                f"MAX_OPEN_POSITIONS_PER_SYMBOL limit "
                f"({config.MAX_OPEN_POSITIONS_PER_SYMBOL})"
            )

    return ai_signal, None


# =====================================================================
# PERSISTENCE HELPERS
# =====================================================================

def persist_market_state(symbol, market_data):
    features = market_data.get("features") or {}

    return repo.insert_market_state({
        "symbol": symbol,
        "bid": features.get("bid"),
        "ask": features.get("ask"),
        "spread": features.get("spread"),
        "last_close": features.get("last_close"),
        "atr": features.get("atr_h1"),
        "atr_pct": features.get("atr_pct"),
        "volatility_bucket": features.get("volatility_bucket"),
        "daily_trend": features.get("daily_trend"),
        "h1_trend": features.get("h1_trend"),
        "market_regime": features.get("market_regime"),
        "session": features.get("session"),
        "features": features,
    })


def persist_equity(account):
    """
    Snapshot equity, throttled so a fast cycle over four symbols does
    not write four near-identical rows.
    """

    global _last_equity_snapshot_at

    now = datetime.now(timezone.utc)

    if _last_equity_snapshot_at is not None:
        elapsed = (now - _last_equity_snapshot_at).total_seconds()

        if elapsed < config.EQUITY_SNAPSHOT_MIN_SECONDS:
            return None

    _last_equity_snapshot_at = now

    return repo.insert_equity_snapshot(account)


# =====================================================================
# ONE SYMBOL, ONE CYCLE
# =====================================================================

async def process_symbol(symbol, cycle_id):
    """
    The full pipeline for one symbol. Runs the blocking MT5 / HTTP work
    in a worker thread so the event loop keeps serving the dashboard.
    """

    # -------------------------------------------------------------
    # 1. Market data
    # -------------------------------------------------------------

    market_data = await asyncio.to_thread(
        fetch_multi_timeframe_data, symbol
    )

    bot_state["equity"] = market_data["equity"]

    persist_market_state(symbol, market_data)

    persist_equity(market_data["account"])

    # -------------------------------------------------------------
    # 2. AI decision (never raises)
    # -------------------------------------------------------------

    decision = await asyncio.to_thread(
        get_ai_decision, market_data, symbol
    )

    decision["cycle_id"] = cycle_id

    # -------------------------------------------------------------
    # 3. Risk gate
    # -------------------------------------------------------------

    final_decision, override_reason = apply_risk_checks(symbol, decision)

    decision["final_decision"] = final_decision
    decision["override_reason"] = override_reason

    decision_id = repo.insert_decision(decision)

    decision["id"] = decision_id

    # The AI panel now tracks the decision it belongs to, instead of a
    # single global slot every symbol overwrote.
    bot_state["last_logic"] = decision.get("reasoning") or ""
    bot_state["last_confidence"] = decision.get("ai_score") or 0

    if override_reason:
        repo.insert_event(
            f"{symbol}: engine overrode AI {decision.get('ai_signal')} "
            f"-> {final_decision}. Reason: {override_reason}",
            level="WARN",
            category="OVERRIDE",
            symbol=symbol,
            decision_id=decision_id,
        )

    if decision.get("status") != "ok":
        repo.insert_event(
            f"{symbol}: AI decision unusable - {decision.get('error')}",
            level="ERROR",
            category="AI",
            symbol=symbol,
            decision_id=decision_id,
        )

    # -------------------------------------------------------------
    # 4. Execute
    # -------------------------------------------------------------

    if final_decision not in {"BUY", "SELL"}:
        print(
            f"[HOLD] {symbol} | Score: {decision.get('ai_score')}"
            + (f" | {override_reason}" if override_reason else "")
        )
        return decision

    await execute_and_record(symbol, final_decision, decision, market_data)

    return decision


async def execute_and_record(symbol, signal, decision, market_data):
    """
    Write the intent, send the order, then record the outcome.

    The PENDING row is written BEFORE the order is sent. If this
    process dies mid-flight, the reconciler resolves that row against
    MT5's history instead of re-sending - which is what stops a restart
    from duplicating an order.
    """

    features = market_data.get("features") or {}

    client_order_id = new_client_order_id()

    trade_id = repo.insert_trade_intent({
        "client_order_id": client_order_id,
        "symbol": symbol,
        "direction": signal,
        "volume": config.LOT_SIZE,
        "requested_price": features.get("ask") if signal == "BUY"
        else features.get("bid"),
        "reason": decision.get("reasoning"),
        "decision_id": decision.get("id"),
        "magic": config.MAGIC_NUMBER,
        "market_regime": decision.get("market_regime")
        or features.get("market_regime"),
        "setup": decision.get("setup"),
        "ai_score": decision.get("ai_score"),
        "timeframe": decision.get("timeframe"),
        "news_condition": news.news_condition(decision.get("news")),
    })

    repo.update_decision(decision["id"], trade_id=trade_id)

    result = await asyncio.to_thread(
        execute_trade, symbol, signal, client_order_id
    )

    status = result.get("status")

    if status == "EXECUTED":
        repo.update_trade(
            trade_id,
            execution_status="EXECUTED",
            opened_at=repo.utc_now(),
            entry_price=result.get("entry_price"),
            stop_loss=result.get("stop_loss"),
            take_profit=result.get("take_profit"),
            volume=result.get("filled_volume") or result.get("volume"),
            mt5_retcode=result.get("mt5_retcode"),
            mt5_comment=result.get("mt5_comment"),
            order_ticket=result.get("order_ticket"),
            deal_ticket=result.get("deal_ticket"),
            position_ticket=result.get("position_ticket"),
            raw_result=result.get("raw_result"),
        )

        repo.insert_event(
            f"{symbol} {signal} EXECUTED at "
            f"{result.get('entry_price')} "
            f"(score {decision.get('ai_score')}/100)",
            level="INFO",
            category="EXECUTION",
            symbol=symbol,
            trade_id=trade_id,
            decision_id=decision.get("id"),
        )

        print(
            f"[TRADE] {symbol} | {signal} | "
            f"Score: {decision.get('ai_score')}/100"
        )

    elif result.get("ambiguous"):
        # Leave the row PENDING on purpose - the reconciler decides.
        repo.insert_event(
            f"{symbol} {signal} outcome UNKNOWN: {result.get('reason')}. "
            f"Left pending for reconciliation.",
            level="ERROR",
            category="EXECUTION",
            symbol=symbol,
            trade_id=trade_id,
        )

    else:
        repo.update_trade(
            trade_id,
            execution_status=status or "FAILED",
            mt5_retcode=result.get("mt5_retcode"),
            mt5_comment=result.get("mt5_comment"),
            raw_result=result.get("raw_result"),
            reason=result.get("reason"),
        )

        repo.insert_event(
            f"{symbol} {signal} {status}: {result.get('reason')}",
            level="ERROR",
            category="EXECUTION",
            symbol=symbol,
            trade_id=trade_id,
            decision_id=decision.get("id"),
        )

        print(f"[{status}] {symbol} | {result.get('reason')}")


# =====================================================================
# MAIN LOOP
# =====================================================================

async def trading_loop():
    """
    Main AI trading loop.

    Sequentially processes every symbol in config.SYMBOLS.
    """

    while True:

        if bot_state["is_running"]:

            cycle_id = uuid.uuid4().hex[:12]

            heartbeat_lock()

            if not ensure_mt5():
                repo.insert_event(
                    "Cycle skipped - MT5 unavailable",
                    level="ERROR",
                    category="MT5",
                )

                await asyncio.sleep(bot_state["interval"])
                continue

            # Settle anything that closed since the last cycle. This is
            # what turns finished trades into P&L and experiences.
            try:
                from reconciler import run_full_reconciliation

                await asyncio.to_thread(run_full_reconciliation)
            except Exception as error:              # noqa: BLE001
                print(f"[ERROR] reconciliation: {error}")

            for symbol in config.SYMBOLS:

                # Bot may have been stopped while processing
                if not bot_state["is_running"]:
                    break

                try:
                    await process_symbol(symbol, cycle_id)

                except Exception as e:
                    bot_state["last_error"] = f"{symbol}: {e}"

                    print(f"[ERROR] {symbol}: {e}")

                    repo.insert_event(
                        f"Cycle error on {symbol}: {e}",
                        level="ERROR",
                        category="CYCLE",
                        symbol=symbol,
                    )

            bot_state["last_cycle_at"] = repo.utc_now()

            refresh_cache()

            # --------------------------------------------------------
            # Wait before next complete cycle
            # --------------------------------------------------------
            await asyncio.sleep(bot_state["interval"])

        else:
            # Bot is stopped, don't burn CPU
            await asyncio.sleep(1)
