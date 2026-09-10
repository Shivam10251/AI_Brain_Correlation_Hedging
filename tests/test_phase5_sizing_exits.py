"""
Phase 5 acceptance tests: sizing and exits.

Acceptance:
  * sizing hits risk_amount within one volume_step on three symbol types
  * BUY then SELL yields ONE net position and a REVERSAL exit row
  * flatten fires at the boundary in a clock-mocked test
  * partial close is handled
"""

from datetime import datetime, timedelta, timezone

import pytest

import tests.fake_mt5 as fake_mt5

from backend.risk import money, sizing, stops


MAGIC = 100001


def spec(symbol):
    return dict(fake_mt5.SYMBOL_SPECS[symbol])


def filling_for(symbol):
    """Pick a filling mode the fake symbol actually supports."""

    mask = fake_mt5.SYMBOL_SPECS[symbol]["filling_mode"]

    if mask & fake_mt5.SYMBOL_FILLING_FOK:
        return fake_mt5.ORDER_FILLING_FOK

    return fake_mt5.ORDER_FILLING_IOC


# =====================================================================
# Sizing  (Phase 0 weakness #4)
# =====================================================================

class TestSizing:

    @pytest.mark.parametrize("symbol,stop_distance", [
        ("EURUSDm", 0.0022),      # 22 pips
        ("BTCUSDm", 128.0),       # $128
        ("XAUUSDm", 4.80),        # $4.80
    ])
    def test_hits_target_risk_within_one_volume_step(self, symbol,
                                                     stop_distance):
        """
        Acceptance: within one volume_step of the target.

        Flooring to the step means the realised risk is always at or
        BELOW target - never above, which is the one direction the risk
        engine must not be surprised in.
        """

        symbol_profile = spec(symbol)

        target = 250.0

        result = sizing.compute_volume(target, stop_distance, symbol_profile)

        assert result["volume"] is not None, result["reason"]

        actual = result["risk_amount"]

        step_risk = money.risk_amount(
            stop_distance, symbol_profile["volume_step"], symbol_profile
        )

        assert actual <= target + 1e-9, "sizing risked MORE than the target"
        assert target - actual < step_risk, (
            f"{symbol}: off by more than one volume step "
            f"({target - actual} vs {step_risk})"
        )

    def test_volume_is_a_multiple_of_the_step(self):
        result = sizing.compute_volume(250.0, 0.0022, spec("EURUSDm"))

        step = spec("EURUSDm")["volume_step"]

        assert abs(result["volume"] / step - round(result["volume"] / step)) < 1e-9

    def test_floors_rather_than_rounds(self):
        """Rounding up would breach the per-trade budget."""

        symbol_profile = spec("EURUSDm")

        # A target landing between two steps.
        target = money.risk_amount(0.0022, 0.199, symbol_profile)

        result = sizing.compute_volume(target, 0.0022, symbol_profile)

        assert result["volume"] == pytest.approx(0.19)
        assert result["risk_amount"] <= target

    def test_refuses_when_the_minimum_lot_risks_too_much(self):
        """
        Silently trading the minimum would breach the budget the risk
        engine is enforcing - so it reports instead.
        """

        symbol_profile = spec("XAUUSDm")

        result = sizing.compute_volume(0.50, 4.80, symbol_profile)

        assert result["volume"] is None
        assert result["clamped"] == "below_minimum"
        assert "exceeds the target" in result["reason"]

    def test_clamps_to_the_brokers_maximum(self):
        symbol_profile = spec("BTCUSDm")     # volume_max 10.0

        result = sizing.compute_volume(1_000_000.0, 1.0, symbol_profile)

        assert result["volume"] <= symbol_profile["volume_max"]
        assert result["clamped"] == "at_maximum"

    def test_respects_a_profile_volume_cap(self):
        result = sizing.compute_volume(
            10_000.0, 0.0022, spec("EURUSDm"), max_volume=0.5
        )

        assert result["volume"] == pytest.approx(0.5)

    def test_missing_specification_returns_none(self):
        assert sizing.compute_volume(250.0, 0.0022, None)["volume"] is None
        assert sizing.compute_volume(250.0, 0.0, spec("EURUSDm"))["volume"] is None

    def test_scales_inversely_with_stop_distance(self):
        """A wider stop must mean a smaller position for the same risk."""

        tight = sizing.compute_volume(250.0, 0.0011, spec("EURUSDm"))
        wide = sizing.compute_volume(250.0, 0.0044, spec("EURUSDm"))

        assert tight["volume"] > wide["volume"]


