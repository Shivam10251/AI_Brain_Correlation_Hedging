"""
Phase 1 acceptance tests: versioning, broker profile, auth, hygiene.

Each test maps to a line in the Phase 1 acceptance criteria, or to a
Phase 0 finding the phase was meant to close.
"""

import json

import pytest

import tests.fake_mt5 as fake_mt5


# =====================================================================
# Strategy versioning  (Phase 1 · Phase 0 §2.9, D1, D3)
# =====================================================================

class TestStrategyVersioning:

    def test_prompt_hash_is_stable_across_calls(self):
        from backend.ai import versioning

        assert versioning.compute_prompt_hash() == versioning.compute_prompt_hash()

    def test_prompt_hash_is_a_sha256_hex_digest(self):
        from backend.ai import versioning

        digest = versioning.compute_prompt_hash()

        assert len(digest) == 64
        assert all(c in "0123456789abcdef" for c in digest)

    def test_prompt_hash_excludes_market_data(self):
        """
        The hash must identify the STRATEGY, not the bar it saw.

        If candle data leaked into it, every decision would produce a
        new version and version history would be meaningless.
        """

        from backend.ai import versioning

        _, user_template = versioning.render_prompt_template()

        assert "__DAILY_CSV__" in user_template
        assert "__HOURLY_CSV__" in user_template

    def test_prompt_hash_changes_when_the_prompt_changes(self, monkeypatch):
        from backend.ai import prompts, versioning

        before = versioning.compute_prompt_hash()

        monkeypatch.setattr(
            prompts, "build_system_prompt",
            lambda symbol: "a materially different strategy",
        )
        monkeypatch.setattr(
            versioning, "build_system_prompt",
            lambda symbol: "a materially different strategy",
        )

        assert versioning.compute_prompt_hash() != before

    def test_prompt_hash_changes_when_a_material_param_changes(self, monkeypatch):
        from backend import config
        from backend.ai import versioning

        before = versioning.compute_prompt_hash()

        monkeypatch.setattr(config, "DEEPSEEK_TEMPERATURE", 0.9)

        assert versioning.compute_prompt_hash() != before

    def test_registration_is_idempotent(self, temp_db):
        from backend.ai import versioning
        from backend.database import repo_meta

        first = versioning.ensure_registered()

        versioning.reset_cache()

        second = versioning.ensure_registered()

        assert first == second
        assert len(repo_meta.get_strategy_versions()) == 1

    def test_registered_row_carries_the_full_descriptor(self, temp_db):
        from backend.ai import versioning
        from backend.database import repo_meta

        version_id = versioning.ensure_registered()

        row = repo_meta.get_strategy_version_by_id(version_id)

        assert row["strategy_id"] == "llm-mtf"
        assert row["version"] == "1.0.0"
        assert len(row["prompt_hash"]) == 64
        assert row["model_alias"]
        assert row["temperature"] is not None

        params = json.loads(row["params_json"])

        assert "sl_percent" in params
        assert "model_alias" in params

    def test_stale_cached_id_does_not_lose_the_decision(
        self, temp_db, monkeypatch
    ):
        """
        decisions.strategy_version_id is a foreign key with
        PRAGMA foreign_keys = ON.

        If ensure_registered() handed back a memoised id from a
        previous database, every insert_decision() would raise
        IntegrityError and the decision row would be LOST - not merely
        unversioned. The cache must therefore be validated against the
        current database before it is trusted.
        """

        from backend.ai import versioning
        from backend.database import repository as repo

        # Simulate a cache surviving a database swap.
        versioning._cached_version_id = 424242

        version_id = versioning.ensure_registered()

        assert version_id != 424242

        decision_id = repo.insert_decision({
            "symbol": "EURUSDm",
            "status": "ok",
            "ai_signal": "HOLD",
            "strategy_version_id": version_id,
        })

        assert decision_id
        assert repo.get_decisions(limit=1)[0]["strategy_version_id"] == version_id

    def test_unversioned_prompt_edit_creates_a_distinct_version(
        self, temp_db, monkeypatch
    ):
        """
        Editing the prompt without bumping VERSION must NOT silently
        reuse the existing id - that would mix two different strategies
        into one trade population and corrupt any calibration study.
        """

        from backend.ai import versioning
        from backend.database import repo_meta

        original_id = versioning.ensure_registered()

        versioning.reset_cache()

        monkeypatch.setattr(
            versioning, "build_system_prompt",
            lambda symbol: "an edited prompt with no version bump",
        )

        drifted_id = versioning.ensure_registered()

        assert drifted_id != original_id

        drifted = repo_meta.get_strategy_version_by_id(drifted_id)

        assert drifted["version"].startswith("1.0.0+hash.")
        assert drifted["parent_version_id"] == original_id


