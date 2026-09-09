"""
Phase 3 acceptance tests: Risk Engine v1.

Acceptance:
  * all 14 checks have a failing-state test, refused with a DISTINCT
    reason
  * a decision that passes all checks produces one order
  * the The5ers profile matches the firm's published numbers
  * the kill switch stops new orders within one cycle

Every check also gets a boundary test at exactly the limit, because
"<" versus "<=" on a daily loss limit is the difference between
passing an evaluation and failing it.
"""

from datetime import datetime, timezone

import pytest

import tests.fake_mt5 as fake_mt5

from backend.risk import engine as risk_engine


# ---------------------------------------------------------------------
# A context that passes all fourteen checks. Each test breaks exactly
# one thing, so a refusal can only be attributed to that one thing.
# ---------------------------------------------------------------------

def base_profile(**overrides):
    profile = {
        "profile_id": "test",
        "min_ai_score": 70,
        "max_open_positions_per_symbol": 1,
        "allow_hedging": False,
        "risk_pct": 0.5,
        "max_risk_per_trade_pct": 0.5,
        "daily_loss_limit_pct": 3.0,
        "daily_loss_buffer_pct": 0.5,
        "max_losses_per_day": 3,
        "max_loss_floor": 90_000.0,
        "max_loss_floor_buffer": 500.0,
        "max_spread_pct_of_stop": 20.0,
        "max_net_exposure_r": 1.5,
        "exposure_weights": {"EURUSD": 1.0, "GBPUSD": 1.0, "XAUUSD": 0.8},
        "news_blackout_pre_min": 15,
        "news_blackout_post_min": 15,
        "min_holding_bars": 1,
        "trading_windows": [],
    }
    profile.update(overrides)
    return profile


def passing_context(**overrides):
    ctx = dict(
        symbol="EURUSDm",
        signal="BUY",
        decision={"status": "ok", "ai_signal": "BUY", "ai_score": 80},
        profile=base_profile(),
        account={"equity": 100_000.0, "balance": 100_000.0,
                 "floating_pnl": 0.0},
        day={"exposure": 0.0, "loss_count": 0, "start_equity": 100_000.0},
        risk_amount=500.0,          # 0.5% of 100k
        stop_distance=0.0022,
        spread=0.00010,             # 4.5% of the stop
        open_trades=[],
        mt5_connected=True,
        kill_switch={"active": False},
        news=None,
        now=datetime(2026, 3, 11, 12, 0, tzinfo=timezone.utc),
    )
    ctx.update(overrides)
    return risk_engine.RiskContext(**ctx)


class TestAllChecksPass:

    def test_clean_context_is_allowed(self):
        verdict = risk_engine.evaluate(passing_context())

        assert verdict.allow is True, verdict.reason
        assert verdict.reason is None
        assert verdict.failed_check() is None

    def test_every_check_ran_and_passed(self):
        verdict = risk_engine.evaluate(passing_context())

        assert len(verdict.checks) == 14
        assert [c["name"] for c in verdict.checks] == list(
            risk_engine.CHECK_NAMES
        )
        assert all(c["status"] == risk_engine.PASSED for c in verdict.checks)


# =====================================================================
# One failing-state test per check
# =====================================================================