class TestMarginCheck:

    def test_allows_an_order_that_fits(self, fake_world):
        ok, detail = sizing.margin_is_available(
            "EURUSDm", "BUY", 0.10, 1.10,
            {"margin_free": 10_000.0},
        )

        assert ok is True
        assert detail["required"] > 0

    def test_refuses_an_order_that_does_not(self, fake_world):
        ok, detail = sizing.margin_is_available(
            "EURUSDm", "BUY", 50.0, 1.10,
            {"margin_free": 100.0},
        )

        assert ok is False
        assert "exceeds" in detail["reason"]

    def test_keeps_a_safety_margin(self, fake_world):
        """
        A fill consuming ALL free margin leaves nothing for the adverse
        excursion before the stop, and MT5 starts closing positions
        itself.
        """

        required = fake_mt5.order_calc_margin(
            fake_mt5.ORDER_TYPE_BUY, "EURUSDm", 1.0, 1.10
        )

        # Exactly enough, but not enough with the safety factor.
        ok, _ = sizing.margin_is_available(
            "EURUSDm", "BUY", 1.0, 1.10, {"margin_free": required}
        )

        assert ok is False

    def test_defers_to_the_broker_when_it_cannot_answer(self, fake_world,
                                                        monkeypatch):
        monkeypatch.setattr(
            fake_mt5, "order_calc_margin", lambda *a, **k: None
        )

        ok, detail = sizing.margin_is_available(
            "EURUSDm", "BUY", 0.1, 1.10, {"margin_free": 1.0}
        )

        assert ok is True
        assert "deferring" in detail["reason"]


# =====================================================================
# Stop model as a versioned parameter
# =====================================================================

class TestStopModel:

    def test_percent_model_is_the_frozen_baseline(self):
        result = stops.compute(
            1.10000, "BUY", model=stops.PERCENT,
            sl_percent=0.002, tp_percent=0.004,
            symbol_profile=spec("EURUSDm"),
        )

        assert result["stop_loss"] == pytest.approx(1.09780, abs=1e-5)
        assert result["take_profit"] == pytest.approx(1.10440, abs=1e-5)
        assert result["model_used"] == stops.PERCENT

    def test_atr_model_scales_with_volatility(self):
        result = stops.compute(
            1.10000, "BUY", model=stops.ATR,
            atr=0.0015, k_sl=1.0, k_tp=2.0,
            symbol_profile=spec("EURUSDm"),
        )

        assert result["stop_loss"] == pytest.approx(1.09850, abs=1e-5)
        assert result["take_profit"] == pytest.approx(1.10300, abs=1e-5)
        assert result["model_used"] == stops.ATR

    def test_reward_risk_is_preserved_across_models(self):
        percent = stops.compute(
            1.1, "BUY", model=stops.PERCENT,
            sl_percent=0.002, tp_percent=0.004,
        )
        atr = stops.compute(
            1.1, "BUY", model=stops.ATR, atr=0.0015, k_sl=1.0, k_tp=2.0,
        )

        for result in (percent, atr):
            ratio = result["take_profit_distance"] / result["stop_distance"]
            assert ratio == pytest.approx(2.0)

    def test_sells_mirror_buys(self):
        buy = stops.compute(1.1, "BUY", sl_percent=0.002, tp_percent=0.004)
        sell = stops.compute(1.1, "SELL", sl_percent=0.002, tp_percent=0.004)

        assert buy["stop_loss"] < 1.1 < buy["take_profit"]
        assert sell["take_profit"] < 1.1 < sell["stop_loss"]

    def test_falls_back_to_percent_without_an_atr_and_says_so(self):
        """
        Silently trading a different model than the version declares
        would corrupt the comparison the versioning exists to enable.
        """

        result = stops.compute(1.1, "BUY", model=stops.ATR, atr=None)

        assert result["model_used"] == stops.PERCENT

    def test_respects_the_brokers_minimum_distance(self):
        """
        A stop inside trade_stops_level is rejected by MT5, which on
        the old path surfaced as an unexplained execution failure.
        """

        symbol_profile = spec("XAUUSDm")     # stops_level 35, point 0.001

        # An ATR far tighter than the broker allows.
        result = stops.compute(
            2400.0, "BUY", model=stops.ATR, atr=0.001,
            symbol_profile=symbol_profile,
        )

        assert result["clamped"] is True
        assert result["stop_distance"] >= result["broker_min_distance"]

    def test_prices_are_rounded_to_the_symbols_digits(self):
        result = stops.compute(
            64_000.0, "BUY", sl_percent=0.002,
            symbol_profile=spec("BTCUSDm"),      # 2 digits
        )

        assert result["stop_loss"] == round(result["stop_loss"], 2)

    def test_model_is_read_from_the_frozen_strategy_params(self):
        assert stops.model_for_strategy({"stop_model": "atr"}) == stops.ATR
        assert stops.model_for_strategy({}) == stops.PERCENT
        assert stops.model_for_strategy(
            {"stop_model": "nonsense"}
        ) == stops.PERCENT