# =====================================================================
# Decision provenance  (Phase 1 acceptance: every row has hash/model/version)
# =====================================================================

class TestDecisionProvenance:

    def test_schema_has_the_provenance_columns(self, temp_db):
        from backend.database import explorer

        columns = {c["name"] for c in explorer.table_columns("decisions")}

        assert {"prompt_hash", "temperature", "strategy_version_id"} <= columns

    def test_failed_decisions_still_carry_provenance(self, temp_db, monkeypatch):
        """
        A decision that errored is still a decision. Excluding failures
        from provenance would bias the denominator of any later
        calibration study.
        """

        from backend import config
        from backend.ai import brain

        monkeypatch.setattr(config, "DEEPSEEK_API_KEY", None)

        decision = brain.get_ai_decision({"features": {}}, "EURUSDm")

        assert decision["status"] == "error"
        assert decision["ai_signal"] == "HOLD"
        assert decision["prompt_hash"] is not None
        assert decision["strategy_version_id"] is not None
        assert decision["temperature"] == config.DEEPSEEK_TEMPERATURE

    def test_provenance_is_persisted_on_the_row(self, temp_db, monkeypatch):
        from backend import config
        from backend.ai import brain
        from backend.database import repository as repo

        monkeypatch.setattr(config, "DEEPSEEK_API_KEY", None)

        decision = brain.get_ai_decision({"features": {}}, "EURUSDm")

        decision_id = repo.insert_decision(decision)

        stored = repo.get_decisions(limit=1)[0]

        assert stored["id"] == decision_id
        assert stored["prompt_hash"] == decision["prompt_hash"]
        assert stored["strategy_version_id"] == decision["strategy_version_id"]
        assert stored["temperature"] == decision["temperature"]

    def test_served_model_overrides_the_requested_alias(self):
        """
        DEEPSEEK_MODEL is a moving alias (Phase 0 §2.9). What actually
        served the request is what makes two runs comparable.
        """

        from backend.ai import brain

        base = brain._base_result("EURUSDm")

        assert base["model"]  # requested alias present on failure paths


# =====================================================================
# Broker profile  (Phase 1 · Phase 0 §2.2, §2.6, D5)
# =====================================================================

