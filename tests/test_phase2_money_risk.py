"""
Phase 2 acceptance tests: money risk and the daily ledger.

The headline criteria:
  * every trades row has risk_amount > 0
  * risk_amount is correct on a 5-digit FX, a 2-digit crypto and a
    3-digit metal symbol, using the STORED broker specification
  * daily_ledger.realized_pnl equals the sum of MT5 deal
    profit+commission+swap for that day, to the cent
"""

from datetime import datetime, timedelta, timezone

import pytest

import tests.fake_mt5 as fake_mt5


MAGIC = 100001


# =====================================================================
# risk_amount arithmetic  (Phase 0 §2.4)
# =====================================================================

class TestRiskAmount:
    """
    Each case is checked against the hand calculation for that
    contract, not against the implementation - otherwise the test only
    proves the code agrees with itself.
    """

    def test_five_digit_fx(self):
        from backend.risk import money

        spec = fake_mt5.SYMBOL_SPECS["EURUSDm"]

        # 0.01 lot = 1,000 units. A 22-pip stop is 0.0022 of price.
        risk = money.risk_amount(0.0022, 0.01, spec)

        assert risk == pytest.approx(2.20, abs=0.001)

        # Independent check: 0.0022 x 1,000 units.
        assert risk == pytest.approx(0.0022 * 1_000, abs=0.001)

    def test_two_digit_crypto(self):
        from backend.risk import money

        spec = fake_mt5.SYMBOL_SPECS["BTCUSDm"]

        # 0.01 BTC with a $128 stop.
        risk = money.risk_amount(128.0, 0.01, spec)

        assert risk == pytest.approx(1.28, abs=0.001)
        assert risk == pytest.approx(128.0 * 0.01, abs=0.001)

    def test_three_digit_metal(self):
        from backend.risk import money

        spec = fake_mt5.SYMBOL_SPECS["XAUUSDm"]

        # 100 oz per lot, so 0.01 lot = 1 oz. A $4.80 stop.
        risk = money.risk_amount(4.80, 0.01, spec)

        assert risk == pytest.approx(4.80, abs=0.001)
        assert risk == pytest.approx(4.80 * 100 * 0.01, abs=0.001)

    def test_scales_linearly_with_volume(self):
        from backend.risk import money

        spec = fake_mt5.SYMBOL_SPECS["EURUSDm"]

        one = money.risk_amount(0.0022, 0.01, spec)
        ten = money.risk_amount(0.0022, 0.10, spec)

        assert ten == pytest.approx(one * 10, abs=0.001)

    def test_missing_specification_returns_none_not_zero(self):
        """
        A silently wrong risk figure is worse than an absent one: the
        Phase 3 engine can refuse on None, but would approve a bogus
        number.
        """

        from backend.risk import money

        assert money.risk_amount(0.0022, 0.01, None) is None
        assert money.risk_amount(0.0022, 0.01, {}) is None
        assert money.risk_amount(
            0.0022, 0.01, {"trade_tick_size": 0, "trade_tick_value": 1}
        ) is None
        assert money.risk_amount(None, 0.01, {"trade_tick_size": 1}) is None

    def test_uses_the_stored_broker_profile(self, temp_db, fake_world):
        """Acceptance: computed from stored broker_profiles values."""

        from backend.database import repo_meta
        from backend.market import broker_profile
        from backend.risk import money

        broker_profile.capture(["EURUSDm", "BTCUSDm", "XAUUSDm"])

        account_id = fake_world.login

        cases = [
            ("EURUSDm", 0.0022, 2.20),
            ("BTCUSDm", 128.0, 1.28),
            ("XAUUSDm", 4.80, 4.80),
        ]

        for symbol, distance, expected in cases:
            stored = repo_meta.get_symbol_profile(account_id, symbol)

            assert stored is not None, symbol

            assert money.risk_amount(distance, 0.01, stored) == pytest.approx(
                expected, abs=0.001
            ), symbol


