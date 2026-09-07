"""
SQLite connection management for the trading bot.

Deliberately small: one file-backed database, WAL mode so the FastAPI
request handlers can read while the trading loop writes, and a module
level lock so concurrent writes from the async loop are serialised.
"""

import os
import sqlite3
import threading
from pathlib import Path


# ---------------------------------------------------------------------
# Location
# ---------------------------------------------------------------------

_PACKAGE_DIR = Path(__file__).resolve().parent

DEFAULT_DB_PATH = _PACKAGE_DIR / "quantbot.db"

SCHEMA_PATH = _PACKAGE_DIR / "schema.sql"


def get_db_path():
    """
    Resolve the database file path.

    Overridable with the DB_PATH environment variable so tests (and a
    second machine) can point somewhere else without touching code.
    """

    override = os.getenv("DB_PATH")

    if override:
        return Path(override).expanduser().resolve()

    return DEFAULT_DB_PATH


# ---------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------

# SQLite serialises writers itself, but a Python-side lock gives us
# clean "one writer at a time" semantics and avoids spurious
# "database is locked" errors under the 30s trading cadence.
write_lock = threading.RLock()

_local = threading.local()


def get_connection():
    """
    Return a per-thread sqlite3 connection with sane pragmas.

    Rows come back as sqlite3.Row so callers can use dict(row).
    """

    conn = getattr(_local, "conn", None)

    if conn is not None:
        return conn

    db_path = get_db_path()

    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(
        str(db_path),
        timeout=30.0,
        isolation_level=None,          # autocommit; we manage transactions
        check_same_thread=False,
    )

    conn.row_factory = sqlite3.Row

    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")

    _local.conn = conn

    return conn


def close_connection():
    """Close this thread's connection, if it has one."""

    conn = getattr(_local, "conn", None)

    if conn is not None:
        try:
            conn.close()
        finally:
            _local.conn = None


# ---------------------------------------------------------------------
# Initialisation
# ---------------------------------------------------------------------

def initialize_database():
    """
    Create the schema if it does not exist.

    Safe to call on every startup: every statement in schema.sql is
    IF NOT EXISTS, so this is idempotent.
    """

    conn = get_connection()

    schema_sql = SCHEMA_PATH.read_text(encoding="utf-8")

    with write_lock:
        conn.executescript(schema_sql)

    return get_db_path()