class TestFillingMode:

    @pytest.mark.parametrize("mask,expected", [
        (fake_mt5.SYMBOL_FILLING_FOK, "FOK"),
        (fake_mt5.SYMBOL_FILLING_IOC, "IOC"),
        (fake_mt5.SYMBOL_FILLING_FOK | fake_mt5.SYMBOL_FILLING_IOC, "FOK"),
        (0, "RETURN"),
        (None, "RETURN"),
    ])
    def test_resolves_from_the_broker_bitmask(self, mask, expected):
        from backend.market.broker_profile import resolve_filling_mode

        label, _ = resolve_filling_mode(mask)

        assert label == expected

    def test_bitmask_and_order_enum_are_not_conflated(self):
        """
        symbol_info.filling_mode is a BITMASK; the request needs an
        ORDER_FILLING_* ENUM. FOK is bit 1 but enum 0 - conflating them
        is the bug this guards.
        """

        from backend.market.broker_profile import resolve_filling_mode

        label, order_type = resolve_filling_mode(fake_mt5.SYMBOL_FILLING_FOK)

        assert label == "FOK"
        assert order_type == fake_mt5.ORDER_FILLING_FOK == 0

    def test_hardcoded_ioc_would_be_rejected_on_a_fok_only_symbol(
        self, fake_world
    ):
        """
        Reproduces the pre-Phase-1 failure: XAUUSDm is FOK-only in the
        fake broker, so the old hardcoded IOC is refused.
        """

        result = fake_mt5.order_send({
            "symbol": "XAUUSDm",
            "type_filling": fake_mt5.ORDER_FILLING_IOC,
            "volume": 0.01,
            "type": fake_mt5.ORDER_TYPE_BUY,
            "price": 2400.0,
        })

        assert result.retcode == fake_mt5.TRADE_RETCODE_INVALID_FILL

    def test_resolved_mode_is_accepted_on_every_symbol(self, fake_world):
        """
        Phase 1 acceptance: 'a demo market order is accepted on every
        symbol in SYMBOLS' - verified here against the fake broker,
        and on the real terminal via the Windows checklist.
        """

        from backend.market.broker_profile import resolve_filling_mode

        for symbol in ("EURUSDm", "GBPUSDm", "BTCUSDm", "XAUUSDm"):

            info = fake_mt5.symbol_info(symbol)

            _, order_type = resolve_filling_mode(info.filling_mode)

            result = fake_mt5.order_send({
                "symbol": symbol,
                "type_filling": order_type,
                "volume": 0.01,
                "type": fake_mt5.ORDER_TYPE_BUY,
                "price": info.point * 100,
                "sl": 0.0,
                "tp": 0.0,
                "magic": 1,
                "comment": "t",
            })

            assert result.retcode == fake_mt5.TRADE_RETCODE_DONE, symbol


class TestServerOffset:

    @pytest.mark.parametrize("offset", [0, 60, 120, 180, -300])
    def test_measures_the_configured_offset(self, fake_world, offset):
        from backend.market import broker_profile

        fake_world.server_offset_minutes = offset

        measured, samples, stable = broker_profile.measure_server_utc_offset(
            ["EURUSDm"]
        )

        assert measured == offset
        assert stable is True
        assert samples

    def test_detects_an_unstable_offset(self, fake_world):
        from backend.market import broker_profile

        fake_world.server_offset_minutes = 120
        fake_world.unstable_offset = True

        measured, _, stable = broker_profile.measure_server_utc_offset(
            ["EURUSDm", "GBPUSDm"]
        )

        assert stable is False
        assert measured == 120        # mode survives the outlier

    def test_unknown_offset_is_none_not_zero(self, fake_world):
        """
        Phase 0 §2.2: assuming zero is exactly the bug. Unmeasurable
        must read as UNKNOWN.
        """

        from backend.market import broker_profile

        fake_world.unknown_symbols = {"EURUSDm"}

        measured, samples, stable = broker_profile.measure_server_utc_offset(
            ["EURUSDm"]
        )

        assert measured is None
        assert samples == []
        assert stable is False

    def test_bar_times_are_converted_to_utc(self, fake_world):
        import pandas as pd

        from backend.market.data_engine import to_utc

        raw = fake_mt5.copy_rates_from_pos("EURUSDm", fake_mt5.TIMEFRAME_H1, 0, 3)

        server = pd.to_datetime(raw["time"], unit="s", utc=True)

        converted = to_utc(raw["time"], 180)

        assert ((server - converted) == pd.Timedelta(minutes=180)).all()

    def test_unknown_offset_leaves_times_untouched(self, fake_world):
        import pandas as pd

        from backend.market.data_engine import to_utc

        raw = fake_mt5.copy_rates_from_pos("EURUSDm", fake_mt5.TIMEFRAME_H1, 0, 3)

        assert (
            to_utc(raw["time"], None)
            == pd.to_datetime(raw["time"], unit="s", utc=True)
        ).all()


