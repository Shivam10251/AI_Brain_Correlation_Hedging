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

from backend import config
from backend.ai.brain import get_ai_decision
from backend.database import repo_ledger, repo_meta
from backend.database import repository as repo
from backend.market import data_engine, news
from backend.risk import engine as risk_engine
from backend.risk import killswitch, money, sizing, stops
from backend.risk import profiles as risk_profiles
from backend.market.data_engine import fetch_multi_timeframe_data
from backend.market.execution import execute_trade, new_client_order_id

# Re-exported so `from engine import bot_state` and the existing
# engine.<name> call sites keep working after the split.
from backend.core import exits
from backend.core.runtime import (                  # noqa: F401
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

def build_risk_context(symbol, decision, market_data, risk):
    """
    Gather everything the fourteen checks need (Phase 3).

    Assembled here, evaluated there. Keeping the gathering out of the
    engine is what lets a test construct a violating state directly
    instead of driving the whole loop into it.
    """

    features = market_data.get("features") or {}

    account = market_data.get("account") or {}

    profile = risk_profiles.load()

    account_id = bot_state.get("account_id")

    day = repo_ledger.day_summary(
        account_id,
        repo_ledger.trading_day(bot_state.get("server_utc_offset_min")),
        floating_pnl=account.get("floating_pnl") or 0.0,
    ) if account_id is not None else {}

    return risk_engine.RiskContext(
        symbol=symbol,
        signal=decision.get("ai_signal") or "HOLD",
        decision=decision,
        profile=profile,
        account=account,
        day=day,
        risk_amount=risk.get("risk_amount"),
        stop_distance=risk.get("stop_distance_price"),
        spread=features.get("spread"),
        open_trades=repo.get_open_trades(),
        mt5_connected=bot_state["mt5_connected"],
        kill_switch=killswitch.get_state(),
        news=None,                       # Phase 7 supplies the feed
    )


def apply_risk_checks(symbol, decision, market_data=None, risk=None):
    """
    Decide what the engine actually does with the AI's signal.

    Phase 3: this is now a thin adapter over backend.risk.engine, which
    holds sole authority. It returns the same (final_decision,
    override_reason) pair the loop has always consumed, and
    additionally stashes the full verdict on `decision` so it is
    persisted with the row.

    The previous implementation was five inline conditions with two of
    the five disabled by default - Phase 0 ranked that as weakness #2.
    """

    ai_signal = decision.get("ai_signal", "HOLD")

    # A non-trade signal never reaches the engine: there is nothing to
    # authorise, and recording fourteen refusals for a HOLD would bury
    # the real refusals in noise.
    if decision.get("status") == "ok" and ai_signal not in {"BUY", "SELL"}:
        return ai_signal, None

    # Phase 5: a signal the sizer could not turn into a volume is not
    # sendable. Refuse here rather than let execute_trade fall back to a
    # fixed lot downstream - that fallback is the exact breach sizing
    # exists to prevent (e.g. the minimum lot risks more than the
    # per-trade budget, or margin is short).
    if (ai_signal in {"BUY", "SELL"}
            and (risk or {}).get("sizing_refused")):
        return "HOLD", f"Sizing refused: {risk['sizing_refused']}"

    context = build_risk_context(
        symbol, decision, market_data or {}, risk or {}
    )

    verdict = risk_engine.evaluate(context)

    decision["risk_checks"] = verdict.as_dict()

    try:
        decision["risk_profile_id"] = risk_profiles.ensure_registered(
            context.profile
        )
    except Exception:                               # noqa: BLE001
        decision["risk_profile_id"] = None

    if verdict.allow:
        return ai_signal, None

    return "HOLD", verdict.reason


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


def update_daily_ledger(account):
    """
    Sample the day's equity marks and worst floating loss (Phase 2).

    Deliberately NOT throttled like the equity snapshot: the floating
    low-water mark is the whole point, and a drawdown that happens
    between two snapshots would otherwise be invisible. An open
    position that dipped hard and recovered leaves no trace in the deal
    history, so if it is not sampled here it is lost.
    """

    account_id = bot_state.get("account_id")

    if account_id is None:
        return None

    day = repo_ledger.trading_day(bot_state.get("server_utc_offset_min"))

    return repo_ledger.record_equity_marks(
        account_id,
        day,
        equity=account.get("equity"),
        balance=account.get("balance"),
        floating_pnl=account.get("floating_pnl"),
    )


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

    update_daily_ledger(market_data["account"])

    # -------------------------------------------------------------
    # 2. AI decision (never raises)
    # -------------------------------------------------------------

    decision = await asyncio.to_thread(
        get_ai_decision, market_data, symbol
    )

    decision["cycle_id"] = cycle_id

    # -------------------------------------------------------------
    # 3. Risk gate
    #
    # The trade is PRICED before it is judged: checks 10-14 are money
    # limits, and they cannot evaluate a trade whose risk is unknown.
    # -------------------------------------------------------------

    features = market_data.get("features") or {}

    planned_risk = plan_risk(
        symbol,
        decision.get("ai_signal") or "HOLD",
        features,
        volume=None,
        account=market_data.get("account"),
    ) if decision.get("ai_signal") in {"BUY", "SELL"} else {}

    # Phase 5: a flip closes the existing position instead of opening
    # the opposite one. Runs BEFORE the gate, because the hedging check
    # would otherwise refuse the flip and the position would simply be
    # held - the AI's change of mind silently discarded.
    reversed_position = False

    if decision.get("status") == "ok" and decision.get("ai_signal") in {
        "BUY", "SELL"
    }:
        try:
            profile = risk_profiles.load()
        except Exception:                           # noqa: BLE001
            profile = None

        reversed_position, reversal_detail = await asyncio.to_thread(
            exits.handle_reversal,
            symbol,
            decision["ai_signal"],
            decision,
            profile,
        )

        if reversed_position:
            decision["reversal"] = reversal_detail

    if reversed_position:
        # Do not enter in the same cycle: the close must settle and
        # reconcile so the next entry is judged on a clean book.
        #
        # This falls THROUGH to the persistence below rather than
        # returning: a reversal cycle is still a decision, and a
        # decision that is never written has no strategy version
        # (invariant 9), no decision_bars to replay, and is invisible to
        # the Phase 6 calibration - which is precisely the population of
        # flips that study most needs to see.
        final_decision = "HOLD"
        override_reason = (
            f"Closed opposing position on reversal to "
            f"{decision['ai_signal']}; entry deferred to the next bar."
        )
    else:
        final_decision, override_reason = apply_risk_checks(
            symbol, decision, market_data, planned_risk
        )

    decision["final_decision"] = final_decision
    decision["override_reason"] = override_reason

    # Phase 4: which feature formulas and which bar produced this.
    decision["feature_version"] = features.get("feature_version")
    decision["bar_time_utc"] = features.get("bar_time_utc")

    decision_id = repo.insert_decision(decision)

    decision["id"] = decision_id

    # Phase 4: the exact bars this decision saw, for offline replay.
    repo.insert_decision_bars(decision_id, market_data.get("snapshot"))

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

    await execute_and_record(
        symbol, final_decision, decision, market_data, planned_risk
    )

    return decision


def plan_risk(symbol, signal, features, volume=None, account=None):
    """
    Price and size the trade BEFORE it is sent (Phases 2 and 5).

    Order matters here:

      1. place the stop (versioned model, broker minimum respected)
      2. size the position so that stop costs the profile's budget
      3. re-price the risk at the volume actually chosen

    Sizing has to follow stop placement, not precede it: volume is a
    function of the stop distance, so a fixed lot with a variable stop
    puts a different amount at risk on every trade - Phase 0
    weakness #4.

    `risk_amount` is None when the broker specification is missing.
    Reported honestly rather than guessed, because the Phase 3 engine
    must be able to tell "unpriced" from "zero risk" and refuses on the
    former.
    """

    entry = features.get("ask") if signal == "BUY" else features.get("bid")

    if entry is None:
        return {"risk_amount": None, "volume": volume}

    symbol_profile = None

    account_id = bot_state.get("account_id")

    if account_id is not None:
        symbol_profile = repo_meta.get_symbol_profile(account_id, symbol)

    # ---- 1. Stop placement (versioned) ------------------------------
    placement = stops.compute(
        entry,
        signal,
        model=config.STOP_MODEL,
        sl_percent=config.SL_PERCENT,
        tp_percent=config.TP_PERCENT,
        atr=features.get("atr_h1"),
        k_sl=config.ATR_SL_MULTIPLE,
        k_tp=config.ATR_TP_MULTIPLE,
        symbol_profile=symbol_profile,
    )

    stop = placement["stop_loss"]

    # ---- 2. Sizing --------------------------------------------------
    chosen_volume = volume
    sizing_detail = None

    if config.POSITION_SIZING == "risk" and account and symbol_profile:

        try:
            profile = risk_profiles.load()
        except Exception:                           # noqa: BLE001
            profile = None

        if profile:
            sizing_detail = sizing.plan(
                symbol,
                signal,
                entry,
                placement["stop_distance"],
                profile,
                account,
                symbol_profile,
            )

            if sizing_detail.get("volume"):
                chosen_volume = sizing_detail["volume"]
            else:
                # Sized to nothing - the minimum lot already risks more
                # than allowed, or margin is short. Do NOT silently
                # fall back to a fixed lot: that is the breach the
                # sizing exists to prevent.
                return {
                    "risk_amount": sizing_detail.get("risk_amount"),
                    "volume": None,
                    "sizing": sizing_detail,
                    "planned_entry": entry,
                    "planned_stop_loss": stop,
                    "stop_model": placement["model_used"],
                    "sizing_refused": sizing_detail.get("reason"),
                }

    if chosen_volume is None:
        chosen_volume = config.LOT_SIZE

    # ---- 3. Price the trade at the volume actually chosen -----------
    described = money.describe(
        entry_price=entry,
        stop_loss=stop,
        direction=signal,
        volume=chosen_volume,
        spread=features.get("spread"),
        symbol_profile=symbol_profile,
    )

    described.update({
        "volume": chosen_volume,
        "planned_entry": entry,
        "planned_stop_loss": stop,
        "planned_take_profit": placement["take_profit"],
        "stop_model": placement["model_used"],
        "stop_clamped": placement["clamped"],
        "sizing": sizing_detail,
    })

    return described


async def execute_and_record(symbol, signal, decision, market_data,
                             planned_risk=None):
    """
    Write the intent, send the order, then record the outcome.

    The PENDING row is written BEFORE the order is sent. If this
    process dies mid-flight, the reconciler resolves that row against
    MT5's history instead of re-sending - which is what stops a restart
    from duplicating an order.
    """

    features = market_data.get("features") or {}

    client_order_id = new_client_order_id()

    # Phase 2: money risk is known before the order exists, not after.
    # Reuse the figure the risk engine judged, so the row records the
    # trade that was actually authorised rather than a re-derivation.
    risk = planned_risk or plan_risk(symbol, signal, features, config.LOT_SIZE)

    if risk.get("risk_amount") is None:
        repo.insert_event(
            f"{symbol}: risk_amount could not be computed - no broker "
            f"specification for this symbol. Trade will be recorded "
            f"without a money-risk figure.",
            level="WARN",
            category="RISK",
            symbol=symbol,
        )

    trade_id = repo.insert_trade_intent({
        "client_order_id": client_order_id,
        "symbol": symbol,
        "direction": signal,
        "volume": risk.get("volume") or config.LOT_SIZE,
        "requested_price": features.get("ask") if signal == "BUY"
        else features.get("bid"),
        "stop_model": risk.get("stop_model"),
        "reason": decision.get("reasoning"),
        "decision_id": decision.get("id"),
        "magic": config.MAGIC_NUMBER,
        "market_regime": decision.get("market_regime")
        or features.get("market_regime"),
        "setup": decision.get("setup"),
        "ai_score": decision.get("ai_score"),
        "timeframe": decision.get("timeframe"),
        "news_condition": news.news_condition(decision.get("news")),

        "risk_amount": risk.get("risk_amount"),
        "stop_distance_price": risk.get("stop_distance_price"),
        "stop_distance_effective": risk.get("stop_distance_effective"),
        "spread_at_entry": risk.get("spread_at_entry"),
    })

    repo.update_decision(decision["id"], trade_id=trade_id)

    # Phase 5: the order goes out at the sized volume and the versioned
    # stop, not a fixed lot and a percent stop. Falls back inside
    # execute_trade when these are None.
    result = await asyncio.to_thread(
        execute_trade,
        symbol,
        signal,
        client_order_id,
        risk.get("volume"),
        risk.get("planned_stop_loss"),
        risk.get("planned_take_profit"),
    )

    status = result.get("status")

    if status == "EXECUTED":

        # Re-derive risk from the ACTUAL fill. Slippage moves the entry,
        # and with it the real distance to the stop - so the planned
        # figure is refined rather than trusted.
        filled = money.describe(
            entry_price=result.get("entry_price"),
            stop_loss=result.get("stop_loss"),
            direction=signal,
            volume=result.get("filled_volume") or result.get("volume"),
            spread=features.get("spread"),
            symbol_profile=(
                repo_meta.get_symbol_profile(bot_state["account_id"], symbol)
                if bot_state.get("account_id") is not None else None
            ),
        )

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

            risk_amount=filled.get("risk_amount"),
            stop_distance_price=filled.get("stop_distance_price"),
            stop_distance_effective=filled.get("stop_distance_effective"),
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

def latest_closed_bar_time(symbol):
    """
    The open time of the newest CLOSED H1 bar, as an ISO UTC string.

    One cheap MT5 call per symbol per poll. Returns None when the
    symbol is unavailable, which the caller treats as "nothing new".
    """

    import MetaTrader5 as mt5

    rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_H1, 1, 1)

    if rates is None or len(rates) == 0:
        return None

    offset = bot_state.get("server_utc_offset_min")

    return str(
        data_engine.to_utc([rates[0]["time"]], offset)[0]
    )