class TestEachCheckRefuses:
    """
    Acceptance: all 14 checks have a failing-state test that is refused
    with a distinct reason.
    """

    def test_01_ai_status(self):
        verdict = risk_engine.evaluate(passing_context(
            decision={"status": "error", "ai_signal": "BUY",
                      "ai_score": 80, "error": "timeout"}
        ))

        assert verdict.allow is False
        assert verdict.failed_check() == "ai_status"
        assert "unusable" in verdict.reason

    def test_02_mt5_connected(self):
        verdict = risk_engine.evaluate(passing_context(mt5_connected=False))

        assert verdict.failed_check() == "mt5_connected"
        assert "not connected" in verdict.reason

    def test_03_kill_switch(self):
        verdict = risk_engine.evaluate(passing_context(
            kill_switch={"active": True, "reason": "manual halt",
                         "set_at": "2026-03-11T09:00:00+00:00"}
        ))

        assert verdict.failed_check() == "kill_switch"
        assert "manual halt" in verdict.reason

    def test_04_min_score(self):
        verdict = risk_engine.evaluate(passing_context(
            decision={"status": "ok", "ai_signal": "BUY", "ai_score": 69}
        ))

        assert verdict.failed_check() == "min_score"
        assert "69" in verdict.reason

    def test_05_session_window(self):
        verdict = risk_engine.evaluate(passing_context(
            profile=base_profile(
                trading_windows=[{"start": "07:00", "end": "11:00"}]
            ),
            now=datetime(2026, 3, 11, 14, 0, tzinfo=timezone.utc),
        ))

        assert verdict.failed_check() == "session_window"
        assert "Outside" in verdict.reason

    def test_06_news_blackout(self):
        verdict = risk_engine.evaluate(passing_context(
            news={"state": "blackout", "event": "US CPI",
                  "minutes_to_event": 4}
        ))

        assert verdict.failed_check() == "news_blackout"
        assert "US CPI" in verdict.reason

    def test_07_spread(self):
        verdict = risk_engine.evaluate(passing_context(
            spread=0.0010, stop_distance=0.0022      # 45% of the stop
        ))

        assert verdict.failed_check() == "spread"
        assert "45" in verdict.reason

    def test_08_no_hedge(self):
        verdict = risk_engine.evaluate(passing_context(
            open_trades=[{"symbol": "EURUSDm", "direction": "SELL",
                          "position_ticket": 1, "risk_amount": 500.0}]
        ))

        assert verdict.failed_check() == "no_hedge"
        assert "hedging is prohibited" in verdict.reason

    def test_09_position_cap(self):
        verdict = risk_engine.evaluate(passing_context(
            open_trades=[{"symbol": "EURUSDm", "direction": "BUY",
                          "position_ticket": 1, "risk_amount": 500.0}]
        ))

        assert verdict.failed_check() == "position_cap"
        assert "cap" in verdict.reason

    def test_10_per_trade_risk(self):
        verdict = risk_engine.evaluate(passing_context(risk_amount=750.0))

        assert verdict.failed_check() == "per_trade_risk"
        assert "750" in verdict.reason

    def test_11_daily_loss(self):
        # 3% - 0.5% buffer = 2.5% of 100k = 2,500 allowance.
        verdict = risk_engine.evaluate(passing_context(
            day={"exposure": -2_200.0, "loss_count": 0,
                 "start_equity": 100_000.0}
        ))

        assert verdict.failed_check() == "daily_loss"
        assert "allowance" in verdict.reason

    def test_12_loss_streak(self):
        verdict = risk_engine.evaluate(passing_context(
            day={"exposure": 0.0, "loss_count": 3,
                 "start_equity": 100_000.0}
        ))

        assert verdict.failed_check() == "loss_streak"
        assert "limit of 3" in verdict.reason

    def test_13_account_floor(self):
        verdict = risk_engine.evaluate(passing_context(
            account={"equity": 90_800.0, "balance": 100_000.0,
                     "floating_pnl": 0.0}
        ))

        assert verdict.failed_check() == "account_floor"
        assert "floor" in verdict.reason

    def test_14_net_exposure(self):
        # Two correlated longs already open, each a full R.
        verdict = risk_engine.evaluate(passing_context(
            open_trades=[
                {"symbol": "GBPUSDm", "direction": "BUY",
                 "risk_amount": 500.0},
                {"symbol": "XAUUSDm", "direction": "BUY",
                 "risk_amount": 500.0},
            ]
        ))

        assert verdict.failed_check() == "net_exposure"
        assert "R" in verdict.reason

    def test_every_reason_is_distinct(self):
        """
        Acceptance: refused with a DISTINCT reason. A shared message
        would make a refusal ambiguous in the audit trail.
        """

        contexts = {
            "ai_status": passing_context(
                decision={"status": "error", "ai_signal": "BUY",
                          "ai_score": 80, "error": "x"}),
            "mt5_connected": passing_context(mt5_connected=False),
            "kill_switch": passing_context(
                kill_switch={"active": True, "reason": "halt"}),
            "min_score": passing_context(
                decision={"status": "ok", "ai_signal": "BUY", "ai_score": 10}),
            "session_window": passing_context(
                profile=base_profile(
                    trading_windows=[{"start": "07:00", "end": "11:00"}]),
                now=datetime(2026, 3, 11, 14, 0, tzinfo=timezone.utc)),
            "news_blackout": passing_context(
                news={"state": "blackout", "event": "CPI",
                      "minutes_to_event": 2}),
            "spread": passing_context(spread=0.0010),
            "no_hedge": passing_context(open_trades=[
                {"symbol": "EURUSDm", "direction": "SELL",
                 "risk_amount": 500.0}]),
            "position_cap": passing_context(open_trades=[
                {"symbol": "EURUSDm", "direction": "BUY",
                 "risk_amount": 500.0}]),
            "per_trade_risk": passing_context(risk_amount=5_000.0),
            "daily_loss": passing_context(
                day={"exposure": -2_400.0, "loss_count": 0,
                     "start_equity": 100_000.0}),
            "loss_streak": passing_context(
                day={"exposure": 0.0, "loss_count": 5,
                     "start_equity": 100_000.0}),
            "account_floor": passing_context(
                account={"equity": 90_100.0, "balance": 100_000.0}),
            "net_exposure": passing_context(open_trades=[
                {"symbol": "GBPUSDm", "direction": "BUY",
                 "risk_amount": 500.0},
                {"symbol": "XAUUSDm", "direction": "BUY",
                 "risk_amount": 500.0}]),
        }

        assert set(contexts) == set(risk_engine.CHECK_NAMES)

        reasons = {}

        for name, ctx in contexts.items():
            verdict = risk_engine.evaluate(ctx)

            assert verdict.allow is False, name
            assert verdict.failed_check() == name, (
                f"{name} was not the failing check; got "
                f"{verdict.failed_check()}"
            )

            reasons[name] = verdict.reason

        assert len(set(reasons.values())) == 14, "reasons are not distinct"


