"""
FastAPI surface for the AI Hedge Fund Bot.

This module is deliberately thin: it starts the engine, serves the
dashboard, and exposes read endpoints over the database. The trading
logic lives in engine.py.

Restart safety: the trading loop is started ONCE here, at process
startup. Nothing the frontend can do starts, stops or restarts the
loop's lifecycle - /api/control only flips a flag the loop reads. A
browser refresh therefore cannot duplicate an MT5 order.
"""

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Query, Request
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

import MetaTrader5 as mt5

from backend import auth, config
from backend.ai import versioning
from backend.core import engine
from backend.core.engine import bot_state
from backend.database import analytics, explorer, initialize_database
from backend.database import repo_meta
from backend.database import repository as repo


# Resolved from this file rather than the working directory, so the app
# runs the same however uvicorn is invoked.
FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"

templates = Jinja2Templates(directory=str(FRONTEND_DIR / "templates"))


# ============================================================
# STARTUP / SHUTDOWN
# ============================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Restart recovery, in order:

      1. initialise the database
      2. restore engine + dashboard state from it
      3. connect to MT5
      4. reconcile the database against MT5
      5. start the trading loop (one instance only)
    """

    # 1. Database
    db_path = initialize_database()
    print(f"Database ready: {db_path}")

    # 1b. Freeze the running strategy (Phase 1).
    #
    # Registered BEFORE the loop starts, so the very first decision of
    # the process already carries a strategy_version_id. An unversioned
    # prompt edit shows up here as a new row rather than silently
    # polluting an existing version's trade population.
    try:
        version_id = versioning.ensure_registered()

        descriptor = versioning.current_descriptor()

        print(
            f"Strategy: {descriptor['strategy_id']} v{descriptor['version']} "
            f"(id={version_id}, prompt {descriptor['prompt_hash'][:12]}, "
            f"model {descriptor['model_alias']}, "
            f"temp {descriptor['temperature']})"
        )

        repo.insert_event(
            f"Strategy version {descriptor['strategy_id']} "
            f"v{descriptor['version']} active "
            f"(prompt_hash {descriptor['prompt_hash'][:12]})",
            level="INFO",
            category="STRATEGY",
            data={"strategy_version_id": version_id, **descriptor},
        )

    except Exception as error:                      # noqa: BLE001
        print(f"[ERROR] Strategy registration failed: {error}")

    # 1c. Security posture warning (Phase 1).
    if not config.AUTH_ENABLED:
        print(
            "[WARN] API_TOKEN is not set - /api/* is UNAUTHENTICATED. "
            "Bind to 127.0.0.1 only, or set API_TOKEN in .env."
        )

        repo.insert_event(
            "API_TOKEN is not set; control plane is unauthenticated",
            level="WARN",
            category="SECURITY",
        )

    # 2. Restore
    restored = engine.restore_state()
    print(
        f"State restored: {restored['trades']} trade(s), "
        f"{restored['experiences']} experience(s), "
        f"equity {restored['equity']}, "
        f"interval {restored['interval']}s, "
        f"engine {'RESUMED' if restored['resumed_running'] else 'stopped'}"
    )

    repo.insert_event(
        "Backend started", level="INFO", category="ENGINE",
        data=restored,
    )

    # 3. MT5 (non-fatal - the dashboard must still serve history)
    engine.connect_mt5()

    # 3b. Broker profile (Phase 1).
    #
    # Measures the server UTC offset, reads margin mode and captures
    # every per-symbol specification. Until this runs, the engine is
    # working from assumptions - which is what Phase 0 §2.2 and §2.6
    # were about.
    if bot_state["mt5_connected"]:
        try:
            from backend.market import broker_profile

            profile = await asyncio.to_thread(broker_profile.capture)

            if profile.get("available"):
                bot_state["account_id"] = profile["account_id"]
                bot_state["server_utc_offset_min"] = (
                    profile["server_utc_offset_min"]
                )
                bot_state["margin_mode"] = profile["margin_mode_label"]

                offset = profile["server_utc_offset_min"]

                print(
                    f"Broker profile: account {profile['account_id']} on "
                    f"{profile['server']} · server offset "
                    f"{'UNKNOWN' if offset is None else f'{offset:+d} min'}"
                    f"{'' if profile['offset_stable'] else ' (UNSTABLE)'} · "
                    f"{profile['margin_mode_label']}"
                )

                for entry in profile["symbols"]:
                    print(
                        f"  {entry['symbol']}: filling={entry['filling']} "
                        f"digits={entry['digits']} "
                        f"stops_level={entry['stops_level']} "
                        f"volume_step={entry['volume_step']}"
                    )

                if profile["unavailable_symbols"]:
                    print(
                        f"  [ERROR] unavailable on this broker: "
                        f"{', '.join(profile['unavailable_symbols'])}"
                    )
            else:
                print(f"[WARN] Broker profile unavailable: {profile.get('error')}")

        except Exception as error:                  # noqa: BLE001
            print(f"[ERROR] Broker profile capture failed: {error}")

    # 4. Reconcile
    if bot_state["mt5_connected"]:
        try:
            from backend.core.reconciler import run_full_reconciliation

            summary = await asyncio.to_thread(run_full_reconciliation)

            print(f"Reconciliation: {summary}")

            repo.insert_event(
                f"Reconciled with MT5: {summary['resolved']} resolved, "
                f"{summary['closed']} closed, {summary['adopted']} adopted",
                level="INFO",
                category="RECONCILE",
                data=summary,
            )

        except Exception as error:                  # noqa: BLE001
            print(f"[ERROR] Reconciliation failed: {error}")

        engine.refresh_cache()

    # 5. Trading loop - single instance
    task = None

    if engine.acquire_engine_lock():
        task = asyncio.create_task(engine.trading_loop())
        print("AI Hedge Fund trading loop started.")
    else:
        print("Trading loop NOT started (another process owns it).")

    yield

    # ------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------

    if task is not None:
        task.cancel()

        try:
            await task
        except asyncio.CancelledError:
            pass

    engine.persist_control_state()
    engine.release_engine_lock()

    repo.insert_event("Backend stopped", level="INFO", category="ENGINE")

    mt5.shutdown()

    print("MT5 connection closed.")


app = FastAPI(
    title="AI Hedge Fund Bot",
    version="2.0.0",
    lifespan=lifespan,
)


# ============================================================
# AUTH (Phase 1)
#
# Every /api/* route except /api/health requires the bearer token
# from .env when API_TOKEN is set. Enforced as middleware so a route
# added in a later phase inherits protection instead of relying on
# somebody remembering a decorator.
#
# The HTML pages stay open and receive the token server-side, so both
# dashboards keep working with no login step.
# ============================================================

@app.middleware("http")
async def enforce_api_token(request: Request, call_next):

    if auth.path_requires_token(request.url.path):

        rejection = auth.check_request(request)

        if rejection is not None:
            return rejection

    return await call_next(request)


# ============================================================
# REQUEST MODEL
# ============================================================

class ControlRequest(BaseModel):
    action: str | None = None
    interval: int | None = None


# ============================================================
# DASHBOARD
# ============================================================

@app.get("/")
async def home(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "request": request,
            # Phase 1: the page authenticates its own fetch() calls.
            # Safe because the page is served to a browser on this
            # host - anyone who can load it is already here.
            "api_token": auth.token_for_template(),
        },
    )


@app.get("/dashboard")
async def quant_db_dashboard(request: Request):
    """
    Quant DB Dashboard - a read-only visual explorer over the engine's
    SQLite database (data/quantbot.db). It never writes to the database.
    """

    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "request": request,
            "api_token": auth.token_for_template(),
        },
    )


# ============================================================
# API: QUANT DB DASHBOARD (read-only database explorer)
#
# Every handler runs blocking sqlite work in a worker thread so the
# trading loop and the rest of the dashboard keep serving. The
# explorer module opens its own PRAGMA query_only connection - none of
# these endpoints can mutate data/quantbot.db.
# ============================================================

@app.get("/api/db/meta")
async def db_meta():
    return await asyncio.to_thread(explorer.database_metadata)


@app.get("/api/db/tables")
async def db_tables():
    if not await asyncio.to_thread(explorer.database_exists):
        return {"available": False, "tables": []}

    return {"available": True, "tables": await asyncio.to_thread(explorer.list_tables)}


@app.get("/api/db/schema")
async def db_schema():
    if not await asyncio.to_thread(explorer.database_exists):
        return {"available": False, "schema": []}

    return {"available": True, "schema": await asyncio.to_thread(explorer.schema_overview)}


@app.get("/api/db/table/{name}")
async def db_table(
    name: str,
    limit: int = Query(explorer.DEFAULT_PAGE_SIZE, ge=1, le=explorer.MAX_PAGE_SIZE),
    offset: int = Query(0, ge=0),
    order_by: str | None = None,
    order_dir: str = Query("asc", pattern="^(asc|desc)$"),
    search: str | None = None,
):
    if not await asyncio.to_thread(explorer.database_exists):
        return {"status": "error", "message": "Database file not found yet."}

    try:
        return await asyncio.to_thread(
            explorer.table_data,
            name,
            limit=limit,
            offset=offset,
            order_by=order_by,
            order_dir=order_dir,
            search=search,
        )
    except ValueError as error:
        return {"status": "error", "message": str(error)}


class QueryRequest(BaseModel):
    sql: str
    max_rows: int | None = None


@app.post("/api/db/query")
async def db_query(request: QueryRequest):
    if not await asyncio.to_thread(explorer.database_exists):
        return {"status": "error", "message": "Database file not found yet."}

    try:
        return await asyncio.to_thread(
            explorer.run_query,
            request.sql,
            max_rows=request.max_rows or explorer.MAX_QUERY_ROWS,
        )
    except ValueError as error:
        return {"status": "error", "message": str(error)}


@app.get("/api/db/quant")
async def db_quant():
    return await asyncio.to_thread(explorer.quant_dashboard)


# ============================================================
# API: PROVENANCE (Phase 1)
#
# What strategy is running, and what the broker actually said. Both
# are read from the database, so they reflect what was recorded rather
# than what the code assumes.
# ============================================================

@app.get("/api/meta/strategy")
async def meta_strategy():
    """The running strategy plus every version ever registered."""

    def _read():
        descriptor = versioning.current_descriptor()

        return {
            "current": descriptor,
            "registered": repo_meta.get_strategy_versions(limit=50),
        }

    return await asyncio.to_thread(_read)


@app.get("/api/meta/broker")
async def meta_broker():
    """
    Measured broker facts: server UTC offset, margin mode, and the
    per-symbol specification used for filling mode and deviation.
    """

    def _read():
        profiles = repo_meta.get_broker_profiles()

        return {
            "accounts": [
                {
                    **profile,
                    "offset_samples": repo_meta.decode_json(
                        profile.get("offset_samples_json"), []
                    ),
                    "terminal": repo_meta.decode_json(
                        profile.get("terminal_json"), {}
                    ),
                    "symbols": repo_meta.get_symbol_profiles(
                        profile["account_id"]
                    ),
                }
                for profile in profiles
            ],
        }

    return await asyncio.to_thread(_read)


# ============================================================
# API: CONTROL BOT
# ============================================================

@app.post("/api/control")
async def control_bot(request: ControlRequest):
    """
    Start/stop the trading bot and optionally change
    the trading interval.

    This only toggles a flag. It never creates or restarts the trading
    loop, so it cannot duplicate an order.
    """

    if request.action is not None:
        action = request.action.lower()

        if action == "start":
            bot_state["is_running"] = True

            repo.insert_event(
                "Engine started from dashboard",
                level="INFO", category="ENGINE",
            )

        elif action == "stop":
            bot_state["is_running"] = False

            repo.insert_event(
                "Engine stopped from dashboard",
                level="INFO", category="ENGINE",
            )

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

    engine.persist_control_state()

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
    Current bot state.

    The original keys (is_running, interval, equity, last_logic,
    last_confidence, trade_history) are preserved for compatibility.
    Everything else is additive.
    """

    latest_decisions = repo.get_latest_decision_per_symbol()

    return {
        **bot_state,

        # Per-symbol decisions. The old dashboard rendered a single
        # global last_logic that every symbol in the cycle overwrote,
        # which is why the AI panel could disagree with the feed.
        "decisions_by_symbol": [
            {
                "id": d["id"],
                "symbol": d["symbol"],
                "created_at": d["created_at"],
                "status": d["status"],
                "ai_signal": d["ai_signal"],
                "final_decision": d["final_decision"],
                "override_reason": d["override_reason"],
                "ai_score": d["ai_score"],
                "reasoning": d["reasoning"],
                "market_regime": d["market_regime"],
                "setup": d["setup"],
                "trade_id": d["trade_id"],
            }
            for d in latest_decisions
        ],

        "market_states": repo.get_latest_market_state_per_symbol(),
    }