class TestVolumeForRisk:
    """The inverse, which Phase 5 sizing builds on."""

    @pytest.mark.parametrize("symbol,distance,target", [
        ("EURUSDm", 0.0022, 22.0),
        ("BTCUSDm", 128.0, 12.8),
        ("XAUUSDm", 4.80, 48.0),
    ])
    def test_round_trips_with_risk_amount(self, symbol, distance, target):
        from backend.risk import money

        spec = fake_mt5.SYMBOL_SPECS[symbol]

        volume = money.volume_for_risk(target, distance, spec)

        assert money.risk_amount(distance, volume, spec) == pytest.approx(
            target, rel=1e-6
        )


# =====================================================================
# Stop distance and spread  (Phase 0 §2.5)
# =====================================================================

class TestStopDistance:

    def test_nominal_is_what_a_stop_out_costs(self):
        from backend.risk import money

        # BUY filled at ask 1.10010, stop at 1.09790.
        assert money.stop_distance(1.10010, 1.09790) == pytest.approx(0.0022)

    def test_effective_distance_is_net_of_spread(self):
        """
        A long is marked at bid, so the market only has to travel
        (nominal - spread) to trigger the stop. That governs the
        PROBABILITY of being stopped, not the size of the loss.
        """

        from backend.risk import money

        effective = money.effective_stop_distance(
            1.10010, 1.09790, "BUY", spread=0.00010
        )

        assert effective == pytest.approx(0.0021)

    def test_effective_distance_is_symmetric_for_shorts(self):
        from backend.risk import money

        long_side = money.effective_stop_distance(
            1.10010, 1.09790, "BUY", spread=0.00010
        )
        short_side = money.effective_stop_distance(
            1.10000, 1.10220, "SELL", spread=0.00010
        )

        assert long_side == pytest.approx(short_side)

    def test_effective_never_negative(self):
        """A spread wider than the stop reports 0, not a negative."""

        from backend.risk import money

        assert money.effective_stop_distance(
            1.1000, 1.0999, "BUY", spread=0.0050
        ) == 0.0

    def test_unknown_spread_falls_back_to_nominal(self):
        from backend.risk import money

        assert money.effective_stop_distance(
            1.10010, 1.09790, "BUY", spread=None
        ) == pytest.approx(0.0022)

    def test_describe_bundles_everything(self):
        from backend.risk import money

        described = money.describe(
            entry_price=1.10010,
            stop_loss=1.09790,
            direction="BUY",
            volume=0.01,
            spread=0.00010,
            symbol_profile=fake_mt5.SYMBOL_SPECS["EURUSDm"],
        )

        assert described["risk_amount"] == pytest.approx(2.20, abs=0.001)
        assert described["stop_distance_price"] == pytest.approx(0.0022)
        assert described["stop_distance_effective"] == pytest.approx(0.0021)
        assert described["spread_at_entry"] == 0.00010


# =====================================================================
# Money-based R
# =====================================================================

class TestRMultiple:

    def test_full_stop_out_is_minus_one_r(self):
        from backend.risk import money

        assert money.r_multiple(-2.20, 2.20) == -1.0

    def test_two_to_one_winner_is_plus_two_r(self):
        from backend.risk import money

        assert money.r_multiple(4.40, 2.20) == 2.0

    def test_costs_are_included_because_pnl_is_net(self):
        """
        pnl already nets commission and swap, so money-R answers "what
        did I actually keep", unlike the price-based version.
        """

        from backend.risk import money

        assert money.r_multiple(4.30, 2.20) == pytest.approx(1.9545, abs=1e-4)

    def test_none_without_a_risk_figure(self):
        from backend.risk import money

        assert money.r_multiple(4.40, None) is None
        assert money.r_multiple(4.40, 0) is None
        assert money.r_multiple(None, 2.20) is None

    def test_reconciler_prefers_money_r_and_falls_back(self):
        from backend.core import reconciler

        priced = {
            "entry_price": 1.10010,
            "stop_loss": 1.09790,
            "direction": "BUY",
            "risk_amount": 2.20,
        }

        # Money path.
        assert reconciler._r_multiple(priced, 1.09790, -2.20) == -1.0

        # Pre-Phase-2 row: no risk_amount -> price-based, still an R.
        legacy = dict(priced, risk_amount=None)

        assert reconciler._r_multiple(legacy, 1.09790, -2.20) == -1.0
        assert reconciler._r_multiple(legacy, 1.10450, 4.40) == pytest.approx(2.0)