# =====================================================================
# Boundaries: exactly at the limit
# =====================================================================

class TestBoundaries:

    def test_score_exactly_at_the_floor_passes(self):
        assert risk_engine.evaluate(passing_context(
            decision={"status": "ok", "ai_signal": "BUY", "ai_score": 70}
        )).allow is True

    def test_one_below_the_floor_fails(self):
        assert risk_engine.evaluate(passing_context(
            decision={"status": "ok", "ai_signal": "BUY", "ai_score": 69}
        )).allow is False

    def test_risk_exactly_at_budget_passes(self):
        assert risk_engine.evaluate(passing_context(
            risk_amount=500.0
        )).allow is True

    def test_risk_a_cent_over_budget_fails(self):
        assert risk_engine.evaluate(passing_context(
            risk_amount=500.01
        )).allow is False

    def test_daily_loss_landing_exactly_on_the_allowance_passes(self):
        """2,500 allowance, 2,000 already lost, 500 more risked."""

        assert risk_engine.evaluate(passing_context(
            day={"exposure": -2_000.0, "loss_count": 0,
                 "start_equity": 100_000.0}
        )).allow is True

    def test_one_cent_past_the_allowance_fails(self):
        assert risk_engine.evaluate(passing_context(
            day={"exposure": -2_000.01, "loss_count": 0,
                 "start_equity": 100_000.0}
        )).allow is False

    def test_losses_one_below_the_limit_passes(self):
        assert risk_engine.evaluate(passing_context(
            day={"exposure": 0.0, "loss_count": 2,
                 "start_equity": 100_000.0}
        )).allow is True

    def test_floor_landing_exactly_on_it_passes(self):
        """Floor 90,000 + 500 buffer = 90,500. Equity 91,000 - 500 risk."""

        assert risk_engine.evaluate(passing_context(
            account={"equity": 91_000.0, "balance": 100_000.0}
        )).allow is True

    def test_a_cent_below_the_floor_fails(self):
        assert risk_engine.evaluate(passing_context(
            account={"equity": 90_999.99, "balance": 100_000.0}
        )).allow is False

    def test_spread_exactly_at_the_limit_passes(self):
        # 20% of a 0.0022 stop is 0.00044.
        assert risk_engine.evaluate(passing_context(
            spread=0.00044, stop_distance=0.0022
        )).allow is True

    def test_exposure_exactly_at_the_cap_passes(self):
        # Cap 1.5R: one open 0.5R long plus this 1.0R long.
        assert risk_engine.evaluate(passing_context(
            symbol="EURUSDm",
            open_trades=[{"symbol": "XAUUSDm", "direction": "BUY",
                          "risk_amount": 312.5}],
        )).allow is True