# =====================================================================
# Close path
# =====================================================================

class TestClosePath:

    def _open(self, fake_world, symbol="EURUSDm", volume=0.10):
        result = fake_mt5.order_send({
            "symbol": symbol,
            "type": fake_mt5.ORDER_TYPE_BUY,
            "type_filling": filling_for(symbol),
            "volume": volume,
            "price": fake_mt5.spec_for(symbol)["price"],
            "sl": 0.0, "tp": 0.0,
            "magic": MAGIC,
            "comment": "AIQ-open",
        })

        deals = fake_mt5.history_deals_get(ticket=result.deal)

        return deals[0].position_id

    def test_closes_a_position(self, temp_db, fake_world):
        from backend.market.execution import close_position

        ticket = self._open(fake_world)

        result = close_position(ticket, reason="MANUAL")

        assert result["status"] == "CLOSED"
        assert fake_mt5.positions_get(ticket=ticket) == ()

    def test_close_is_idempotent(self, temp_db, fake_world):
        """A retry after a crash must not be an error - or a reversal."""

        from backend.market.execution import close_position

        ticket = self._open(fake_world)

        assert close_position(ticket)["status"] == "CLOSED"

        again = close_position(ticket)

        assert again["status"] == "ALREADY_CLOSED"

        # Critically: no NEW position was opened by the second attempt.
        assert len(fake_mt5.positions_get()) == 0

    def test_close_carries_a_comment_tag(self, temp_db, fake_world):
        """
        The tag is what lets the reconciler answer "did this close
        reach the broker?" after a crash.
        """

        from backend.market.execution import close_position, comment_for

        ticket = self._open(fake_world)

        result = close_position(ticket, client_order_id="abc123def456")

        closing = [
            d for d in fake_mt5.world.deals
            if d.entry == fake_mt5.DEAL_ENTRY_OUT
        ]

        assert comment_for("abc123def456") in closing[-1].comment
        assert result["status"] == "CLOSED"

    def test_partial_fill_is_reissued_until_complete(self, temp_db,
                                                     fake_world):
        """
        MT5 returns DONE_PARTIAL when only some volume filled. Treating
        that as done would leave a position open that the database
        believes is closed.
        """

        from backend.market.execution import close_position

        ticket = self._open(fake_world, volume=0.10)

        # Fill 0.03 lots at a time.
        fake_world.partial_close_volume = 0.03

        result = close_position(ticket)

        assert result["status"] == "CLOSED"
        assert result["partial_fills"] >= 1
        assert fake_mt5.positions_get(ticket=ticket) == ()

    def test_rejection_is_reported_not_swallowed(self, temp_db, fake_world):
        from backend.market.execution import close_position

        ticket = self._open(fake_world)

        fake_world.reject_orders = True

        result = close_position(ticket)

        assert result["status"] == "REJECTED"
        assert fake_mt5.positions_get(ticket=ticket)     # still open

    def test_ambiguous_close_is_left_for_reconciliation(self, temp_db,
                                                        fake_world):
        from backend.market.execution import close_position

        ticket = self._open(fake_world)

        fake_world.order_send_returns_none = True

        result = close_position(ticket)

        assert result["status"] == "FAILED"
        assert result["ambiguous"] is True


