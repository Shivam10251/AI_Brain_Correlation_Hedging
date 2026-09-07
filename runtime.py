"""
Engine runtime: shared state, the MT5 connection, the single-writer
lock, and startup recovery.

Everything here is about the PROCESS - what state it holds, whether it
owns the trading loop, and how it rebuilds itself from the database
after a restart. The trading cycle itself lives in engine.py.
"""

import os
import socket
import uuid
from datetime import datetime, timezone

import MetaTrader5 as mt5

import config
from database import repository as repo


# =====================================================================
# SHARED STATE
#
# This dict is now a CACHE of what is already in the database, not the
# system of record. Every field here is rebuilt on startup by
# restore_state().
# =====================================================================

bot_state = {
    "is_running": False,
    "interval": config.DEFAULT_INTERVAL,
    "equity": 0.0,
    "last_logic": "",
    "last_confidence": 0,
    "trade_history": [],

    # added
    "mt5_connected": False,
    "last_cycle_at": None,
    "last_error": None,
    "symbols": config.SYMBOLS,
    "owns_engine_lock": False,
}


_engine_lock_id = uuid.uuid4().hex


# =====================================================================
# MT5 CONNECTION
# =====================================================================

def connect_mt5():
    """
    Initialise MT5.

    A failure is logged and recorded, NOT raised: the dashboard must
    still start and serve history when the terminal is closed.
    """

    try:
        if mt5.initialize():
            bot_state["mt5_connected"] = True
            bot_state["last_error"] = None

            print("MT5 initialized successfully.")

            repo.insert_event(
                "MT5 connected", level="INFO", category="MT5"
            )

            return True

        error = mt5.last_error()

    except Exception as exception:                  # noqa: BLE001
        error = str(exception)

    bot_state["mt5_connected"] = False
    bot_state["last_error"] = f"MT5 initialization failed: {error}"

    print(f"[WARN] {bot_state['last_error']}")

    repo.insert_event(
        f"MT5 initialization failed: {error}",
        level="ERROR",
        category="MT5",
    )

    return False


def ensure_mt5():
    """Reconnect if the terminal dropped."""

    if bot_state["mt5_connected"] and mt5.terminal_info() is not None:
        return True

    bot_state["mt5_connected"] = False

    return connect_mt5()


# =====================================================================
# SINGLE-WRITER LOCK
#
# Guards against two processes (e.g. uvicorn --reload, or a second
# terminal) both running the loop against the same account.
# =====================================================================

LOCK_STALE_SECONDS = 90


def acquire_engine_lock():
    existing = repo.get_state("engine_lock")

    if existing:
        try:
            heartbeat = datetime.fromisoformat(existing["heartbeat"])
            age = (datetime.now(timezone.utc) - heartbeat).total_seconds()
        except (KeyError, TypeError, ValueError):
            age = LOCK_STALE_SECONDS + 1

        if age < LOCK_STALE_SECONDS and existing.get("id") != _engine_lock_id:
            print(
                f"[WARN] Another engine holds the lock "
                f"(pid={existing.get('pid')}, {age:.0f}s ago). "
                f"This process will serve the dashboard only."
            )

            repo.insert_event(
                f"Engine lock held by pid {existing.get('pid')}; "
                f"trading loop not started in pid {os.getpid()}",
                level="WARN",
                category="ENGINE",
            )

            return False

    heartbeat_lock()

    bot_state["owns_engine_lock"] = True

    return True


def heartbeat_lock():
    repo.set_state("engine_lock", {
        "id": _engine_lock_id,
        "pid": os.getpid(),
        "host": socket.gethostname(),
        "heartbeat": datetime.now(timezone.utc).isoformat(),
    })


def release_engine_lock():
    if bot_state.get("owns_engine_lock"):
        repo.set_state("engine_lock", None)
        bot_state["owns_engine_lock"] = False


# =====================================================================
# STARTUP RECOVERY
# =====================================================================

def refresh_cache(limit=100):
    """Rebuild the in-memory view of recent trades from the database."""

    trades = repo.get_trades(limit=limit)

    bot_state["trade_history"] = [
        {
            "id": trade["id"],
            "time": trade["created_at"],
            "asset": trade["symbol"],
            "signal": trade["direction"],
            "logic": trade.get("reason") or "",
            "status": trade["execution_status"],
            "pnl": trade.get("pnl"),
            "result": trade.get("result"),
            "score": trade.get("ai_score"),
        }
        for trade in reversed(trades)
    ]


def restore_state():
    """
    Rebuild every piece of runtime state from the database.

    This is what makes a restart invisible in the dashboard.
    """

    bot_state["interval"] = repo.get_state("interval", config.DEFAULT_INTERVAL)

    # Trading does NOT auto-resume unless explicitly configured, so an
    # unattended restart cannot start firing orders on its own.
    was_running = repo.get_state("is_running", False)

    bot_state["is_running"] = bool(
        was_running and config.RESUME_ENGINE_ON_STARTUP
    )

    latest_equity = repo.get_latest_equity()

    if latest_equity:
        bot_state["equity"] = latest_equity["equity"]

    decisions = repo.get_decisions(limit=1)

    if decisions:
        bot_state["last_logic"] = decisions[0].get("reasoning") or ""
        bot_state["last_confidence"] = decisions[0].get("ai_score") or 0

    refresh_cache()

    return {
        "trades": len(bot_state["trade_history"]),
        "equity": bot_state["equity"],
        "interval": bot_state["interval"],
        "resumed_running": bot_state["is_running"],
        "experiences": repo.count_experiences(),
    }


def persist_control_state():
    repo.set_state("is_running", bot_state["is_running"])
    repo.set_state("interval", bot_state["interval"])