# =====================================================================
# Fail-closed behaviour
# =====================================================================

class TestFailsClosed:

    def test_unknown_risk_amount_refuses(self):
        """
        Phase 2 returns None when the broker specification is missing.
        Approving an unpriced trade would risk an unknown amount.
        """

        verdict = risk_engine.evaluate(passing_context(risk_amount=None))

        assert verdict.allow is False
        assert verdict.failed_check() == "per_trade_risk"
        assert "unknown" in verdict.reason

    def test_stale_news_feed_refuses(self):
        verdict = risk_engine.evaluate(passing_context(
            news={"state": "unknown", "reason": "calendar.json is 40m old"}
        ))

        assert verdict.allow is False
        assert "refused" in verdict.reason

    def test_unknown_spread_refuses_when_the_gate_is_configured(self):
        verdict = risk_engine.evaluate(passing_context(spread=None))

        assert verdict.allow is False
        assert verdict.failed_check() == "spread"

    def test_a_raising_check_refuses_rather_than_falls_through(self,
                                                               monkeypatch):
        """A risk gate that crashes must never read as 'allowed'."""

        def explode(ctx):
            raise RuntimeError("boom")

        monkeypatch.setattr(risk_engine, "check_07_spread", explode)
        monkeypatch.setattr(
            risk_engine, "CHECKS",
            tuple(
                explode if c.__name__ == "check_07_spread" else c
                for c in risk_engine.CHECKS
            ),
        )

        verdict = risk_engine.evaluate(passing_context())

        assert verdict.allow is False
        assert "raised" in verdict.reason

    def test_unmapped_symbol_still_consumes_exposure(self):
        """
        An instrument missing from the weight table must not slip past
        the cap by being unrecognised.
        """

        verdict = risk_engine.evaluate(passing_context(
            symbol="NZDCHFm",
            open_trades=[
                {"symbol": "EURUSDm", "direction": "BUY",
                 "risk_amount": 500.0},
            ],
            profile=base_profile(max_net_exposure_r=1.0),
        ))

        assert verdict.allow is False
        assert verdict.failed_check() == "net_exposure"


# =====================================================================
# Ordering and audit trail
# =====================================================================