class TestModifySltp:

    def test_moves_the_stop(self, temp_db, fake_world):
        from backend.market.execution import modify_sltp

        ticket = TestClosePath()._open(fake_world)

        result = modify_sltp(ticket, stop_loss=1.05, take_profit=1.15)

        assert result["status"] == "MODIFIED"

        position = fake_mt5.positions_get(ticket=ticket)[0]

        assert position.sl == pytest.approx(1.05)
        assert position.tp == pytest.approx(1.15)

    def test_missing_position_is_reported(self, temp_db, fake_world):
        from backend.market.execution import modify_sltp

        assert modify_sltp(999_999, stop_loss=1.0)["status"] == "FAILED"


# =====================================================================
# Reversal  (Phase 0 weakness #5)
# =====================================================================

class TestReversal:

    def _open_tracked(self, fake_world, symbol="EURUSDm", direction="BUY"):
        from backend.database import repository as repo

        result = fake_mt5.order_send({
            "symbol": symbol,
            "type": (
                fake_mt5.ORDER_TYPE_BUY if direction == "BUY"
                else fake_mt5.ORDER_TYPE_SELL
            ),
            "type_filling": filling_for(symbol),
            "volume": 0.01,
            "price": fake_mt5.spec_for(symbol)["price"],
            "sl": 0.0, "tp": 0.0,
            "magic": MAGIC, "comment": "AIQ-open",
        })

        ticket = fake_mt5.history_deals_get(ticket=result.deal)[0].position_id

        trade_id = repo.insert_trade_intent({
            "client_order_id": f"rev-{ticket}",
            "symbol": symbol, "direction": direction, "volume": 0.01,
        })

        repo.update_trade(
            trade_id, execution_status="EXECUTED",
            position_ticket=ticket, entry_price=1.1,
        )

        return trade_id, ticket

    def test_flip_closes_instead_of_hedging(self, temp_db, fake_world):
        """
        Acceptance: BUY then SELL yields ONE net position and a
        REVERSAL exit row.
        """

        from backend.core import exits
        from backend.database import repository as repo

        trade_id, ticket = self._open_tracked(fake_world, direction="BUY")

        closed, detail = exits.handle_reversal(
            "EURUSDm", "SELL",
            {"ai_score": 85},
            {"min_ai_score": 70},
        )

        assert closed is True

        # No opposite position was opened.
        assert len(fake_mt5.positions_get()) == 0

        trade = repo.get_trade(trade_id)

        assert trade["exit_reason"] == "REVERSAL"

    def test_low_conviction_flip_holds_the_position(self, temp_db,
                                                    fake_world):
        """
        A flip is only worth acting on if it would have been worth
        entering on. Closing a good position on a weak signal is the
        worst of both.
        """

        from backend.core import exits

        self._open_tracked(fake_world, direction="BUY")

        closed, detail = exits.handle_reversal(
            "EURUSDm", "SELL", {"ai_score": 40}, {"min_ai_score": 70}
        )

        assert closed is False
        assert "below the 70 floor" in detail["reason"]
        assert len(fake_mt5.positions_get()) == 1

    def test_same_direction_is_not_a_reversal(self, temp_db, fake_world):
        from backend.core import exits

        self._open_tracked(fake_world, direction="BUY")

        closed, detail = exits.handle_reversal(
            "EURUSDm", "BUY", {"ai_score": 90}, {"min_ai_score": 70}
        )

        assert closed is False
        assert detail["reason"] == "no opposing position"

    def test_other_symbols_are_untouched(self, temp_db, fake_world):
        from backend.core import exits

        self._open_tracked(fake_world, symbol="EURUSDm", direction="BUY")
        self._open_tracked(fake_world, symbol="GBPUSDm", direction="BUY")

        exits.handle_reversal(
            "EURUSDm", "SELL", {"ai_score": 90}, {"min_ai_score": 70}
        )

        remaining = fake_mt5.positions_get()

        assert len(remaining) == 1
        assert remaining[0].symbol == "GBPUSDm"

    def test_the_reversal_decision_is_still_recorded(self, temp_db,
                                                     fake_world, monkeypatch):
        """
        A cycle that reverses must still leave a `decisions` row.

        Returning early without inserting it loses the decision from the
        audit trail entirely: no strategy_version_id (invariant 9), no
        decision_bars for replay, and Phase 6's calibration would
        under-count exactly the most interesting decisions - the flips.
        """

        import asyncio

        from backend.core import engine
        from backend.core.runtime import bot_state
        from backend.database import repository as repo
        from backend.market import broker_profile
        from backend.risk import profiles

        profiles.reset_cache()

        captured = broker_profile.capture(["EURUSDm"])

        monkeypatch.setitem(bot_state, "account_id", captured["account_id"])
        monkeypatch.setitem(bot_state, "server_utc_offset_min", 0)
        monkeypatch.setitem(bot_state, "mt5_connected", True)

        self._open_tracked(fake_world, direction="BUY")

        from backend.ai import versioning

        # Stamped exactly as backend.ai.brain stamps it, so the
        # provenance assertion below tests the engine rather than the
        # stub.
        monkeypatch.setattr(engine, "get_ai_decision", lambda *a, **k: {
            "symbol": "EURUSDm",
            "status": "ok",
            "ai_signal": "SELL",
            "ai_score": 90,
            "reasoning": "flip",
            "timeframe": "H1",
            "prompt_hash": versioning.compute_prompt_hash(),
            "strategy_version_id": versioning.ensure_registered(),
        })

        asyncio.run(engine.process_symbol("EURUSDm", "cycle-1"))

        decisions = repo.get_decisions(limit=10)

        assert len(decisions) == 1, "the reversal decision was never stored"

        row = decisions[0]

        assert row["final_decision"] == "HOLD"
        assert "reversal" in (row["override_reason"] or "").lower()
        assert row["strategy_version_id"] is not None

    def test_reversal_is_logged(self, temp_db, fake_world):
        from backend.core import exits
        from backend.database import repository as repo

        self._open_tracked(fake_world, direction="BUY")

        exits.handle_reversal(
            "EURUSDm", "SELL", {"ai_score": 90}, {"min_ai_score": 70}
        )

        messages = " ".join(e["message"] for e in repo.get_events(limit=20))

        assert "AI flipped" in messages
        assert "closing on reversal" in messages