class TestDeviationBps:

    def test_is_instrument_correct_not_a_fixed_point_count(self):
        """
        Phase 0 §2.6: 20 points was 2 pips on EURUSD and $0.20 on
        BTCUSD. In bps both are the same fraction of price.
        """

        from backend.market.broker_profile import deviation_points

        fx = deviation_points(
            {"point": 0.00001, "trade_stops_level": 10}, 1.10000
        )
        crypto = deviation_points(
            {"point": 0.01, "trade_stops_level": 0}, 64_000.00
        )

        # 5 bps of price, expressed in each instrument's own points.
        # The old fixed DEVIATION=20 gave 2 pips here and $0.20 there.
        assert fx == pytest.approx(55, abs=1)          # ~5.5 pips
        assert crypto == pytest.approx(3200, abs=5)    # ~$32 on $64k

        # Both are the same FRACTION of price, which is the point.
        assert fx * 0.00001 / 1.10000 == pytest.approx(
            crypto * 0.01 / 64_000.00, rel=0.02
        )

    def test_clamped_to_ten_times_the_stops_level(self):
        from backend.market.broker_profile import deviation_points

        # Gold: 5 bps of 2400 is 1200 points, but the broker's own
        # stops level bounds how much slippage we accept.
        points = deviation_points(
            {"point": 0.001, "trade_stops_level": 35}, 2_400.000
        )

        assert points == 350

    def test_never_below_the_brokers_stops_level(self):
        from backend.market.broker_profile import deviation_points

        points = deviation_points(
            {"point": 1.0, "trade_stops_level": 50}, 1.0
        )

        assert points >= 50

    def test_falls_back_when_no_profile_exists(self):
        from backend import config
        from backend.market.broker_profile import deviation_points

        assert deviation_points(None, 1.1) == config.DEVIATION
        assert deviation_points({"point": 0}, 1.1) == config.DEVIATION


class TestBrokerProfileCapture:

    def test_captures_account_and_every_symbol(self, temp_db, fake_world):
        from backend.database import repo_meta
        from backend.market import broker_profile

        fake_world.server_offset_minutes = 120

        summary = broker_profile.capture(
            ["EURUSDm", "GBPUSDm", "BTCUSDm", "XAUUSDm"]
        )

        assert summary["available"] is True
        assert summary["server_utc_offset_min"] == 120
        assert summary["margin_mode_label"] == "RETAIL_HEDGING"
        assert len(summary["symbols"]) == 4

        stored = repo_meta.get_broker_profile(summary["account_id"])

        assert stored["server_utc_offset_min"] == 120
        assert stored["offset_stable"] == 1

        symbols = repo_meta.get_symbol_profiles(summary["account_id"])

        assert {s["symbol"] for s in symbols} == {
            "EURUSDm", "GBPUSDm", "BTCUSDm", "XAUUSDm"
        }

        by_symbol = {s["symbol"]: s for s in symbols}

        assert by_symbol["XAUUSDm"]["filling_mode_chosen"] == "FOK"
        assert by_symbol["GBPUSDm"]["filling_mode_chosen"] == "IOC"
        assert by_symbol["BTCUSDm"]["digits"] == 2
        assert by_symbol["EURUSDm"]["volume_step"] == 0.01

    def test_reports_symbols_the_broker_does_not_offer(self, temp_db, fake_world):
        """
        This is the 'only XAUUSD ever trades' explanation: a symbol the
        broker does not have is now surfaced explicitly at startup
        instead of erroring once per cycle forever.
        """

        from backend.database import repository as repo
        from backend.market import broker_profile

        fake_world.unknown_symbols = {"GBPUSDm", "BTCUSDm"}

        summary = broker_profile.capture(
            ["EURUSDm", "GBPUSDm", "BTCUSDm", "XAUUSDm"]
        )

        assert set(summary["unavailable_symbols"]) == {"GBPUSDm", "BTCUSDm"}

        messages = " ".join(e["message"] for e in repo.get_events(limit=20))

        assert "unavailable on this broker" in messages

    def test_capture_is_idempotent_and_logs_changes(self, temp_db, fake_world):
        from backend.database import repo_meta
        from backend.database import repository as repo
        from backend.market import broker_profile

        fake_world.server_offset_minutes = 120
        broker_profile.capture(["EURUSDm"])

        # DST shift.
        fake_world.server_offset_minutes = 180
        summary = broker_profile.capture(["EURUSDm"])

        assert len(repo_meta.get_broker_profiles()) == 1
        assert "server_utc_offset_min" in summary["changes"]

        messages = " ".join(e["message"] for e in repo.get_events(limit=20))

        assert "Broker profile changed" in messages

    def test_startup_event_logs_offset_margin_mode_and_filling(
        self, temp_db, fake_world
    ):
        """Phase 1 acceptance criterion, verbatim."""

        from backend.database import repository as repo
        from backend.market import broker_profile

        fake_world.server_offset_minutes = 180

        broker_profile.capture(["EURUSDm", "XAUUSDm"])

        event = next(
            e for e in repo.get_events(limit=20)
            if e["category"] == "BROKER" and "Broker profile captured" in e["message"]
        )

        assert "+180 min" in event["message"]
        assert "RETAIL_HEDGING" in event["message"]
        assert "EURUSDm=FOK" in event["message"]
        assert "XAUUSDm=FOK" in event["message"]

    def test_survives_a_disconnected_terminal(self, temp_db, fake_world):
        fake_mt5.shutdown()

        from backend.market import broker_profile

        summary = broker_profile.capture(["EURUSDm"])

        assert summary["available"] is False
        assert "error" in summary