class TestOrderingAndAudit:

    def test_first_failure_wins(self):
        """
        Kill switch (3) outranks the score floor (4), even when both
        would fail.
        """

        verdict = risk_engine.evaluate(passing_context(
            kill_switch={"active": True, "reason": "halt"},
            decision={"status": "ok", "ai_signal": "BUY", "ai_score": 1},
        ))

        assert verdict.failed_check() == "kill_switch"

    def test_later_checks_are_recorded_as_not_evaluated(self):
        verdict = risk_engine.evaluate(passing_context(mt5_connected=False))

        statuses = {c["name"]: c["status"] for c in verdict.checks}

        assert statuses["ai_status"] == risk_engine.PASSED
        assert statuses["mt5_connected"] == risk_engine.FAILED
        assert statuses["daily_loss"] == risk_engine.SKIPPED

        # All fourteen still present: a refusal explains itself.
        assert len(verdict.checks) == 14

    def test_verdict_serialises_for_the_decision_row(self):
        verdict = risk_engine.evaluate(passing_context(risk_amount=None))

        payload = verdict.as_dict()

        assert payload["allow"] is False
        assert payload["failed_check"] == "per_trade_risk"
        assert len(payload["checks"]) == 14

    def test_hold_signal_is_not_run_through_the_engine(self):
        """
        A HOLD has nothing to authorise. Recording fourteen refusals
        for it would bury the real refusals.
        """

        verdict = risk_engine.evaluate(passing_context(
            decision={"status": "ok", "ai_signal": "HOLD", "ai_score": 80}
        ))

        assert verdict.failed_check() == "ai_status"


# =====================================================================
# Profiles
# =====================================================================

class TestProfiles:

    def test_both_shipped_profiles_load_and_validate(self):
        from backend.risk import profiles

        for profile_id in ("mt5-demo", "the5ers-100k"):
            profile = profiles.load(profile_id, force=True)

            assert profile["profile_id"] == profile_id
            assert len(profile["_checksum"]) == 64

    def test_the5ers_matches_the_published_rules(self):
        """
        Acceptance: The5ers profile validated against Doc 2 numbers.
        Each assertion is a rule from the firm's published rulebook.
        """

        from backend.risk import profiles

        profile = profiles.load("the5ers-100k", force=True)

        # $100K account, $10,000 max loss -> floor at $90,000.
        assert profile["account_size"] == 100_000.0
        assert profile["max_loss_floor"] == 90_000.0
        assert profile["max_loss_floor_buffer"] > 0

        # 3% daily loss, hard breach.
        assert profile["daily_loss_limit_pct"] == 3.0
        assert profile["daily_loss_buffer_pct"] > 0

        # Hedging prohibited.
        assert profile["allow_hedging"] is False

        # News: firm says +/-2 min; we use a deliberate superset.
        assert profile["news_blackout_pre_min"] >= 2
        assert profile["news_blackout_post_min"] >= 2

        # Microscalping prohibited.
        assert profile["min_holding_bars"] >= 1

    def test_the5ers_risk_arithmetic_is_survivable(self):
        """
        Phase 0 §6: six consecutive full losses at 0.5% is $3,000,
        which IS the daily limit. The loss counter must bite first.
        """

        from backend.risk import profiles

        profile = profiles.load("the5ers-100k", force=True)

        risk_per_trade = profile["max_risk_per_trade_pct"]
        max_losses = profile["max_losses_per_day"]

        worst_day = risk_per_trade * max_losses

        assert worst_day < profile["daily_loss_limit_pct"], (
            f"{max_losses} losses at {risk_per_trade}% is {worst_day}%, "
            f"which reaches the {profile['daily_loss_limit_pct']}% daily "
            f"limit"
        )

    def test_rejects_a_trade_bigger_than_the_whole_day(self):
        from backend.risk.profiles import ProfileError, validate

        with pytest.raises(ProfileError, match="exceeds daily_loss_limit_pct"):
            validate({**base_profile(
                max_risk_per_trade_pct=5.0, daily_loss_limit_pct=3.0
            ), "profile_id": "bad"})

    def test_rejects_a_buffer_that_blocks_everything(self):
        from backend.risk.profiles import ProfileError, validate

        with pytest.raises(ProfileError, match="must be smaller than"):
            validate({**base_profile(
                daily_loss_limit_pct=3.0, daily_loss_buffer_pct=3.0
            ), "profile_id": "bad"})

    def test_rejects_hedging(self):
        from backend.risk.profiles import ProfileError, validate

        with pytest.raises(ProfileError, match="allow_hedging"):
            validate({**base_profile(allow_hedging=True), "profile_id": "bad"})

    def test_rejects_a_floor_above_the_account(self):
        from backend.risk.profiles import ProfileError, validate

        with pytest.raises(ProfileError, match="max_loss_floor"):
            validate({**base_profile(
                max_loss_floor=110_000.0, max_risk_per_trade_pct=0.5,
            ), "profile_id": "bad", "account_size": 100_000.0})

    def test_rejects_negative_limits(self):
        from backend.risk.profiles import ProfileError, validate

        with pytest.raises(ProfileError, match="negative"):
            validate({**base_profile(min_ai_score=-1), "profile_id": "bad"})

    def test_unknown_profile_names_what_is_available(self):
        from backend.risk.profiles import ProfileError, load

        with pytest.raises(ProfileError, match="mt5-demo"):
            load("does-not-exist", force=True)

    def test_edited_profile_registers_a_new_row(self, temp_db):
        """
        Historical decisions must keep pointing at the limits actually
        in force when they were made.
        """

        from backend.database import repo_meta
        from backend.risk import profiles

        original = profiles.load("mt5-demo", force=True)

        first = profiles.ensure_registered(original)

        edited = {**original, "daily_loss_limit_pct": 1.0}
        edited["_checksum"] = profiles.checksum(edited)

        second = profiles.ensure_registered(edited)

        assert first != second
        assert len(repo_meta.get_risk_profiles()) == 2

    def test_registration_is_idempotent(self, temp_db):
        from backend.risk import profiles

        profile = profiles.load("mt5-demo", force=True)

        assert profiles.ensure_registered(profile) == (
            profiles.ensure_registered(profile)
        )