# =====================================================================
# Flatten
# =====================================================================

class TestFlatten:

    def test_fires_inside_the_weekend_window(self):
        """Acceptance: flatten fires at the boundary, clock-mocked."""

        from backend.core import exits

        profile = {"flatten_before_weekend_min": 30}

        # Friday 23:30 server time, exactly 29 min before 23:59.
        friday = datetime(2026, 3, 13, 23, 30, tzinfo=timezone.utc)

        assert friday.weekday() == 4

        due, detail = exits.weekend_flatten_due(profile, 0, friday)

        assert due is True
        assert detail["minutes_before_close"] == 30

    def test_does_not_fire_before_the_window(self):
        from backend.core import exits

        profile = {"flatten_before_weekend_min": 30}

        friday = datetime(2026, 3, 13, 23, 28, tzinfo=timezone.utc)

        due, _ = exits.weekend_flatten_due(profile, 0, friday)

        assert due is False

    def test_boundary_minute_exactly(self):
        from backend.core import exits

        profile = {"flatten_before_weekend_min": 30}

        # 23:59 - 30 min = 23:29 exactly.
        at = datetime(2026, 3, 13, 23, 29, tzinfo=timezone.utc)

        assert exits.weekend_flatten_due(profile, 0, at)[0] is True

        just_before = at - timedelta(seconds=1)

        assert exits.weekend_flatten_due(profile, 0, just_before)[0] is False

    def test_does_not_fire_on_other_days(self):
        from backend.core import exits

        profile = {"flatten_before_weekend_min": 30}

        thursday = datetime(2026, 3, 12, 23, 45, tzinfo=timezone.utc)

        assert exits.weekend_flatten_due(profile, 0, thursday)[0] is False

    def test_uses_the_broker_clock_not_utc(self):
        """
        The broker's Friday close ends the week. On a UTC+3 broker a
        UTC Friday would be the wrong moment by three hours - three
        hours of unhedged weekend gap risk.
        """

        from backend.core import exits

        profile = {"flatten_before_weekend_min": 30}

        # 23:45 Friday UTC is inside the 23:29-23:59 flatten window. The
        # same wall-clock moment on a UTC+3 broker is 02:45 SATURDAY -
        # the window closed hours ago on the broker's own clock.
        utc_moment = datetime(2026, 3, 13, 23, 45, tzinfo=timezone.utc)

        server_moment = utc_moment + timedelta(minutes=180)

        assert exits.weekend_flatten_due(profile, 180, server_moment)[0] is False

        # The same wall-clock moment read as UTC would have fired.
        assert exits.weekend_flatten_due(profile, 0, utc_moment)[0] is True

    def test_disabled_when_not_configured(self):
        from backend.core import exits

        assert exits.weekend_flatten_due({}, 0)[0] is False

    def test_flatten_all_closes_everything(self, temp_db, fake_world):
        from backend.core import exits
        from backend.database import repository as repo

        reversal = TestReversal()

        reversal._open_tracked(fake_world, symbol="EURUSDm")
        reversal._open_tracked(fake_world, symbol="GBPUSDm")

        result = exits.flatten_all(exits.FLATTEN)

        assert result["closed"] == 2
        assert result["failed"] == 0
        assert len(fake_mt5.positions_get()) == 0

        reasons = {t["exit_reason"] for t in repo.get_trades(limit=10)}

        assert "FLATTEN" in reasons

    def test_flatten_reports_failures(self, temp_db, fake_world):
        from backend.core import exits

        TestReversal()._open_tracked(fake_world)

        fake_world.reject_orders = True

        result = exits.flatten_all(exits.FLATTEN)

        assert result["failed"] == 1
        assert result["closed"] == 0

    def test_nothing_open_is_not_an_error(self, temp_db, fake_world):
        from backend.core import exits

        assert exits.flatten_all(exits.FLATTEN)["closed"] == 0


