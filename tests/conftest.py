"""
Shared pytest setup (Phase 1).

MetaTrader5 is a Windows-only package, so every test in this repo runs
against `tests/fake_mt5.py`. That shim must be installed into
`sys.modules` BEFORE any backend module is imported, which is why it
happens here at collection time rather than inside a fixture.

Each test that touches the database gets its own temporary file, so no
test can ever see - let alone modify - the live `data/quantbot.db`.
"""

import os
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ---------------------------------------------------------------------
# Pin DB_PATH before anything can open a connection.
#
# This is a SAFETY interlock, not a convenience. Some code paths reach
# the database without going through the `temp_db` fixture - notably
# ai.brain._provenance(), which registers a strategy version on every
# decision. Without this, running pytest on the Windows trading host
# would open data/quantbot.db: the LIVE trading history.
#
# Set at import time, because backend.config reads os.environ at module
# scope and pytest imports test modules before fixtures ever run.
# ---------------------------------------------------------------------

_SESSION_DB_DIR = tempfile.mkdtemp(prefix="quantbot-pytest-")

os.environ["DB_PATH"] = os.path.join(_SESSION_DB_DIR, "session.db")


# The two original suites are standalone scripts: they run their checks
# at import time and finish with sys.exit(). Collecting them directly
# would abort the pytest session, so they are excluded here and driven
# as subprocesses by test_legacy_suites.py instead - which keeps all 79
# of their checks running.
collect_ignore = [
    "test_api.py",
    "test_restart_recovery.py",
]


# ---------------------------------------------------------------------
# Install the MT5 shim before backend imports happen
# ---------------------------------------------------------------------

import tests.fake_mt5 as fake_mt5                    # noqa: E402

fake_mt5.install()


# Deterministic environment. Set before `backend.config` is imported,
# because it reads os.environ at module scope.
os.environ.setdefault("DEEPSEEK_API_KEY", "test-key")
os.environ.setdefault("SYMBOLS", "EURUSDm,GBPUSDm,BTCUSDm,XAUUSDm")
os.environ.setdefault("RESUME_ENGINE_ON_STARTUP", "false")


@pytest.fixture(autouse=True)
def never_touch_the_live_database():
    """
    Fail loudly if any test escapes to the default database path.

    A test that writes to data/quantbot.db on the trading host would be
    corrupting real trading history, so this is worth an explicit
    assertion rather than trusting every code path to be well behaved.
    """

    from backend.database.connection import DEFAULT_DB_PATH

    existed = DEFAULT_DB_PATH.exists()

    yield

    if DEFAULT_DB_PATH.exists() and not existed:
        DEFAULT_DB_PATH.unlink()

        raise AssertionError(
            f"A test created {DEFAULT_DB_PATH}. Tests must never reach "
            f"the default database path - use the temp_db fixture, or "
            f"check that DB_PATH is honoured on this code path."
        )


@pytest.fixture
def fake_world():
    """A clean fake-MT5 world per test."""

    fake_mt5.world.reset()
    fake_mt5.initialize()

    yield fake_mt5.world

    fake_mt5.world.reset()


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    """
    An isolated database for one test.

    Points DB_PATH at a temp file, resets the per-thread connection and
    the memoised strategy-version id, then initialises the schema.
    """

    from backend.ai import versioning
    from backend.database import close_connection, initialize_database
    from backend.database import explorer

    db_path = tmp_path / "quantbot.db"

    monkeypatch.setenv("DB_PATH", str(db_path))

    close_connection()
    explorer.close_connection()
    versioning.reset_cache()

    initialize_database()

    yield db_path

    close_connection()
    explorer.close_connection()
    versioning.reset_cache()


@pytest.fixture
def client(temp_db, fake_world):
    """
    A TestClient with the app's real lifespan executed.

    Depends on temp_db so the lifespan's initialize_database() writes to
    the temp file rather than the live database.
    """

    from fastapi.testclient import TestClient

    from backend import main

    with TestClient(main.app) as test_client:
        yield test_client