class TestDerivedLimits:

    def test_daily_allowance_uses_the_higher_of_equity_and_balance(self):
        from backend.risk import profiles

        profile = base_profile(daily_loss_limit_pct=3.0,
                               daily_loss_buffer_pct=0.5)

        allowance = profiles.daily_loss_allowance(
            profile,
            {"start_equity": 102_000.0},
            {"balance": 100_000.0},
        )

        # 2.5% of 102,000
        assert allowance == pytest.approx(2_550.0)

    def test_no_daily_limit_configured_is_none(self):
        from backend.risk import profiles

        assert profiles.daily_loss_allowance(
            base_profile(daily_loss_limit_pct=0.0), {}, {"balance": 100_000}
        ) is None

    def test_risk_budget_is_a_share_of_balance(self):
        from backend.risk import profiles

        assert profiles.risk_budget(
            base_profile(max_risk_per_trade_pct=0.5), {"balance": 100_000.0}
        ) == pytest.approx(500.0)

    def test_floor_includes_the_buffer(self):
        from backend.risk import profiles

        assert profiles.effective_floor(base_profile()) == 90_500.0


# =====================================================================
# Exposure
# =====================================================================

class TestExposure:

    def test_broker_suffixes_are_normalised(self):
        from backend.risk.exposure import normalise_symbol

        assert normalise_symbol("EURUSDm") == "EURUSD"
        assert normalise_symbol("XAUUSD.raw") == "XAUUSD"
        assert normalise_symbol("GBPUSD_i") == "GBPUSD"
        assert normalise_symbol("EURUSD") == "EURUSD"

    def test_opposite_directions_offset(self):
        from backend.risk import exposure

        net, _ = exposure.net_exposure(
            [
                {"symbol": "EURUSDm", "direction": "BUY",
                 "risk_amount": 500.0},
                {"symbol": "GBPUSDm", "direction": "SELL",
                 "risk_amount": 500.0},
            ],
            base_profile(),
            risk_unit=500.0,
        )

        assert net == pytest.approx(0.0)

    def test_correlated_longs_accumulate(self):
        """Four correlated 1R longs is not four independent bets."""

        from backend.risk import exposure

        net, _ = exposure.net_exposure(
            [
                {"symbol": "EURUSDm", "direction": "BUY", "risk_amount": 500.0},
                {"symbol": "GBPUSDm", "direction": "BUY", "risk_amount": 500.0},
            ],
            base_profile(),
            risk_unit=500.0,
        )

        assert net == pytest.approx(2.0)

    def test_projection_is_forward_looking(self):
        from backend.risk import exposure

        projected, detail = exposure.projected_exposure(
            [{"symbol": "EURUSDm", "direction": "BUY", "risk_amount": 500.0}],
            base_profile(),
            risk_unit=500.0,
            symbol="GBPUSDm", direction="BUY", risk_amount=500.0,
        )

        assert projected == pytest.approx(2.0)
        assert detail["current_r"] == pytest.approx(1.0)
        assert detail["new_trade_r"] == pytest.approx(1.0)