# ============================================================
# API: HISTORY (survives restart - read straight from SQLite)
# ============================================================

@app.get("/api/history/trades")
async def history_trades(
    limit: int = Query(200, ge=1, le=2000),
    symbol: str | None = None,
    since_id: int | None = None,
):
    return {"trades": repo.get_trades(
        limit=limit, symbol=symbol, since_id=since_id
    )}


@app.get("/api/history/decisions")
async def history_decisions(
    limit: int = Query(100, ge=1, le=2000),
    symbol: str | None = None,
    since_id: int | None = None,
):
    return {"decisions": repo.get_decisions(
        limit=limit, symbol=symbol, since_id=since_id
    )}


@app.get("/api/history/equity")
async def history_equity(limit: int = Query(500, ge=1, le=5000)):
    """
    Raw equity points, oldest first.

    The chart is rebuilt from these on every page load - the rendered
    graph itself is never stored.
    """

    return {"snapshots": repo.get_equity_snapshots(limit=limit)}


@app.get("/api/history/events")
async def history_events(
    limit: int = Query(200, ge=1, le=2000),
    since_id: int | None = None,
):
    return {"events": repo.get_events(limit=limit, since_id=since_id)}


# ============================================================
# API: ANALYTICS
# ============================================================

@app.get("/api/analytics")
async def get_analytics(
    symbol: str | None = None,
    setup: str | None = None,
    market_regime: str | None = None,
    timeframe: str | None = None,
):
    return {
        "overall": analytics.compute_metrics(
            symbol=symbol,
            setup=setup,
            market_regime=market_regime,
            timeframe=timeframe,
        ),
        "daily_pnl": analytics.daily_pnl(),
    }