# =====================================================================
# Trading-day arithmetic
# =====================================================================

class TestTradingDay:

    def test_uses_server_time_not_utc(self):
        """
        The firm computes its daily limit on SERVER time. A UTC day
        would misalign the window by the offset - up to three hours of
        trades in the wrong bucket.
        """

        from backend.database import repo_ledger

        # 22:30 UTC is already the next day on a UTC+3 broker.
        moment = datetime(2026, 3, 10, 22, 30, tzinfo=timezone.utc)

        assert repo_ledger.trading_day(0, moment) == "2026-03-10"
        assert repo_ledger.trading_day(180, moment) == "2026-03-11"

    def test_negative_offset(self):
        from backend.database import repo_ledger

        moment = datetime(2026, 3, 11, 1, 30, tzinfo=timezone.utc)

        assert repo_ledger.trading_day(-300, moment) == "2026-03-10"

    def test_deal_epoch_reads_as_the_server_day(self):
        """
        MT5 timestamps are server wall clock expressed as an epoch, so
        the server date is read directly. Applying the offset again
        here would double-count it.
        """

        from backend.database import repo_ledger

        epoch = datetime(
            2026, 3, 11, 1, 0, tzinfo=timezone.utc
        ).timestamp()

        assert repo_ledger.day_from_server_epoch(epoch) == "2026-03-11"
        assert repo_ledger.day_from_server_epoch(None) is None

    def test_day_bounds_shift_with_the_offset(self):
        from backend.database import repo_ledger

        start_utc, end_utc = repo_ledger.day_bounds_utc("2026-03-11", 180)

        assert start_utc == datetime(
            2026, 3, 10, 21, 0, tzinfo=timezone.utc
        )
        assert end_utc - start_utc == timedelta(days=1)


# =====================================================================
# The ledger itself
# =====================================================================

class TestDailyLedger:

    def test_accumulates_closed_trades(self, temp_db):
        from backend.database import repo_ledger

        for pnl in (10.0, -4.0, 6.5):
            repo_ledger.apply_closed_trade(
                123456, "2026-03-11", pnl=pnl, commission=-0.1, swap=0.0
            )

        summary = repo_ledger.day_summary(123456, "2026-03-11")

        assert summary["realized_pnl"] == pytest.approx(12.5)
        assert summary["trade_count"] == 3
        assert summary["win_count"] == 2
        assert summary["loss_count"] == 1

    def test_exposure_is_realised_plus_floating(self, temp_db):
        """
        The number a 3%-daily rule is actually measured against.

        With no row for the day yet, exposure is pure floating - the
        risk engine must still get a usable answer on the first trade
        of the day.
        """

        from backend.database import repo_ledger

        summary = repo_ledger.day_summary(999, "2026-03-11", floating_pnl=-30.0)

        assert summary["exists"] is False
        assert summary["exposure"] == -30.0

    def test_tracks_worst_floating_loss(self, temp_db):
        """
        A position that dipped hard and recovered leaves NO trace in
        the deal history, so if it is not sampled it is lost.
        """

        from backend.database import repo_ledger

        for floating in (-5.0, -42.0, -3.0, 10.0):
            repo_ledger.record_equity_marks(
                123456, "2026-03-11",
                equity=10_000 + floating, balance=10_000,
                floating_pnl=floating,
            )

        ledger = repo_ledger.get_ledger(123456, "2026-03-11")

        assert ledger["max_floating_loss"] == pytest.approx(42.0)
        assert ledger["min_equity"] == pytest.approx(9_958.0)
        assert ledger["max_equity"] == pytest.approx(10_010.0)

    def test_opening_marks_are_seeded_once(self, temp_db):
        from backend.database import repo_ledger

        repo_ledger.record_equity_marks(
            123456, "2026-03-11", equity=10_000, balance=10_000
        )
        repo_ledger.record_equity_marks(
            123456, "2026-03-11", equity=9_500, balance=10_000
        )

        ledger = repo_ledger.get_ledger(123456, "2026-03-11")

        assert ledger["start_equity"] == pytest.approx(10_000)
        assert ledger["eod_equity"] == pytest.approx(9_500)

    def test_days_are_isolated(self, temp_db):
        from backend.database import repo_ledger

        repo_ledger.apply_closed_trade(123456, "2026-03-11", pnl=10.0)
        repo_ledger.apply_closed_trade(123456, "2026-03-12", pnl=-3.0)

        assert repo_ledger.day_summary(
            123456, "2026-03-11"
        )["realized_pnl"] == pytest.approx(10.0)
        assert repo_ledger.day_summary(
            123456, "2026-03-12"
        )["realized_pnl"] == pytest.approx(-3.0)

    def test_accounts_are_isolated(self, temp_db):
        from backend.database import repo_ledger

        repo_ledger.apply_closed_trade(111, "2026-03-11", pnl=10.0)
        repo_ledger.apply_closed_trade(222, "2026-03-11", pnl=-3.0)

        assert repo_ledger.day_summary(
            111, "2026-03-11"
        )["realized_pnl"] == pytest.approx(10.0)
        assert repo_ledger.day_summary(
            222, "2026-03-11"
        )["realized_pnl"] == pytest.approx(-3.0)