# =====================================================================
# Kill switch
# =====================================================================

class TestKillSwitch:

    def test_engage_and_clear(self, temp_db):
        from backend.risk import killswitch

        assert killswitch.is_active() is False

        killswitch.engage("testing", source="test")

        assert killswitch.is_active() is True
        assert killswitch.get_state()["reason"] == "testing"

        killswitch.clear("done", source="test")

        assert killswitch.is_active() is False

    def test_engaging_twice_keeps_the_original_reason(self, temp_db):
        from backend.risk import killswitch

        killswitch.engage("first", source="test")
        killswitch.engage("second", source="test")

        assert killswitch.get_state()["reason"] == "first"

    def test_survives_a_restart(self, temp_db):
        """
        Durable on purpose: a halt that evaporates on crash is not a
        halt, and a crash-loop is when you most want it to hold.
        """

        from backend.database import close_connection
        from backend.risk import killswitch

        killswitch.engage("persist me", source="test")

        close_connection()

        assert killswitch.is_active() is True

    def test_is_logged_as_an_event(self, temp_db):
        from backend.database import repository as repo
        from backend.risk import killswitch

        killswitch.engage("audit me", source="test")

        messages = " ".join(e["message"] for e in repo.get_events(limit=10))

        assert "KILL SWITCH ENGAGED" in messages

    def test_stops_new_orders(self):
        verdict = risk_engine.evaluate(passing_context(
            kill_switch={"active": True, "reason": "halt"}
        ))

        assert verdict.allow is False


class TestKillSwitchApi:

    def test_halt_then_resume(self, client):
        response = client.post(
            "/api/risk/halt", json={"action": "halt", "reason": "drill"}
        )

        assert response.status_code == 200
        assert response.json()["kill_switch"]["active"] is True

        state = client.get("/api/risk/state").json()

        assert state["kill_switch"]["active"] is True

        response = client.post("/api/risk/halt", json={"action": "resume"})

        assert response.json()["kill_switch"]["active"] is False

    def test_flatten_is_honest_about_not_being_implemented(self, client):
        """
        Phase 5 owns the close path. Reporting a flatten that did not
        happen would be worse than saying so.
        """

        payload = client.post(
            "/api/risk/halt", json={"action": "halt", "flatten": True}
        ).json()

        assert payload["flatten"]["status"] == "unavailable"
        assert "NOT closed" in payload["flatten"]["message"]

    def test_bad_action_rejected(self, client):
        payload = client.post(
            "/api/risk/halt", json={"action": "nonsense"}
        ).json()

        assert payload["status"] == "error"

    def test_state_reports_the_live_limits(self, client):
        payload = client.get("/api/risk/state").json()

        assert payload["status"] == "ok"
        assert payload["profile"]["profile_id"] == "mt5-demo"
        assert len(payload["checks"]) == 14
        assert "daily_loss_allowance" in payload["limits"]


# =====================================================================
# Through the trading loop
# =====================================================================

