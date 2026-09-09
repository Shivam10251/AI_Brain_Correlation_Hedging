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

# backend/database -> backend -> project root
PROJECT_ROOT = _PACKAGE_DIR.parent.parent

# Source code lives in backend/, data lives in data/. Keeping the
# database out of the package means a redeploy of the code never risks
# touching live trading history.
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "quantbot.db"

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

# Columns added to tables that already exist in a live database.
#
# CREATE TABLE IF NOT EXISTS cannot add a column to a table that is
# already there, so a database created before a given phase would never
# gain the new columns. Each entry here is applied with ALTER TABLE ADD
# COLUMN only when the column is missing.
#
# Rules for anything added to this map:
#   * additive only - never drop, rename or retype a column
#   * no NOT NULL without a DEFAULT (SQLite rejects it on ADD COLUMN)
#   * safe to run on every startup, forever
ADDITIVE_COLUMNS = {
    # Phase 1 - decision reproducibility
    "decisions": (
        ("prompt_hash", "TEXT"),
        ("temperature", "REAL"),
        ("strategy_version_id", "INTEGER"),
    ),
}


def _existing_tables(conn):
    return {
        row["name"]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }


def _existing_columns(conn, table):
    return {row["name"] for row in conn.execute(f'PRAGMA table_info("{table}")')}


def apply_additive_migrations(conn):
    """
    Bring an existing database up to the current column set.

    Returns the list of "table.column" strings that were added, so
    startup can log exactly what changed. Never destructive.
    """

    added = []

    tables = _existing_tables(conn)

    for table, columns in ADDITIVE_COLUMNS.items():

        if table not in tables:
            # Fresh database: schema.sql already created it in full.
            continue

        present = _existing_columns(conn, table)

        for name, column_type in columns:

            if name in present:
                continue

            conn.execute(
                f'ALTER TABLE "{table}" ADD COLUMN "{name}" {column_type}'
            )

            added.append(f"{table}.{name}")

    return added


def initialize_database():
    """
    Create the schema if it does not exist, then apply additive column
    migrations to a database that predates them.

    Safe to call on every startup: every statement in schema.sql is
    IF NOT EXISTS, and the migration only ever adds missing columns.
    """

    conn = get_connection()

    schema_sql = SCHEMA_PATH.read_text(encoding="utf-8")

    with write_lock:
        conn.executescript(schema_sql)

        added = apply_additive_migrations(conn)

    if added:
        print(f"Database migrated: added {', '.join(added)}")

    return get_db_path()