# =====================================================================
# Ledger reconciliation against MT5  (acceptance: agrees to the cent)
# =====================================================================

class TestLedgerReconciliation:

    def _open_and_close(self, symbol, profit, commission=-0.10, swap=0.0):
        result = fake_mt5.order_send({
            "symbol": symbol,
            "type": fake_mt5.ORDER_TYPE_BUY,
            "type_filling": fake_mt5.ORDER_FILLING_FOK
            if symbol in ("EURUSDm", "XAUUSDm") else fake_mt5.ORDER_FILLING_IOC,
            "volume": 0.01,
            "price": fake_mt5.spec_for(symbol)["price"],
            "sl": 0.0, "tp": 0.0,
            "magic": MAGIC,
            "comment": "AIQ-test",
        })

        deals = fake_mt5.history_deals_get(ticket=result.deal)

        fake_mt5.close_position(
            deals[0].position_id,
            profit=profit, commission=commission, swap=swap,
        )

    def test_matches_mt5_to_the_cent(self, temp_db, fake_world, monkeypatch):
        """
        Acceptance: daily_ledger.realized_pnl equals the sum of MT5
        deal profit+commission+swap for that day, to the cent.
        """

        from backend.core import reconciler
        from backend.core.runtime import bot_state
        from backend.database import repo_ledger

        monkeypatch.setitem(bot_state, "account_id", fake_world.login)
        monkeypatch.setitem(bot_state, "server_utc_offset_min", 0)

        expected = 0.0

        for profit, commission, swap in (
            (12.34, -0.10, 0.0),
            (-5.00, -0.10, -0.25),
            (3.10, -0.10, 0.0),
            (-1.20, -0.10, 0.0),
            (8.00, -0.10, 0.15),
        ):
            self._open_and_close("EURUSDm", profit, commission, swap)
            expected += profit + commission + swap

        drift = reconciler.reconcile_daily_ledger()

        day = repo_ledger.trading_day(0)

        ledger = repo_ledger.get_ledger(fake_world.login, day)

        assert ledger["realized_pnl"] == pytest.approx(expected, abs=0.01)
        assert ledger["trade_count"] == 5
        assert ledger["win_count"] == 3
        assert ledger["loss_count"] == 2
        assert ledger["reconciled_at"] is not None
        assert drift == pytest.approx(abs(expected), abs=0.01)

    def test_ignores_trades_that_are_not_ours(self, temp_db, fake_world,
                                              monkeypatch):
        """A manual trade in the same terminal must not enter the ledger."""

        from backend.core import reconciler
        from backend.core.runtime import bot_state
        from backend.database import repo_ledger

        monkeypatch.setitem(bot_state, "account_id", fake_world.login)
        monkeypatch.setitem(bot_state, "server_utc_offset_min", 0)

        self._open_and_close("EURUSDm", 10.0, commission=0.0)

        # Someone else's position, different magic.
        foreign = fake_mt5.order_send({
            "symbol": "EURUSDm", "type": fake_mt5.ORDER_TYPE_BUY,
            "type_filling": fake_mt5.ORDER_FILLING_FOK,
            "volume": 0.01, "price": 1.1, "sl": 0.0, "tp": 0.0,
            "magic": 999999, "comment": "manual",
        })
        deals = fake_mt5.history_deals_get(ticket=foreign.deal)
        fake_mt5.close_position(
            deals[0].position_id, profit=500.0, commission=0.0
        )

        reconciler.reconcile_daily_ledger()

        day = repo_ledger.trading_day(0)
        ledger = repo_ledger.get_ledger(fake_world.login, day)

        assert ledger["realized_pnl"] == pytest.approx(10.0, abs=0.01)
        assert ledger["trade_count"] == 1

    def test_drift_is_corrected_to_mt5_and_logged(self, temp_db, fake_world,
                                                  monkeypatch):
        from backend.core import reconciler
        from backend.core.runtime import bot_state
        from backend.database import repo_ledger
        from backend.database import repository as repo

        monkeypatch.setitem(bot_state, "account_id", fake_world.login)
        monkeypatch.setitem(bot_state, "server_utc_offset_min", 0)

        day = repo_ledger.trading_day(0)

        # A local figure MT5 does not agree with.
        repo_ledger.apply_closed_trade(
            fake_world.login, day, pnl=999.0, commission=0.0
        )

        self._open_and_close("EURUSDm", 10.0, commission=0.0)

        drift = reconciler.reconcile_daily_ledger()

        assert drift == pytest.approx(989.0, abs=0.01)

        ledger = repo_ledger.get_ledger(fake_world.login, day)

        assert ledger["realized_pnl"] == pytest.approx(10.0, abs=0.01)

        messages = " ".join(e["message"] for e in repo.get_events(limit=20))

        assert "Daily ledger drift" in messages

    def test_no_account_is_a_safe_noop(self, temp_db, fake_world, monkeypatch):
        from backend.core import reconciler
        from backend.core.runtime import bot_state

        monkeypatch.setitem(bot_state, "account_id", None)

        assert reconciler.reconcile_daily_ledger() == 0.0

    def test_runs_as_the_fourth_pass(self, temp_db, fake_world, monkeypatch):
        from backend.core import reconciler
        from backend.core.runtime import bot_state

        monkeypatch.setitem(bot_state, "account_id", fake_world.login)
        monkeypatch.setitem(bot_state, "server_utc_offset_min", 0)

        summary = reconciler.run_full_reconciliation()

        assert "ledger_drift" in summary
        assert summary["errors"] == []