class TestEngineIntegration:

    def _setup(self, fake_world, monkeypatch):
        from backend.core.runtime import bot_state
        from backend.market import broker_profile
        from backend.risk import profiles

        profiles.reset_cache()
        broker_profile.capture(["EURUSDm", "GBPUSDm", "XAUUSDm"])

        monkeypatch.setitem(bot_state, "account_id", fake_world.login)
        monkeypatch.setitem(bot_state, "server_utc_offset_min", 0)
        monkeypatch.setitem(bot_state, "mt5_connected", True)

    def test_verdict_is_persisted_on_the_decision(self, temp_db, fake_world,
                                                  monkeypatch):
        from backend.core import engine
        from backend.database import repository as repo

        self._setup(fake_world, monkeypatch)

        decision = {
            "symbol": "EURUSDm", "status": "ok",
            "ai_signal": "BUY", "ai_score": 20,       # below the floor
        }

        final, reason = engine.apply_risk_checks(
            "EURUSDm", decision,
            market_data={
                "features": {"ask": 1.1, "bid": 1.1, "spread": 0.00001},
                "account": {"equity": 10_000.0, "balance": 10_000.0,
                            "floating_pnl": 0.0},
            },
            risk={"risk_amount": 2.2, "stop_distance_price": 0.0022},
        )

        assert final == "HOLD"
        assert "below" in reason

        decision["final_decision"] = final
        decision["override_reason"] = reason

        repo.insert_decision(decision)

        stored = repo.get_decisions(limit=1)[0]

        assert stored["risk_checks_json"]
        assert "min_score" in stored["risk_checks_json"]
        assert stored["risk_profile_id"] is not None

    def test_a_clean_signal_is_allowed_through(self, temp_db, fake_world,
                                               monkeypatch):
        """Acceptance: a decision that passes all checks produces one order."""

        from backend.core import engine

        self._setup(fake_world, monkeypatch)

        final, reason = engine.apply_risk_checks(
            "EURUSDm",
            {"symbol": "EURUSDm", "status": "ok",
             "ai_signal": "BUY", "ai_score": 85},
            market_data={
                "features": {"ask": 1.1, "bid": 1.1, "spread": 0.00001},
                "account": {"equity": 10_000.0, "balance": 10_000.0,
                            "floating_pnl": 0.0},
            },
            risk={"risk_amount": 2.2, "stop_distance_price": 0.0022},
        )

        assert final == "BUY", reason
        assert reason is None

    def test_kill_switch_stops_orders_within_one_cycle(
        self, temp_db, fake_world, monkeypatch
    ):
        """Acceptance criterion, end to end through the gate."""

        from backend.core import engine
        from backend.risk import killswitch

        self._setup(fake_world, monkeypatch)

        market = {
            "features": {"ask": 1.1, "bid": 1.1, "spread": 0.00001},
            "account": {"equity": 10_000.0, "balance": 10_000.0,
                        "floating_pnl": 0.0},
        }
        risk = {"risk_amount": 2.2, "stop_distance_price": 0.0022}

        good = {"symbol": "EURUSDm", "status": "ok",
                "ai_signal": "BUY", "ai_score": 85}

        assert engine.apply_risk_checks(
            "EURUSDm", dict(good), market, risk
        )[0] == "BUY"

        killswitch.engage("halt now", source="test")

        final, reason = engine.apply_risk_checks(
            "EURUSDm", dict(good), market, risk
        )

        assert final == "HOLD"
        assert "Kill switch" in reason

    def test_hold_signals_skip_the_engine(self, temp_db, fake_world,
                                          monkeypatch):
        from backend.core import engine

        self._setup(fake_world, monkeypatch)

        decision = {"symbol": "EURUSDm", "status": "ok",
                    "ai_signal": "HOLD", "ai_score": 80}

        final, reason = engine.apply_risk_checks("EURUSDm", decision, {}, {})

        assert final == "HOLD"
        assert reason is None
        assert "risk_checks" not in decision