def symbol_has_new_bar(symbol):
    """
    Has this symbol produced a bar we have not decided on yet?

    Compared against the database rather than in-memory state, so the
    cadence survives a restart with nothing extra to persist and
    nothing that can drift out of sync.
    """

    current = latest_closed_bar_time(symbol)

    if current is None:
        return False, None

    decided = repo.latest_decided_bar(symbol)

    return (decided is None or current > decided), current


async def run_cycle(cycle_id, symbols):
    """One pass over the given symbols."""

    for symbol in symbols:

        # Bot may have been stopped while processing.
        if not bot_state["is_running"]:
            break

        try:
            await process_symbol(symbol, cycle_id)

        except Exception as e:                      # noqa: BLE001
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


def run_scheduled_exits():
    """Profile-driven flatten windows (Phase 5)."""

    try:
        profile = risk_profiles.load()
    except Exception:                               # noqa: BLE001
        return None

    return exits.run_scheduled_exits(
        profile, bot_state.get("server_utc_offset_min")
    )


async def trading_loop():
    """
    Main AI trading loop.

    Two cadences (Phase 4):

      per_bar   poll every DECISION_POLL_SECONDS, but decide for a
                symbol only when its H1 bar has advanced. The data
                changes hourly; deciding 120 times per bar on the same
                34 candles was measuring model noise, not the market
                (Phase 0 weakness #6).

      interval  the original behaviour, retained as experiment arm E2
                so 'per bar vs 30s' can be answered with data rather
                than opinion.

    Reconciliation runs on EVERY poll in both modes. Settling closed
    trades and keeping the ledger current must not wait for a new bar.
    """

    while True:

        if not bot_state["is_running"]:
            # Bot is stopped, don't burn CPU
            await asyncio.sleep(1)
            continue

        per_bar = config.DECISION_MODE == "per_bar"

        cycle_id = uuid.uuid4().hex[:12]

        heartbeat_lock()

        if not ensure_mt5():
            repo.insert_event(
                "Cycle skipped - MT5 unavailable",
                level="ERROR",
                category="MT5",
            )

            await asyncio.sleep(
                config.DECISION_POLL_SECONDS if per_bar
                else bot_state["interval"]
            )
            continue

        # Settle anything that closed since the last poll. This is what
        # turns finished trades into P&L, experiences and ledger rows.
        try:
            from backend.core.reconciler import run_full_reconciliation

            await asyncio.to_thread(run_full_reconciliation)
        except Exception as error:                  # noqa: BLE001
            print(f"[ERROR] reconciliation: {error}")

        # Phase 5: time-driven exits run on EVERY poll, in both modes.
        # A flatten that only fires when a bar closes is a flatten that
        # misses its window.
        try:
            await asyncio.to_thread(run_scheduled_exits)
        except Exception as error:                  # noqa: BLE001
            print(f"[ERROR] scheduled exits: {error}")

        if per_bar:
            due = []

            for symbol in config.SYMBOLS:
                try:
                    has_new, bar_time = await asyncio.to_thread(
                        symbol_has_new_bar, symbol
                    )
                except Exception as error:          # noqa: BLE001
                    print(f"[ERROR] bar check {symbol}: {error}")
                    continue

                if has_new:
                    due.append(symbol)

            if due:
                print(f"[CYCLE {cycle_id}] new H1 bar on: {', '.join(due)}")

                await run_cycle(cycle_id, due)
            else:
                # Still record liveness even when nothing is due.
                bot_state["last_cycle_at"] = repo.utc_now()

            await asyncio.sleep(config.DECISION_POLL_SECONDS)

        else:
            await run_cycle(cycle_id, config.SYMBOLS)

            await asyncio.sleep(bot_state["interval"])