# =====================================================================
# End to end through the engine
# =====================================================================

class TestEngineRecordsRisk:

    def test_intent_carries_risk_before_the_order_is_sent(
        self, temp_db, fake_world, monkeypatch
    ):
        """
        Acceptance: every trades row has risk_amount > 0.

        The figure must exist on the PENDING row - i.e. before
        order_send - not be back-filled afterwards.
        """

        from backend.core import engine
        from backend.core.runtime import bot_state
        from backend.market import broker_profile

        broker_profile.capture(["EURUSDm"])

        monkeypatch.setitem(bot_state, "account_id", fake_world.login)

        planned = engine.plan_risk(
            "EURUSDm", "BUY",
            {"ask": 1.10010, "bid": 1.10000, "spread": 0.00010},
            volume=0.01,
        )

        assert planned["risk_amount"] > 0
        assert planned["risk_amount"] == pytest.approx(2.20, abs=0.02)
        assert planned["stop_distance_price"] > planned["stop_distance_effective"]

    @pytest.mark.parametrize("symbol,expected", [
        ("EURUSDm", 2.20),
        ("BTCUSDm", 1.28),
        ("XAUUSDm", 4.80),
    ])
    def test_risk_is_right_across_instrument_types(
        self, temp_db, fake_world, monkeypatch, symbol, expected
    ):
        from backend.core import engine
        from backend.core.runtime import bot_state
        from backend.market import broker_profile

        broker_profile.capture([symbol])
        monkeypatch.setitem(bot_state, "account_id", fake_world.login)

        price = fake_mt5.SYMBOL_SPECS[symbol]["price"]

        planned = engine.plan_risk(
            symbol, "BUY",
            {"ask": price, "bid": price, "spread": 0.0},
            volume=0.01,
        )

        # 0.20% stop on each instrument's own price.
        assert planned["risk_amount"] == pytest.approx(expected, rel=0.01)

    def test_missing_profile_reports_none_and_warns(
        self, temp_db, fake_world, monkeypatch
    ):
        from backend.core import engine
        from backend.core.runtime import bot_state

        monkeypatch.setitem(bot_state, "account_id", None)

        planned = engine.plan_risk(
            "EURUSDm", "BUY", {"ask": 1.1, "bid": 1.1, "spread": 0.0001}, 0.01
        )

        assert planned["risk_amount"] is None

    def test_ledger_marks_update_each_cycle(self, temp_db, fake_world,
                                            monkeypatch):
        from backend.core import engine
        from backend.core.runtime import bot_state
        from backend.database import repo_ledger

        monkeypatch.setitem(bot_state, "account_id", fake_world.login)
        monkeypatch.setitem(bot_state, "server_utc_offset_min", 0)

        engine.update_daily_ledger({
            "equity": 9_950.0, "balance": 10_000.0, "floating_pnl": -50.0,
        })

        summary = repo_ledger.day_summary(
            fake_world.login, repo_ledger.trading_day(0)
        )

        assert summary["max_floating_loss"] == pytest.approx(50.0)
        assert summary["min_equity"] == pytest.approx(9_950.0)


