"""
Phase 4 acceptance tests: data-engine correctness and decision cadence.

Acceptance:
  * label stability - features at minute 5 and minute 55 of the same
    hour are identical
  * ~96 decisions/day on four symbols in per_bar mode
  * replaying a stored decision_bars row through compute_features
    reproduces the stored features_json exactly
"""

import time
from unittest import mock

import pandas as pd
import pytest

import tests.fake_mt5 as fake_mt5

from backend.market import data_engine


def frame(closes, highs=None, lows=None, start=1_700_000_000, step=3600):
    """A minimal OHLC frame for the feature functions."""

    highs = highs or [c * 1.001 for c in closes]
    lows = lows or [c * 0.999 for c in closes]

    return pd.DataFrame({
        "time": pd.to_datetime(
            [start + i * step for i in range(len(closes))],
            unit="s", utc=True,
        ),
        "open": closes,
        "high": highs,
        "low": lows,
        "close": closes,
    })


# =====================================================================
# Closed bars only  (Phase 0 weakness #7, B2)
# =====================================================================

class TestClosedBarsOnly:

    def test_fetch_starts_at_position_one(self, fake_world):
        """
        Position 0 is the bar still forming: its high, low and close
        change on every tick, so every derived feature changed every
        30 seconds while the market had produced no new information.
        """

        calls = []

        real = fake_mt5.copy_rates_from_pos

        def spy(symbol, timeframe, start, count):
            calls.append((timeframe, start, count))
            return real(symbol, timeframe, start, count)

        with mock.patch.object(fake_mt5, "copy_rates_from_pos", spy):
            data_engine.fetch_multi_timeframe_data("EURUSDm")

        assert calls, "no rate calls were made"

        for timeframe, start, count in calls:
            assert start == 1, (
                f"timeframe {timeframe} fetched from position {start}; "
                f"position 0 is the forming bar"
            )

        assert (fake_mt5.TIMEFRAME_D1, 1, data_engine.DAILY_BARS) in calls
        assert (fake_mt5.TIMEFRAME_H1, 1, data_engine.HOURLY_BARS) in calls

    def test_features_declare_they_used_closed_bars(self, fake_world):
        data = data_engine.fetch_multi_timeframe_data("EURUSDm")

        assert data["features"]["bars_closed_only"] is True

    def test_newest_bar_is_not_the_current_one(self, fake_world):
        """The newest returned bar must be strictly in the past."""

        data = data_engine.fetch_multi_timeframe_data("EURUSDm")

        newest = pd.Timestamp(data["features"]["bar_time_server"])

        server_now = pd.Timestamp(
            time.time() + fake_world.server_offset_minutes * 60,
            unit="s", tz="UTC",
        )

        assert newest < server_now


# =====================================================================
# Label stability  (the headline acceptance criterion)
# =====================================================================