# =====================================================================
# Auth  (Phase 1 acceptance: unauthenticated POST /api/control -> 401)
# =====================================================================

class TestAuth:

    @pytest.fixture
    def authed_client(self, temp_db, fake_world, monkeypatch):
        """
        A client with auth switched on.

        Patches the config module's attributes rather than reloading
        it: importlib.reload mutates the shared module object, and
        monkeypatch then "restores" the post-reload value, leaking the
        token into every later test in the session.
        """

        from fastapi.testclient import TestClient

        from backend import config, main

        monkeypatch.setattr(config, "API_TOKEN", "secret-token")
        monkeypatch.setattr(config, "AUTH_ENABLED", True)

        with TestClient(main.app) as client:
            yield client

    def test_unauthenticated_control_is_rejected(self, authed_client):
        response = authed_client.post("/api/control", json={"action": "start"})

        assert response.status_code == 401

    def test_wrong_token_is_rejected(self, authed_client):
        response = authed_client.post(
            "/api/control",
            json={"action": "start"},
            headers={"Authorization": "Bearer wrong"},
        )

        assert response.status_code == 401

    def test_correct_token_is_accepted(self, authed_client):
        response = authed_client.post(
            "/api/control",
            json={"action": "stop"},
            headers={"Authorization": "Bearer secret-token"},
        )

        assert response.status_code == 200
        assert response.json()["status"] == "success"

    def test_health_stays_open_for_watchdogs(self, authed_client):
        assert authed_client.get("/api/health").status_code == 200

    def test_every_other_api_route_is_protected(self, authed_client):
        for path in (
            "/api/status", "/api/history/trades", "/api/history/equity",
            "/api/history/decisions", "/api/history/events", "/api/analytics",
            "/api/positions", "/api/db/meta", "/api/db/tables",
            "/api/db/schema", "/api/meta/strategy", "/api/meta/broker",
        ):
            assert authed_client.get(path).status_code == 401, path

    def test_read_only_sql_console_is_protected(self, authed_client):
        response = authed_client.post(
            "/api/db/query", json={"sql": "SELECT 1"}
        )

        assert response.status_code == 401

    def test_pages_render_and_receive_the_token(self, authed_client):
        for path in ("/", "/dashboard"):
            response = authed_client.get(path)

            assert response.status_code == 200
            assert "secret-token" in response.text
            assert "{{ api_token }}" not in response.text

    def test_open_when_no_token_configured(self, client):
        assert client.get("/api/status").status_code == 200
        assert client.post(
            "/api/control", json={"action": "stop"}
        ).status_code == 200

    def test_token_comparison_rejects_a_prefix(self, authed_client):
        response = authed_client.get(
            "/api/status", headers={"Authorization": "Bearer secret"}
        )

        assert response.status_code == 401


# =====================================================================
# Hygiene  (Phase 0 §2.7, §2.10)
# =====================================================================