# =====================================================================
# Exit lineage
# =====================================================================

class TestExitReason:

    def test_stop_out_is_labelled_sl(self):
        from backend.core import reconciler

        trade = {
            "direction": "BUY", "entry_price": 1.10000,
            "stop_loss": 1.09780, "take_profit": 1.10440,
            "stop_distance_price": 0.0022,
        }

        assert reconciler._infer_exit_reason(trade, 1.09781) == "SL"

    def test_target_hit_is_labelled_tp(self):
        from backend.core import reconciler

        trade = {
            "direction": "BUY", "entry_price": 1.10000,
            "stop_loss": 1.09780, "take_profit": 1.10440,
            "stop_distance_price": 0.0022,
        }

        assert reconciler._infer_exit_reason(trade, 1.10439) == "TP"

    def test_a_close_in_the_middle_is_manual_not_a_guess(self):
        """
        Mislabelling a terminal close as SL would poison the
        exit-reason breakdown Phase 6 reads.
        """

        from backend.core import reconciler

        trade = {
            "direction": "BUY", "entry_price": 1.10000,
            "stop_loss": 1.09780, "take_profit": 1.10440,
            "stop_distance_price": 0.0022,
        }

        assert reconciler._infer_exit_reason(trade, 1.10100) == "MANUAL"

    def test_an_engine_exit_keeps_its_own_reason(self):
        from backend.core import reconciler

        trade = {
            "direction": "BUY", "entry_price": 1.10000,
            "stop_loss": 1.09780, "take_profit": 1.10440,
            "stop_distance_price": 0.0022,
            "exit_reason": "REVERSAL",
        }

        # Even though this price is near the stop.
        assert reconciler._infer_exit_reason(trade, 1.09781) == "REVERSAL"

    def test_sells_are_handled(self):
        from backend.core import reconciler

        trade = {
            "direction": "SELL", "entry_price": 1.10000,
            "stop_loss": 1.10220, "take_profit": 1.09560,
            "stop_distance_price": 0.0022,
        }

        assert reconciler._infer_exit_reason(trade, 1.10219) == "SL"
        assert reconciler._infer_exit_reason(trade, 1.09561) == "TP"


# =====================================================================
# End to end through the engine
# =====================================================================