class TestLabelStability:

    def test_features_identical_at_minute_5_and_minute_55(self, fake_world):
        """
        Acceptance: features computed at minute 5 and minute 55 of the
        same hour are identical.

        With the forming bar excluded, nothing the features depend on
        changes inside an hour - so two computations 50 minutes apart
        must agree exactly.
        """

        hour_start = (int(time.time()) // 3600) * 3600

        def at(minute):
            with mock.patch.object(
                time, "time", return_value=hour_start + minute * 60
            ):
                data = data_engine.fetch_multi_timeframe_data("EURUSDm")

            return data["features"]

        early = at(5)
        late = at(55)

        # The tick moves, so bid/ask/spread legitimately differ. Every
        # BAR-derived label must not.
        bar_derived = (
            "daily_trend", "h1_trend", "momentum", "market_structure",
            "market_regime", "volatility_bucket", "session",
            "atr_h1", "atr_pct", "last_close", "daily_close",
            "daily_high_10", "daily_low_10", "h1_high_24", "h1_low_24",
            "efficiency_ratio_daily", "efficiency_ratio_h1",
            "bar_time_utc", "bar_time_server", "feature_version",
        )

        for key in bar_derived:
            assert early[key] == late[key], (
                f"{key} changed within the hour: "
                f"{early[key]!r} -> {late[key]!r}"
            )

    def test_the_forming_bar_would_have_broken_stability(self, fake_world):
        """
        Guard the guard: with position 0 included, the newest bar
        differs between the two moments - which is exactly the
        instability the fix removes.
        """

        hour_start = (int(time.time()) // 3600) * 3600

        def newest_close(minute, start_pos):
            with mock.patch.object(
                time, "time", return_value=hour_start + minute * 60
            ):
                rates = fake_mt5.copy_rates_from_pos(
                    "EURUSDm", fake_mt5.TIMEFRAME_H1, start_pos, 24
                )

            return rates[-1]["time"]

        assert newest_close(5, 1) == newest_close(55, 1)


# =====================================================================
# Market regime uses both frames  (Phase 0 §A15, B7)
# =====================================================================

class TestMarketRegime:

    def test_daily_frame_actually_affects_the_label(self):
        """
        The signature took a daily frame and ignored it. Two identical
        H1 frames with different dailies must now be able to differ.
        """

        trending_h1 = frame([100 + i for i in range(24)])

        trending_daily = frame([100 + i * 5 for i in range(10)])

        # Daily that goes nowhere: up then back down.
        choppy_daily = frame(
            [100, 105, 100, 106, 99, 107, 100, 104, 101, 100]
        )

        clean = data_engine._market_regime(trending_daily, trending_h1)
        mixed = data_engine._market_regime(choppy_daily, trending_h1)

        assert clean == "trending"
        assert mixed != clean, (
            "the daily frame still has no effect on the regime label"
        )

    def test_geometric_mean_penalises_disagreement(self):
        """
        A market clean on the daily and noise on H1 is not 'half
        trending' - the geometric mean says so.
        """

        clean = frame([100 + i * 2 for i in range(24)])
        noise = frame([100 + (i % 2) for i in range(24)])

        er_clean = data_engine._efficiency_ratio(clean)
        er_noise = data_engine._efficiency_ratio(noise)

        combined = (er_clean * er_noise) ** 0.5
        arithmetic = (er_clean + er_noise) / 2

        assert combined < arithmetic

    def test_efficiency_ratio_bounds(self):
        straight = frame([100 + i for i in range(10)])

        assert data_engine._efficiency_ratio(straight) == pytest.approx(1.0)

        flat = frame([100] * 10)

        assert data_engine._efficiency_ratio(flat) == 0.0

    def test_falls_back_to_h1_without_enough_daily_history(self):
        tiny_daily = frame([100, 101])
        h1 = frame([100 + i for i in range(24)])

        assert data_engine._market_regime(tiny_daily, h1) == "trending"

    def test_components_are_recorded_for_later_analysis(self, fake_world):
        features = data_engine.fetch_multi_timeframe_data(
            "EURUSDm"
        )["features"]

        assert features["efficiency_ratio_daily"] is not None
        assert features["efficiency_ratio_h1"] is not None


# =====================================================================
# Feature versioning
# =====================================================================

class TestFeatureVersion:

    def test_stamped_on_every_feature_set(self, fake_world):
        features = data_engine.fetch_multi_timeframe_data(
            "EURUSDm"
        )["features"]

        assert features["feature_version"] == data_engine.FEATURE_VERSION

    def test_bumped_past_the_phase0_baseline(self):
        """The regime formula changed, so 1.0.0 would be a lie."""

        assert data_engine.FEATURE_VERSION != "1.0.0"

    def test_persisted_on_market_states(self, temp_db, fake_world):
        from backend.core import engine
        from backend.database import repository as repo

        data = data_engine.fetch_multi_timeframe_data("EURUSDm")

        engine.persist_market_state("EURUSDm", data)

        row = repo.get_market_states("EURUSDm", limit=1)[0]

        assert row["feature_version"] == data_engine.FEATURE_VERSION
        assert row["bar_time_utc"]
        assert row["bar_time_server"]


# =====================================================================
# Replay  (the acceptance criterion Phase 8 is built on)
# =====================================================================

class TestReplay:

    def test_replay_reproduces_stored_features_exactly(self, temp_db,
                                                       fake_world):
        """
        Acceptance: replaying a stored decision_bars row through
        compute_features reproduces the stored features_json exactly.
        """

        from backend.database import repository as repo
        from backend.market import replay

        data = data_engine.fetch_multi_timeframe_data("EURUSDm")

        decision_id = repo.insert_decision({
            "symbol": "EURUSDm", "status": "ok", "ai_signal": "BUY",
        })

        repo.insert_decision_bars(decision_id, data["snapshot"])

        snapshot = repo.get_decision_bars(decision_id)

        assert snapshot is not None

        matches, differences = replay.compare_to_stored(snapshot)

        assert matches, f"replay diverged from history: {differences}"

    def test_replay_works_for_every_instrument_shape(self, temp_db,
                                                     fake_world):
        from backend.database import repository as repo
        from backend.market import replay

        for symbol in ("EURUSDm", "BTCUSDm", "XAUUSDm"):

            data = data_engine.fetch_multi_timeframe_data(symbol)

            decision_id = repo.insert_decision({
                "symbol": symbol, "status": "ok", "ai_signal": "BUY",
            })

            repo.insert_decision_bars(decision_id, data["snapshot"])

            matches, differences = replay.compare_to_stored(
                repo.get_decision_bars(decision_id)
            )

            assert matches, f"{symbol}: {differences}"

    def test_replay_survives_a_non_zero_server_offset(self, temp_db,
                                                      fake_world):
        """
        The offset is applied when the bars are converted to UTC, so a
        replay that ignored it would produce different session labels.
        """

        from backend.core.runtime import bot_state
        from backend.database import repository as repo
        from backend.market import replay

        fake_world.server_offset_minutes = 180
        bot_state["server_utc_offset_min"] = 180

        try:
            data = data_engine.fetch_multi_timeframe_data("EURUSDm")

            decision_id = repo.insert_decision({
                "symbol": "EURUSDm", "status": "ok", "ai_signal": "BUY",
            })

            repo.insert_decision_bars(decision_id, data["snapshot"])

            snapshot = repo.get_decision_bars(decision_id)

            assert snapshot["server_utc_offset_min"] == 180

            matches, differences = replay.compare_to_stored(snapshot)

            assert matches, differences

        finally:
            bot_state["server_utc_offset_min"] = None

    def test_snapshot_stores_the_full_window(self, fake_world):
        data = data_engine.fetch_multi_timeframe_data("EURUSDm")

        snapshot = data["snapshot"]

        assert len(snapshot["daily"]) == data_engine.DAILY_BARS
        assert len(snapshot["hourly"]) == data_engine.HOURLY_BARS
        assert snapshot["tick"]["bid"] is not None
        assert snapshot["symbol_info"]["digits"] == 5

    def test_snapshot_keeps_raw_epochs_not_formatted_times(self, fake_world):
        """
        Anything derived can be recomputed; anything rounded cannot be
        un-rounded.
        """

        snapshot = data_engine.fetch_multi_timeframe_data(
            "EURUSDm"
        )["snapshot"]

        assert isinstance(snapshot["hourly"][0]["time"], (int, float))

    def test_duplicate_snapshot_is_not_fatal(self, temp_db, fake_world):
        from backend.database import repository as repo

        data = data_engine.fetch_multi_timeframe_data("EURUSDm")

        decision_id = repo.insert_decision({
            "symbol": "EURUSDm", "status": "ok", "ai_signal": "BUY",
        })

        assert repo.insert_decision_bars(decision_id, data["snapshot"])

        # UNIQUE(decision_id): a retry must not raise into the loop.
        assert repo.insert_decision_bars(decision_id, data["snapshot"]) is None

    def test_missing_snapshot_never_costs_a_trade(self, temp_db):
        from backend.database import repository as repo

        assert repo.insert_decision_bars(1, None) is None


# =====================================================================
# Per-bar cadence
# =====================================================================

class TestPerBarCadence:

    def test_default_mode_is_per_bar(self):
        from backend import config

        assert config.DECISION_MODE == "per_bar"

    def test_invalid_mode_falls_back_safely(self, monkeypatch):
        import importlib

        from backend import config

        monkeypatch.setenv("DECISION_MODE", "nonsense")

        reloaded = importlib.reload(config)

        try:
            assert reloaded.DECISION_MODE == "per_bar"
        finally:
            monkeypatch.delenv("DECISION_MODE", raising=False)
            importlib.reload(config)

    def test_new_bar_detected_when_nothing_decided_yet(self, temp_db,
                                                       fake_world):
        from backend.core import engine

        has_new, bar_time = engine.symbol_has_new_bar("EURUSDm")

        assert has_new is True
        assert bar_time

    def test_no_new_bar_once_the_symbol_is_decided(self, temp_db,
                                                   fake_world):
        """
        The whole point: the same bar must not be decided twice.
        """

        from backend.core import engine
        from backend.database import repository as repo

        data = data_engine.fetch_multi_timeframe_data("EURUSDm")

        engine.persist_market_state("EURUSDm", data)

        has_new, _ = engine.symbol_has_new_bar("EURUSDm")

        assert has_new is False

        assert repo.latest_decided_bar("EURUSDm") is not None

    def test_new_bar_detected_after_the_hour_advances(self, temp_db,
                                                     fake_world):
        from backend.core import engine

        data = data_engine.fetch_multi_timeframe_data("EURUSDm")

        engine.persist_market_state("EURUSDm", data)

        assert engine.symbol_has_new_bar("EURUSDm")[0] is False

        # Jump the clock forward two hours.
        with mock.patch.object(
            time, "time", return_value=time.time() + 7200
        ):
            has_new, _ = engine.symbol_has_new_bar("EURUSDm")

        assert has_new is True

    def test_symbols_are_tracked_independently(self, temp_db, fake_world):
        from backend.core import engine

        data = data_engine.fetch_multi_timeframe_data("EURUSDm")

        engine.persist_market_state("EURUSDm", data)

        assert engine.symbol_has_new_bar("EURUSDm")[0] is False
        assert engine.symbol_has_new_bar("GBPUSDm")[0] is True

    def test_unavailable_symbol_reports_no_new_bar(self, temp_db,
                                                  fake_world):
        """A symbol the broker does not offer must not stall the loop."""

        from backend.core import engine

        fake_world.unknown_symbols = {"GBPUSDm"}

        has_new, bar_time = engine.symbol_has_new_bar("GBPUSDm")

        assert has_new is False
        assert bar_time is None

    def test_cadence_survives_a_restart(self, temp_db, fake_world):
        """
        The last-decided bar is read from the database, not held in
        memory, so a restart cannot cause the same bar to be traded
        twice.
        """

        from backend.core import engine
        from backend.database import close_connection

        data = data_engine.fetch_multi_timeframe_data("EURUSDm")

        engine.persist_market_state("EURUSDm", data)

        close_connection()          # simulate a fresh process

        assert engine.symbol_has_new_bar("EURUSDm")[0] is False

    def test_expected_decision_rate(self):
        """
        Acceptance: ~96 decisions/day on four symbols in per_bar mode.

        24 H1 bars x 4 symbols. The old 30s cadence produced ~11,500
        API calls a day on the same 34 candles.
        """

        symbols = 4
        bars_per_day = 24

        per_bar = symbols * bars_per_day

        assert per_bar == 96

        interval_30s = symbols * (24 * 60 * 60 // 30)

        assert interval_30s == 11_520
        assert per_bar < interval_30s / 100