@app.get("/api/analytics/breakdown/{dimension}")
async def get_breakdown(dimension: str):
    try:
        return {"dimension": dimension, "groups": analytics.breakdown(dimension)}
    except ValueError as error:
        return {"status": "error", "message": str(error)}


# ============================================================
# API: LIVE MT5 STATE (source of truth)
# ============================================================

@app.get("/api/positions")
async def get_positions():
    """
    Open positions read live from MT5 - not from the database.

    MT5 is authoritative for position state; the database is a mirror.
    """

    if not bot_state["mt5_connected"]:
        return {"status": "disconnected", "positions": []}

    positions = mt5.positions_get()

    return {
        "status": "ok",
        "positions": [
            {
                "ticket": p.ticket,
                "symbol": p.symbol,
                "type": "BUY" if p.type == mt5.POSITION_TYPE_BUY else "SELL",
                "volume": p.volume,
                "price_open": p.price_open,
                "price_current": p.price_current,
                "sl": p.sl,
                "tp": p.tp,
                "profit": p.profit,
                "magic": p.magic,
                "ours": p.magic == config.MAGIC_NUMBER,
            }
            for p in (positions or [])
        ],
    }


@app.get("/api/health")
async def health():
    """
    Liveness probe. Deliberately unauthenticated so an external
    watchdog can poll it without holding the token.
    """

    return {
        "status": "ok",
        "mt5_connected": bot_state["mt5_connected"],
        "is_running": bot_state["is_running"],
        "owns_engine_lock": bot_state["owns_engine_lock"],
        "last_cycle_at": bot_state["last_cycle_at"],
        "last_error": bot_state["last_error"],
        "memory_enabled": config.MEMORY_ENABLED,
        "news_enabled": config.NEWS_ENABLED,

        # Phase 1 provenance
        "auth_enabled": config.AUTH_ENABLED,
        "account_id": bot_state.get("account_id"),
        "server_utc_offset_min": bot_state.get("server_utc_offset_min"),
        "margin_mode": bot_state.get("margin_mode"),
        "strategy_version_id": bot_state.get("strategy_version_id"),
    }