# =====================================================================
# Account attribution + API
# =====================================================================

class TestAccountAttribution:

    def test_rows_carry_the_account(self, temp_db, fake_world, monkeypatch):
        from backend.core.runtime import bot_state
        from backend.database import repository as repo

        monkeypatch.setitem(bot_state, "account_id", 424242)

        repo.insert_trade_intent({
            "client_order_id": "acct-1", "symbol": "EURUSDm",
            "direction": "BUY",
        })
        repo.insert_equity_snapshot({"equity": 10_000.0})
        repo.insert_market_state({"symbol": "EURUSDm"})
        repo.insert_event("hello", category="TEST")

        assert repo.get_trades(limit=1)[0]["account_id"] == 424242
        assert repo.get_equity_snapshots(limit=1)[0]["account_id"] == 424242
        assert repo.get_events(limit=1)[0]["account_id"] == 424242

    def test_migration_adds_the_new_columns(self, tmp_path):
        import sqlite3

        from backend.database.connection import apply_additive_migrations

        conn = sqlite3.connect(tmp_path / "old.db")
        conn.row_factory = sqlite3.Row

        conn.execute(
            "CREATE TABLE trades ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " client_order_id TEXT NOT NULL UNIQUE,"
            " created_at TEXT NOT NULL, symbol TEXT, direction TEXT,"
            " risk_amount REAL)"
        )
        conn.execute(
            "INSERT INTO trades (client_order_id, created_at, symbol,"
            " direction) VALUES ('old-1','2026-01-01','EURUSDm','BUY')"
        )

        added = apply_additive_migrations(conn)

        assert "trades.risk_amount" not in added        # already there
        assert "trades.account_id" in added
        assert "trades.stop_distance_price" in added

        rows = conn.execute("SELECT * FROM trades").fetchall()

        assert len(rows) == 1
        assert rows[0]["client_order_id"] == "old-1"

        conn.close()


class TestLedgerEndpoint:

    def test_reports_today_and_history(self, client, fake_world):
        payload = client.get("/api/ledger").json()

        assert payload["account_id"] == fake_world.login
        assert payload["trading_day"]
        assert "today" in payload
        assert isinstance(payload["history"], list)

    def test_today_carries_exposure(self, client, fake_world):
        from backend.database import repo_ledger

        day = repo_ledger.trading_day(
            client.get("/api/health").json()["server_utc_offset_min"]
        )

        repo_ledger.apply_closed_trade(fake_world.login, day, pnl=-25.0)

        today = client.get("/api/ledger").json()["today"]

        assert today["realized_pnl"] == pytest.approx(-25.0)
        assert "exposure" in today
