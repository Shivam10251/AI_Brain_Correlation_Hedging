"""
Kill switch (Phase 3).

A single durable flag that stops the engine opening anything, checked
third in the risk engine so no money check can be reached around it.

Durable on purpose: it lives in engine_state, so a process restart does
NOT clear it. A halt that evaporates when the thing crashes is not a
halt - and a crash-loop is exactly when you most want the bot to stay
stopped.

Clearing it is deliberate and attributed: who, when, why.
"""

from datetime import datetime, timezone

STATE_KEY = "kill_switch"


def _now():
    return datetime.now(timezone.utc).isoformat()


def get_state():
    """
    Current state, always a dict.

    `active` is the only field callers should branch on.
    """

    from backend.database import repository as repo

    state = repo.get_state(STATE_KEY)

    if not isinstance(state, dict):
        return {"active": False}

    return {"active": bool(state.get("active")), **state}


def is_active():
    return bool(get_state().get("active"))


def engage(reason=None, source="api"):
    """
    Halt the engine. Idempotent - re-engaging keeps the original
    timestamp and reason so the audit trail is not overwritten.
    """

    from backend.database import repository as repo

    existing = get_state()

    if existing.get("active"):
        return existing

    state = {
        "active": True,
        "reason": reason or "halted",
        "source": source,
        "set_at": _now(),
    }

    repo.set_state(STATE_KEY, state)

    repo.insert_event(
        f"KILL SWITCH ENGAGED ({source}): {state['reason']}. "
        f"No new orders will be opened until it is cleared.",
        level="ERROR",
        category="RISK",
        data=state,
    )

    return state


def clear(reason=None, source="api"):
    """Resume. Recorded as loudly as the halt was."""

    from backend.database import repository as repo

    previous = get_state()

    state = {
        "active": False,
        "cleared_at": _now(),
        "cleared_reason": reason,
        "source": source,
        "previous": previous,
    }

    repo.set_state(STATE_KEY, state)

    repo.insert_event(
        f"Kill switch cleared ({source})"
        + (f": {reason}" if reason else "")
        + f". Was engaged at {previous.get('set_at')} for "
        f"{previous.get('reason')!r}.",
        level="WARN",
        category="RISK",
        data=state,
    )

    return state
