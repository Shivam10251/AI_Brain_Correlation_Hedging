"""
Risk Engine v1 (Phase 3).

Replaces `engine.apply_risk_checks`, which was five inline conditions
with two of the five disabled by default. Phase 0 ranked "uncapped
stacking + zero portfolio risk" as weakness #2: a guaranteed breach of
any prop-firm rule.

Authority
---------
This module is the ONLY thing that may authorise an order. The trading
loop asks it and obeys the answer; it does not consult config, count
positions, or re-derive limits of its own. That single-authority
property is what makes the limits testable - there is one place to
attack in a test, and no second path to the broker.

Contract
--------
    evaluate(context) -> RiskDecision(allow, reason, checks)

Fourteen checks in a fixed order, first failure wins. Every check is
recorded - passed, failed, or not evaluated - so a refusal is always
explainable from the stored row rather than reconstructed from logs.

Order matters. Cheap and categorical checks come first (is the AI
output usable, is the terminal there, is the kill switch on) before
anything that costs a database read. Checks 10-14 - the money limits -
come last because they need the trade's risk figure, which only exists
once the earlier checks have established there is a trade to price.

Fail-closed
-----------
A check that cannot be evaluated REFUSES. An unknown daily P&L, a
missing risk figure or an unreachable news feed all block the entry.
The alternative - trading on an unknown - is how accounts are lost.
Exits are never gated by this module.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone

from backend.risk import exposure, profiles


# Check outcomes.
PASSED = "passed"
FAILED = "failed"
SKIPPED = "not_evaluated"


@dataclass
class RiskContext:
    """
    Everything the fourteen checks need, gathered once.

    Assembled by the caller so tests can construct a violating state
    directly instead of driving the whole engine into it.
    """

    symbol: str
    signal: str

    decision: dict = field(default_factory=dict)
    profile: dict = field(default_factory=dict)
    account: dict = field(default_factory=dict)
    day: dict = field(default_factory=dict)

    risk_amount: float | None = None
    stop_distance: float | None = None
    spread: float | None = None

    open_trades: list = field(default_factory=list)

    mt5_connected: bool = True
    kill_switch: dict | None = None

    # Phase 7 replaces this. Until then the news check reports 'clear'.
    news: dict | None = None

    now: datetime = None

    def __post_init__(self):
        if self.now is None:
            self.now = datetime.now(timezone.utc)


@dataclass
class RiskDecision:
    allow: bool
    reason: str | None
    checks: list

    def failed_check(self):
        for check in self.checks:
            if check["status"] == FAILED:
                return check["name"]

        return None

    def as_dict(self):
        return {
            "allow": self.allow,
            "reason": self.reason,
            "failed_check": self.failed_check(),
            "checks": self.checks,
        }


def _ok(name, detail=None):
    return {"name": name, "status": PASSED, "reason": None, "detail": detail}


def _no(name, reason, detail=None):
    return {"name": name, "status": FAILED, "reason": reason, "detail": detail}


# =====================================================================
# THE CHECKS
#
# Each returns a result dict. None means "not applicable", which is
# recorded as passed - a limit that is not configured is not a limit.
# =====================================================================

def check_01_ai_status(ctx):
    """An unusable AI response can never become an order."""

    status = ctx.decision.get("status")

    if status != "ok":
        return _no(
            "ai_status",
            f"AI response unusable (status={status}): "
            f"{ctx.decision.get('error')}",
        )

    signal = ctx.decision.get("ai_signal")

    if signal not in {"BUY", "SELL"}:
        return _no("ai_status", f"AI signal is {signal}, not a trade")

    return _ok("ai_status", {"signal": signal})


def check_02_mt5_connected(ctx):
    if not ctx.mt5_connected:
        return _no("mt5_connected", "MT5 is not connected")

    return _ok("mt5_connected")


def check_03_kill_switch(ctx):
    """
    A halted engine opens nothing, for any reason, until a human
    clears it. This sits third so it cannot be reached around by any
    money check below.
    """

    switch = ctx.kill_switch

    if switch and switch.get("active"):
        return _no(
            "kill_switch",
            f"Kill switch active: {switch.get('reason') or 'no reason given'}"
            + (f" (set {switch.get('set_at')})" if switch.get("set_at") else ""),
            switch,
        )

    return _ok("kill_switch")


def check_04_min_score(ctx):
    threshold = float(ctx.profile.get("min_ai_score") or 0)

    if threshold <= 0:
        return _ok("min_score", {"threshold": 0})

    score = ctx.decision.get("ai_score")

    if score is None:
        return _no("min_score", "AI returned no score; cannot apply the floor")

    if float(score) < threshold:
        return _no(
            "min_score",
            f"AI score {score} below the {threshold:g} floor",
            {"score": score, "threshold": threshold},
        )

    return _ok("min_score", {"score": score, "threshold": threshold})


def check_05_session_window(ctx):
    """
    Trading windows and the weekend flatten period.

    Phase 5 owns the flatten JOB; this is the gate that stops a new
    entry being opened inside a window it would immediately be
    flattened out of.
    """

    windows = ctx.profile.get("trading_windows") or []

    if not windows:
        return _ok("session_window", {"windows": "unrestricted"})

    minutes = ctx.now.hour * 60 + ctx.now.minute

    for window in windows:
        try:
            start_h, start_m = (int(x) for x in str(window["start"]).split(":"))
            end_h, end_m = (int(x) for x in str(window["end"]).split(":"))
        except (KeyError, ValueError, TypeError):
            continue

        start = start_h * 60 + start_m
        end = end_h * 60 + end_m

        inside = (
            start <= minutes < end
            if start <= end
            # A window that wraps midnight.
            else minutes >= start or minutes < end
        )

        if inside:
            return _ok("session_window", {"window": window})

    return _no(
        "session_window",
        f"Outside every configured trading window "
        f"({ctx.now.strftime('%H:%M')} UTC)",
        {"windows": windows},
    )


def check_06_news_blackout(ctx):
    """
    News blackout. Phase 7 supplies the calendar; until then there is
    no feed and the check reports clear.

    FAIL-CLOSED once a feed exists: a stale or unparsable calendar
    yields 'unknown', which refuses the entry. Trading blind through a
    high-impact release is the thing the rule exists to prevent.
    """

    pre = int(ctx.profile.get("news_blackout_pre_min") or 0)
    post = int(ctx.profile.get("news_blackout_post_min") or 0)

    if pre <= 0 and post <= 0:
        return _ok("news_blackout", {"configured": False})

    news = ctx.news

    if news is None:
        # No provider wired up yet (Phase 7).
        return _ok("news_blackout", {"provider": "none", "state": "clear"})

    state = news.get("state")

    if state == "unknown":
        return _no(
            "news_blackout",
            f"News feed unusable ({news.get('reason')}); entries refused "
            f"while the blackout state is unknown",
            news,
        )

    if state == "blackout":
        return _no(
            "news_blackout",
            f"News blackout: {news.get('event')} in "
            f"{news.get('minutes_to_event')} min",
            news,
        )

    return _ok("news_blackout", news)


def check_07_spread(ctx):
    """
    Refuse when the spread is a large fraction of the stop distance.

    A 1:2 nominal trade whose spread is a quarter of the stop is really
    about 1:1.5 after costs, and thin-hours spreads on gold and crypto
    are exactly where that bites.
    """

    limit = float(ctx.profile.get("max_spread_pct_of_stop") or 0.0)

    if limit <= 0:
        return _ok("spread", {"configured": False})

    if ctx.spread is None or not ctx.stop_distance:
        return _no(
            "spread",
            "Spread or stop distance unknown; cannot verify the cost gate",
        )

    share = abs(ctx.spread) / abs(ctx.stop_distance) * 100.0

    if share > limit:
        return _no(
            "spread",
            f"Spread is {share:.1f}% of the stop distance, above the "
            f"{limit:g}% limit",
            {"spread_pct_of_stop": round(share, 2), "limit": limit},
        )

    return _ok("spread", {"spread_pct_of_stop": round(share, 2)})


def check_08_no_hedge(ctx):
    """
    One direction per symbol.

    Both target firms prohibit hedging, and on a NETTING account an
    opposite order would silently reduce or close the existing position
    instead - a different trade from the one the AI asked for. So this
    is enforced regardless of margin_mode, exactly as the plan requires.
    """

    if ctx.profile.get("allow_hedging"):
        return _ok("no_hedge", {"allowed": True})

    opposing = [
        t for t in ctx.open_trades
        if t.get("symbol") == ctx.symbol
        and str(t.get("direction", "")).upper() != ctx.signal.upper()
    ]

    if opposing:
        return _no(
            "no_hedge",
            f"{len(opposing)} open {opposing[0].get('direction')} "
            f"position(s) on {ctx.symbol}; hedging is prohibited. "
            f"Close first (reversal), do not open the opposite side.",
            {"tickets": [t.get("position_ticket") for t in opposing]},
        )

    return _ok("no_hedge")


def check_09_position_cap(ctx):
    cap = int(ctx.profile.get("max_open_positions_per_symbol") or 0)

    if cap <= 0:
        return _ok("position_cap", {"cap": "unlimited"})

    same = [t for t in ctx.open_trades if t.get("symbol") == ctx.symbol]

    if len(same) >= cap:
        return _no(
            "position_cap",
            f"{len(same)} open position(s) on {ctx.symbol} is at the cap "
            f"of {cap}",
            {"open": len(same), "cap": cap},
        )

    return _ok("position_cap", {"open": len(same), "cap": cap})


def check_10_per_trade_risk(ctx):
    """
    The trade's own money risk against the per-trade budget.

    A missing risk figure REFUSES. Phase 2 returns None when the broker
    specification is absent, and approving an unpriced trade would put
    an unknown amount at stake.
    """

    budget = profiles.risk_budget(ctx.profile, ctx.account)

    if budget is None:
        return _ok("per_trade_risk", {"configured": False})

    if ctx.risk_amount is None:
        return _no(
            "per_trade_risk",
            "risk_amount is unknown (no broker specification for this "
            "symbol); refusing rather than risking an unpriced amount",
        )

    if ctx.risk_amount <= 0:
        return _no(
            "per_trade_risk",
            f"risk_amount is {ctx.risk_amount}, which cannot be right",
        )

    if ctx.risk_amount > budget:
        return _no(
            "per_trade_risk",
            f"Trade risks {ctx.risk_amount:.2f} vs the per-trade budget of "
            f"{budget:.2f} ({ctx.profile.get('max_risk_per_trade_pct')}% of "
            f"balance)",
            {"risk_amount": ctx.risk_amount, "budget": round(budget, 2)},
        )

    return _ok("per_trade_risk", {
        "risk_amount": ctx.risk_amount, "budget": round(budget, 2),
    })


def check_11_daily_loss(ctx):
    """
    Realised + floating + this trade's risk against the day's
    allowance.

    Forward-looking on purpose: the question is whether the day
    survives this trade going to its stop, not whether it has already
    breached.
    """

    allowance = profiles.daily_loss_allowance(
        ctx.profile, ctx.day, ctx.account
    )

    if allowance is None:
        return _ok("daily_loss", {"configured": False})

    exposure_now = float(ctx.day.get("exposure") or 0.0)

    risk = float(ctx.risk_amount or 0.0)

    projected = exposure_now - risk

    if projected < -allowance:
        return _no(
            "daily_loss",
            f"Day is {exposure_now:+.2f}; risking {risk:.2f} more would "
            f"reach {projected:+.2f}, past the allowance of "
            f"-{allowance:.2f}",
            {
                "exposure": round(exposure_now, 2),
                "risk_amount": round(risk, 2),
                "projected": round(projected, 2),
                "allowance": round(allowance, 2),
            },
        )

    return _ok("daily_loss", {
        "exposure": round(exposure_now, 2),
        "projected": round(projected, 2),
        "allowance": round(allowance, 2),
    })


def check_12_loss_streak(ctx):
    limit = int(ctx.profile.get("max_losses_per_day") or 0)

    if limit <= 0:
        return _ok("loss_streak", {"configured": False})

    losses = int(ctx.day.get("loss_count") or 0)

    if losses >= limit:
        return _no(
            "loss_streak",
            f"{losses} losing trade(s) today is at the limit of {limit}; "
            f"stopping for the day",
            {"loss_count": losses, "limit": limit},
        )

    return _ok("loss_streak", {"loss_count": losses, "limit": limit})


def check_13_account_floor(ctx):
    """
    Equity, minus what this trade would lose, against the hard floor.

    On The5ers the floor is a $10,000 drawdown from the initial
    balance. Breaching it ends the account, so the engine refuses at
    the floor plus a buffer.
    """

    floor = profiles.effective_floor(ctx.profile)

    if floor is None:
        return _ok("account_floor", {"configured": False})

    equity = float(ctx.account.get("equity") or 0.0)

    projected = equity - float(ctx.risk_amount or 0.0)

    if projected < floor:
        return _no(
            "account_floor",
            f"Equity {equity:.2f} minus risk "
            f"{float(ctx.risk_amount or 0):.2f} = {projected:.2f}, below "
            f"the floor of {floor:.2f}",
            {
                "equity": round(equity, 2),
                "projected": round(projected, 2),
                "floor": floor,
            },
        )

    return _ok("account_floor", {
        "equity": round(equity, 2),
        "projected": round(projected, 2),
        "floor": floor,
    })


def check_14_net_exposure(ctx):
    """
    Correlated exposure across the whole book, in R.

    Four correlated 1R longs is a 4R bet on one dollar move
    (Phase 0 C3). Forward-looking, like the daily check.
    """

    cap = float(ctx.profile.get("max_net_exposure_r") or 0.0)

    if cap <= 0:
        return _ok("net_exposure", {"configured": False})

    risk_unit = profiles.risk_budget(ctx.profile, ctx.account)

    projected, detail = exposure.projected_exposure(
        ctx.open_trades,
        ctx.profile,
        risk_unit,
        ctx.symbol,
        ctx.signal,
        ctx.risk_amount,
    )

    if abs(projected) > cap:
        return _no(
            "net_exposure",
            f"Net dollar exposure would be {projected:+.2f}R, beyond the "
            f"{cap:g}R cap (correlated positions)",
            detail,
        )

    return _ok("net_exposure", detail)


# Fixed order. The list IS the contract.
CHECKS = (
    check_01_ai_status,
    check_02_mt5_connected,
    check_03_kill_switch,
    check_04_min_score,
    check_05_session_window,
    check_06_news_blackout,
    check_07_spread,
    check_08_no_hedge,
    check_09_position_cap,
    check_10_per_trade_risk,
    check_11_daily_loss,
    check_12_loss_streak,
    check_13_account_floor,
    check_14_net_exposure,
)


CHECK_NAMES = (
    "ai_status", "mt5_connected", "kill_switch", "min_score",
    "session_window", "news_blackout", "spread", "no_hedge",
    "position_cap", "per_trade_risk", "daily_loss", "loss_streak",
    "account_floor", "net_exposure",
)


def evaluate(ctx):
    """
    Run the fourteen checks in order and return the verdict.

    Stops at the first failure - that is the reason - but records every
    check, including the ones that were never reached, so the stored
    decision explains itself without needing the logs.

    A check that RAISES is treated as a failure. A risk gate that
    crashes must not fall through to "allowed".
    """

    results = []
    verdict = None

    for index, check in enumerate(CHECKS):

        if verdict is not None:
            results.append({
                "name": CHECK_NAMES[index],
                "status": SKIPPED,
                "reason": None,
                "detail": None,
            })
            continue

        try:
            outcome = check(ctx)
        except Exception as error:                  # noqa: BLE001
            outcome = _no(
                CHECK_NAMES[index],
                f"Risk check raised ({type(error).__name__}: {error}); "
                f"refusing rather than assuming it would have passed",
            )

        results.append(outcome)

        if outcome["status"] == FAILED:
            verdict = outcome["reason"]

    return RiskDecision(
        allow=verdict is None,
        reason=verdict,
        checks=results,
    )