class TestEngineSizing:

    def _setup(self, fake_world, monkeypatch):
        from backend.core.runtime import bot_state
        from backend.market import broker_profile
        from backend.risk import profiles

        profiles.reset_cache()
        broker_profile.capture(["EURUSDm", "BTCUSDm", "XAUUSDm"])

        monkeypatch.setitem(bot_state, "account_id", fake_world.login)
        monkeypatch.setitem(bot_state, "server_utc_offset_min", 0)

    def test_volume_follows_risk_not_a_fixed_lot(self, temp_db, fake_world,
                                                 monkeypatch):
        from backend.core import engine

        self._setup(fake_world, monkeypatch)

        account = {"equity": 100_000.0, "balance": 100_000.0,
                   "margin_free": 100_000.0, "floating_pnl": 0.0}

        planned = engine.plan_risk(
            "EURUSDm", "BUY",
            {"ask": 1.10000, "bid": 1.09990, "spread": 0.00010,
             "atr_h1": 0.0015},
            account=account,
        )

        # 0.25% of 100k = $250 target, not the old fixed 0.01 lot.
        assert planned["volume"] > 0.01
        assert planned["risk_amount"] == pytest.approx(250.0, rel=0.02)

    def test_sizing_refusal_does_not_fall_back_to_a_fixed_lot(
        self, temp_db, fake_world, monkeypatch
    ):
        """
        Falling back would be the exact breach the sizing exists to
        prevent.
        """

        from backend.core import engine

        self._setup(fake_world, monkeypatch)

        # A tiny account: the minimum lot risks more than 0.25%.
        account = {"equity": 100.0, "balance": 100.0,
                   "margin_free": 100.0, "floating_pnl": 0.0}

        planned = engine.plan_risk(
            "XAUUSDm", "BUY",
            {"ask": 2400.0, "bid": 2399.9, "spread": 0.1, "atr_h1": 5.0},
            account=account,
        )

        assert planned["volume"] is None
        assert planned["sizing_refused"]

    def test_stop_model_is_recorded_on_the_plan(self, temp_db, fake_world,
                                                monkeypatch):
        from backend.core import engine

        self._setup(fake_world, monkeypatch)

        planned = engine.plan_risk(
            "EURUSDm", "BUY",
            {"ask": 1.1, "bid": 1.1, "spread": 0.0001, "atr_h1": 0.0015},
            account={"equity": 100_000.0, "balance": 100_000.0,
                     "margin_free": 100_000.0},
        )

        assert planned["stop_model"] in (stops.PERCENT, stops.ATR)
        assert planned["planned_stop_loss"] is not None
        assert planned["planned_take_profit"] is not None


# =====================================================================
# The sized volume and versioned stop reach the actual order
# =====================================================================

class TestOrderCarriesThePlan:
    """
    plan_risk can compute a perfect size, but if execute_trade ignores
    it the order still goes out at the fixed lot. These pin the wiring.
    """

    def test_supplied_volume_and_stops_are_used_verbatim(self, temp_db,
                                                         fake_world):
        from backend.market.execution import execute_trade

        result = execute_trade(
            "EURUSDm", "BUY", volume=0.37,
            stop_loss=1.09500, take_profit=1.10800,
        )

        assert result["status"] == "EXECUTED"

        position = fake_mt5.positions_get()[0]

        assert position.volume == pytest.approx(0.37)
        assert position.sl == pytest.approx(1.09500, abs=1e-5)
        assert position.tp == pytest.approx(1.10800, abs=1e-5)

    def test_falls_back_to_the_fixed_lot_and_percent_stop_when_unset(
        self, temp_db, fake_world
    ):
        """A pre-sizing call still behaves exactly as before."""

        from backend import config
        from backend.market.execution import execute_trade

        result = execute_trade("EURUSDm", "BUY")

        assert result["status"] == "EXECUTED"

        position = fake_mt5.positions_get()[0]

        assert position.volume == pytest.approx(config.LOT_SIZE)
        assert position.sl < position.price_open < position.tp

    def test_a_sizing_refusal_does_not_reach_execution(self, temp_db,
                                                       fake_world, monkeypatch):
        """
        apply_risk_checks must turn a sizing refusal into HOLD, so
        execute_trade is never called with volume=None and never falls
        back to a fixed lot.
        """

        from backend.core import engine

        final, reason = engine.apply_risk_checks(
            "XAUUSDm",
            {"status": "ok", "ai_signal": "BUY", "ai_score": 90},
            market_data={},
            risk={"sizing_refused": "minimum volume risks more than the budget",
                  "volume": None, "risk_amount": 5.0},
        )

        assert final == "HOLD"
        assert "Sizing refused" in reason