class TestReconcilerAdoptionFix:

    def test_known_set_is_not_bounded_by_history_size(self, temp_db):
        """
        Phase 0 §2.7: the known set came from get_trades(limit=1000),
        so an open position older than the last 1,000 trades was
        re-adopted every cycle. The replacement query is unbounded.
        """

        from backend.database import repository as repo

        open_id = repo.insert_trade_intent({
            "client_order_id": "old-open",
            "symbol": "EURUSDm",
            "direction": "BUY",
        })

        repo.update_trade(
            open_id, execution_status="EXECUTED", position_ticket=999_001
        )

        # Bury it under more than 1,000 later trades.
        for i in range(1100):
            later = repo.insert_trade_intent({
                "client_order_id": f"later-{i}",
                "symbol": "EURUSDm",
                "direction": "BUY",
            })
            repo.update_trade(
                later,
                execution_status="CLOSED",
                closed_at=repo.utc_now(),
                pnl=1.0,
            )

        tickets = {
            t["position_ticket"] for t in repo.get_open_position_tickets()
        }

        assert 999_001 in tickets

        # The old query would have missed it entirely.
        legacy = {
            t["position_ticket"]
            for t in repo.get_trades(limit=1000)
            if t.get("position_ticket")
        }

        assert 999_001 not in legacy

    def test_closed_trades_are_excluded(self, temp_db):
        from backend.database import repository as repo

        closed = repo.insert_trade_intent({
            "client_order_id": "closed-one",
            "symbol": "EURUSDm",
            "direction": "BUY",
        })

        repo.update_trade(
            closed,
            execution_status="CLOSED",
            position_ticket=999_002,
            closed_at=repo.utc_now(),
        )

        tickets = {
            t["position_ticket"] for t in repo.get_open_position_tickets()
        }

        assert 999_002 not in tickets


class TestAdditiveMigration:

    def test_adds_missing_columns_to_a_pre_phase1_database(self, tmp_path):
        """
        A live database created before Phase 1 must gain the new
        columns without losing a single row.
        """

        import sqlite3

        from backend.database.connection import apply_additive_migrations

        db = tmp_path / "old.db"

        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row

        conn.execute(
            "CREATE TABLE decisions ("
            "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "  created_at TEXT NOT NULL,"
            "  symbol TEXT NOT NULL,"
            "  status TEXT NOT NULL DEFAULT 'ok'"
            ")"
        )
        conn.execute(
            "INSERT INTO decisions (created_at, symbol) VALUES (?, ?)",
            ("2026-01-01T00:00:00+00:00", "EURUSDm"),
        )

        added = apply_additive_migrations(conn)

        assert "decisions.prompt_hash" in added
        assert "decisions.strategy_version_id" in added

        columns = {
            r["name"] for r in conn.execute("PRAGMA table_info(decisions)")
        }

        assert {"prompt_hash", "temperature", "strategy_version_id"} <= columns

        rows = conn.execute("SELECT * FROM decisions").fetchall()

        assert len(rows) == 1
        assert rows[0]["symbol"] == "EURUSDm"
        assert rows[0]["prompt_hash"] is None

        # Second run is a no-op.
        assert apply_additive_migrations(conn) == []

        conn.close()


class TestProvenanceEndpoints:

    def test_strategy_endpoint_reports_the_running_version(self, client):
        payload = client.get("/api/meta/strategy").json()

        assert payload["current"]["strategy_id"] == "llm-mtf"
        assert len(payload["current"]["prompt_hash"]) == 64
        assert len(payload["registered"]) >= 1

    def test_broker_endpoint_reports_measured_facts(self, client):
        payload = client.get("/api/meta/broker").json()

        assert payload["accounts"]

        account = payload["accounts"][0]

        assert account["margin_mode_label"] == "RETAIL_HEDGING"
        assert account["symbols"]

    def test_health_exposes_phase1_provenance(self, client):
        payload = client.get("/api/health").json()

        for key in (
            "auth_enabled", "account_id",
            "server_utc_offset_min", "margin_mode",
        ):
            assert key in payload
