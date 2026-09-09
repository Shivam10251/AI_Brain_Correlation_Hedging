"""
Read-only database explorer for the Quant DB Dashboard.

This module NEVER writes. It opens its own SQLite connection with
``PRAGMA query_only = ON`` so, even if a stray statement slips past the
validation below, the engine's ``data/quantbot.db`` cannot be mutated,
migrated or dropped from the dashboard.

Concurrency: the trading loop keeps the database in WAL mode, so this
read-only connection can read freely while the loop writes. A short
``busy_timeout`` covers the rare checkpoint contention.

Everything is derived on demand from the live rows - nothing is cached,
so the dashboard can never drift from what the engine actually stored.
"""

from __future__ import annotations

import os
import re
import sqlite3
import threading
import time
from typing import Any

from .connection import get_db_path


# ---------------------------------------------------------------------
# Read-only connection (separate from the engine's write connection)
# ---------------------------------------------------------------------

_local = threading.local()

# Hard ceilings so a huge table can never be pulled into memory whole.
MAX_PAGE_SIZE = 500
DEFAULT_PAGE_SIZE = 50
MAX_QUERY_ROWS = 1000

# Columns that, when present, carry a row's "as of" timestamp. Used for
# the "last updated" metric and per-table freshness.
_TIMESTAMP_COLUMNS = (
    "created_at", "updated_at", "closed_at", "opened_at",
    "timestamp", "time", "date", "datetime",
)


def _connect() -> sqlite3.Connection:
    """Per-thread read-only connection with safe pragmas."""

    conn = getattr(_local, "conn", None)

    if conn is not None:
        return conn

    db_path = get_db_path()

    # uri=True + a normal (not ?mode=ro) DSN keeps WAL happy on every
    # platform; query_only enforces read-only at the engine level.
    conn = sqlite3.connect(
        str(db_path),
        timeout=15.0,
        isolation_level=None,
        check_same_thread=False,
    )

    conn.row_factory = sqlite3.Row

    conn.execute("PRAGMA query_only = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA foreign_keys = ON")

    _local.conn = conn

    return conn


def close_connection() -> None:
    conn = getattr(_local, "conn", None)

    if conn is not None:
        try:
            conn.close()
        finally:
            _local.conn = None


def database_exists() -> bool:
    return get_db_path().exists()


# ---------------------------------------------------------------------
# Introspection
# ---------------------------------------------------------------------

def list_tables() -> list[dict[str, Any]]:
    """Every user table and view, with its row-storage kind."""

    rows = _connect().execute(
        "SELECT name, type FROM sqlite_master "
        "WHERE type IN ('table', 'view') "
        "AND name NOT LIKE 'sqlite_%' "
        "ORDER BY type, name"
    ).fetchall()

    return [{"name": r["name"], "type": r["type"]} for r in rows]


def _table_names() -> set[str]:
    return {t["name"] for t in list_tables()}


def _require_table(name: str) -> str:
    """Whitelist a caller-supplied table name against real objects."""

    if name not in _table_names():
        raise ValueError(f"Unknown table: {name!r}")

    return name


def table_columns(table: str) -> list[dict[str, Any]]:
    _require_table(table)

    rows = _connect().execute(f'PRAGMA table_info("{table}")').fetchall()

    return [
        {
            "cid": r["cid"],
            "name": r["name"],
            "type": r["type"] or "",
            "not_null": bool(r["notnull"]),
            "default": r["dflt_value"],
            "primary_key": bool(r["pk"]),
        }
        for r in rows
    ]


def _column_names(table: str) -> list[str]:
    return [c["name"] for c in table_columns(table)]


def table_foreign_keys(table: str) -> list[dict[str, Any]]:
    _require_table(table)

    rows = _connect().execute(
        f'PRAGMA foreign_key_list("{table}")'
    ).fetchall()

    return [
        {
            "from": r["from"],
            "to_table": r["table"],
            "to_column": r["to"],
            "on_update": r["on_update"],
            "on_delete": r["on_delete"],
        }
        for r in rows
    ]


def table_indexes(table: str) -> list[dict[str, Any]]:
    _require_table(table)

    conn = _connect()

    indexes = conn.execute(f'PRAGMA index_list("{table}")').fetchall()

    result = []

    for idx in indexes:
        info = conn.execute(
            f'PRAGMA index_info("{idx["name"]}")'
        ).fetchall()

        result.append({
            "name": idx["name"],
            "unique": bool(idx["unique"]),
            "origin": idx["origin"],          # c / u / pk
            "columns": [i["name"] for i in info],
        })

    return result


def row_count(table: str) -> int:
    _require_table(table)

    row = _connect().execute(f'SELECT COUNT(*) AS n FROM "{table}"').fetchone()

    return int(row["n"]) if row else 0


def _latest_timestamp(table: str, columns: list[str]) -> str | None:
    ts_cols = [c for c in columns if c.lower() in _TIMESTAMP_COLUMNS]

    if not ts_cols:
        return None

    conn = _connect()
    best: str | None = None

    for col in ts_cols:
        try:
            row = conn.execute(
                f'SELECT MAX("{col}") AS m FROM "{table}"'
            ).fetchone()
        except sqlite3.Error:
            continue

        value = row["m"] if row else None

        if value is not None and (best is None or str(value) > str(best)):
            best = str(value)

    return best


def schema_overview() -> list[dict[str, Any]]:
    """Full schema: columns, PK, FKs, indexes and row count per table."""

    overview = []

    for table in list_tables():
        name = table["name"]
        columns = table_columns(name)

        overview.append({
            "name": name,
            "type": table["type"],
            "row_count": row_count(name),
            "columns": columns,
            "primary_key": [c["name"] for c in columns if c["primary_key"]],
            "foreign_keys": table_foreign_keys(name),
            "indexes": table_indexes(name),
            "last_updated": _latest_timestamp(
                name, [c["name"] for c in columns]
            ),
        })

    return overview


# ---------------------------------------------------------------------
# Database-level metadata
# ---------------------------------------------------------------------

def database_metadata() -> dict[str, Any]:
    db_path = get_db_path()

    if not db_path.exists():
        return {
            "available": False,
            "path": str(db_path),
            "message": (
                "Database file not found yet. It is created the first "
                "time the trading engine runs."
            ),
        }

    conn = _connect()

    page_count = conn.execute("PRAGMA page_count").fetchone()[0]
    page_size = conn.execute("PRAGMA page_size").fetchone()[0]
    sqlite_version = conn.execute("SELECT sqlite_version()").fetchone()[0]
    journal_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]

    file_bytes = 0
    for suffix in ("", "-wal", "-shm"):
        candidate = db_path.parent / (db_path.name + suffix)
        if candidate.exists():
            file_bytes += os.path.getsize(candidate)

    tables = list_tables()

    per_table = []
    total_rows = 0
    global_last_updated: str | None = None

    for t in tables:
        count = row_count(t["name"])
        total_rows += count

        last_updated = _latest_timestamp(t["name"], _column_names(t["name"]))

        if last_updated and (
            global_last_updated is None or last_updated > global_last_updated
        ):
            global_last_updated = last_updated

        per_table.append({
            "name": t["name"],
            "type": t["type"],
            "row_count": count,
            "last_updated": last_updated,
        })

    return {
        "available": True,
        "path": str(db_path),
        "sqlite_version": sqlite_version,
        "journal_mode": journal_mode,
        "page_count": page_count,
        "page_size": page_size,
        "size_bytes": page_count * page_size,
        "file_bytes": file_bytes,
        "table_count": sum(1 for t in tables if t["type"] == "table"),
        "view_count": sum(1 for t in tables if t["type"] == "view"),
        "total_rows": total_rows,
        "last_updated": global_last_updated,
        "tables": per_table,
    }


# ---------------------------------------------------------------------
# Paginated table data (server-side paging / search / sort / filter)
# ---------------------------------------------------------------------

def table_data(
    table: str,
    *,
    limit: int = DEFAULT_PAGE_SIZE,
    offset: int = 0,
    order_by: str | None = None,
    order_dir: str = "asc",
    search: str | None = None,
    filters: dict[str, str] | None = None,
) -> dict[str, Any]:
    _require_table(table)

    columns = table_columns(table)
    column_names = [c["name"] for c in columns]

    limit = max(1, min(int(limit), MAX_PAGE_SIZE))
    offset = max(0, int(offset))

    # ----- ORDER BY (identifier must be whitelisted) -----
    order_sql = ""
    if order_by:
        if order_by not in column_names:
            raise ValueError(f"Unknown sort column: {order_by!r}")

        direction = "DESC" if str(order_dir).lower() == "desc" else "ASC"
        order_sql = f' ORDER BY "{order_by}" {direction}'
    else:
        pk = [c["name"] for c in columns if c["primary_key"]]
        if pk:
            order_sql = f' ORDER BY "{pk[0]}" ASC'

    # ----- WHERE (all values parameterised) -----
    where_parts: list[str] = []
    params: list[Any] = []

    if filters:
        for col, value in filters.items():
            if col not in column_names:
                raise ValueError(f"Unknown filter column: {col!r}")
            if value == "" or value is None:
                continue
            where_parts.append(f'CAST("{col}" AS TEXT) = ?')
            params.append(str(value))

    if search:
        term = f"%{search}%"
        ors = [f'CAST("{c}" AS TEXT) LIKE ?' for c in column_names]
        where_parts.append("(" + " OR ".join(ors) + ")")
        params.extend([term] * len(column_names))

    where_sql = (" WHERE " + " AND ".join(where_parts)) if where_parts else ""

    conn = _connect()

    total = conn.execute(
        f'SELECT COUNT(*) AS n FROM "{table}"{where_sql}', params
    ).fetchone()["n"]

    rows = conn.execute(
        f'SELECT * FROM "{table}"{where_sql}{order_sql} LIMIT ? OFFSET ?',
        [*params, limit, offset],
    ).fetchall()

    return {
        "table": table,
        "columns": columns,
        "rows": [dict(r) for r in rows],
        "total": int(total),
        "limit": limit,
        "offset": offset,
        "returned": len(rows),
    }


# ---------------------------------------------------------------------
# Read-only ad-hoc SQL
# ---------------------------------------------------------------------

_FORBIDDEN = re.compile(
    r"\b("
    r"insert|update|delete|drop|alter|create|replace|truncate|"
    r"attach|detach|reindex|vacuum|analyze|"
    r"pragma|begin|commit|rollback|savepoint|release"
    r")\b",
    re.IGNORECASE,
)

_ALLOWED_START = re.compile(r"^\s*(select|with|explain)\b", re.IGNORECASE)


def run_query(sql: str, *, max_rows: int = MAX_QUERY_ROWS) -> dict[str, Any]:
    """
    Execute a single read-only statement.

    Defence in depth:
      1. must be one statement,
      2. must start with SELECT / WITH / EXPLAIN,
      3. must not contain a write / DDL / transaction keyword,
      4. the connection itself is PRAGMA query_only = ON.
    """

    if not sql or not sql.strip():
        raise ValueError("Query is empty.")

    stripped = sql.strip().rstrip(";")

    if ";" in stripped:
        raise ValueError("Only a single statement is allowed.")

    if not _ALLOWED_START.match(stripped):
        raise ValueError(
            "Only SELECT / WITH / EXPLAIN queries are allowed (read-only)."
        )

    if _FORBIDDEN.search(stripped):
        raise ValueError(
            "Query rejected: it contains a write or schema-modifying "
            "keyword. This console is read-only."
        )

    max_rows = max(1, min(int(max_rows), MAX_QUERY_ROWS))

    conn = _connect()

    started = time.perf_counter()

    try:
        cursor = conn.execute(stripped)
        fetched = cursor.fetchmany(max_rows + 1)
    except sqlite3.Error as error:
        raise ValueError(f"SQL error: {error}") from error

    elapsed_ms = (time.perf_counter() - started) * 1000.0

    truncated = len(fetched) > max_rows
    fetched = fetched[:max_rows]

    column_names = (
        [d[0] for d in cursor.description] if cursor.description else []
    )

    return {
        "columns": column_names,
        "rows": [
            {column_names[i]: row[i] for i in range(len(column_names))}
            for row in fetched
        ],
        "row_count": len(fetched),
        "truncated": truncated,
        "execution_ms": round(elapsed_ms, 3),
        "max_rows": max_rows,
    }


# ---------------------------------------------------------------------
# Quant / trading visualisations - built from whatever is really there
# ---------------------------------------------------------------------

def _has_table(name: str) -> bool:
    return name in _table_names()


def _has_columns(table: str, *cols: str) -> bool:
    if not _has_table(table):
        return False
    present = set(_column_names(table))
    return all(c in present for c in cols)


_QUANT_FIELD_PATTERNS = {
    "timestamp": re.compile(r"(created_at|updated_at|closed_at|opened_at|_at$|time|date|datetime|timestamp)", re.I),
    "symbol": re.compile(r"(symbol|ticker|instrument|pair|asset)", re.I),
    "price": re.compile(r"(price|bid|ask|open|high|low|close|entry|exit|sl|tp|stop_loss|take_profit)", re.I),
    "volume": re.compile(r"(volume|qty|quantity|lots?|size)", re.I),
    "pnl": re.compile(r"(pnl|profit|loss|return|r_multiple|expectancy)", re.I),
    "position": re.compile(r"(position|ticket|magic|open_positions|exposure)", re.I),
    "signal": re.compile(r"(signal|decision|score|regime|setup|confidence|trend|momentum)", re.I),
    "equity": re.compile(r"(equity|balance|margin|drawdown|nav)", re.I),
}


def detect_quant_fields() -> dict[str, list[dict[str, str]]]:
    """Classify real columns into quant categories, by name."""

    detected: dict[str, list[dict[str, str]]] = {
        key: [] for key in _QUANT_FIELD_PATTERNS
    }

    for table in list_tables():
        for col in table_columns(table["name"]):
            for category, pattern in _QUANT_FIELD_PATTERNS.items():
                if pattern.search(col["name"]):
                    detected[category].append({
                        "table": table["name"],
                        "column": col["name"],
                        "type": col["type"],
                    })

    return {k: v for k, v in detected.items() if v}


def _rows(sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    return [dict(r) for r in _connect().execute(sql, params).fetchall()]


def quant_dashboard() -> dict[str, Any]:
    """
    A curated set of quant charts. Every block is guarded: if the
    underlying table/columns are missing the block is simply omitted,
    so this keeps working if the schema changes.
    """

    if not database_exists():
        return {"available": False}

    datasets: dict[str, Any] = {}
    notes: list[str] = []

    # ---- Equity curve -------------------------------------------------
    if _has_columns("equity_snapshots", "created_at", "equity"):
        datasets["equity_curve"] = _rows(
            "SELECT created_at, equity, balance, floating_pnl, open_positions "
            "FROM equity_snapshots ORDER BY id ASC LIMIT 5000"
        )

    # ---- Closed-trade P&L / equity / drawdown -----------------------
    if _has_columns("trades", "execution_status", "pnl", "closed_at"):
        closed = _rows(
            "SELECT closed_at, symbol, pnl, r_multiple, result, setup, "
            "       market_regime, ai_score, direction "
            "FROM trades "
            "WHERE execution_status = 'CLOSED' AND pnl IS NOT NULL "
            "ORDER BY closed_at ASC, id ASC"
        )

        running = 0.0
        peak = 0.0
        curve = []

        for t in closed:
            running += float(t["pnl"] or 0.0)
            peak = max(peak, running)
            curve.append({
                "closed_at": t["closed_at"],
                "symbol": t["symbol"],
                "pnl": t["pnl"],
                "cumulative_pnl": round(running, 2),
                "drawdown": round(running - peak, 2),
            })

        datasets["closed_trades"] = closed
        datasets["pnl_curve"] = curve

        # daily P&L
        datasets["daily_pnl"] = _rows(
            "SELECT substr(closed_at, 1, 10) AS day, "
            "       ROUND(SUM(pnl), 2) AS pnl, COUNT(*) AS trades "
            "FROM trades "
            "WHERE execution_status = 'CLOSED' AND pnl IS NOT NULL "
            "GROUP BY day ORDER BY day ASC LIMIT 365"
        )

        # P&L + win-rate by symbol
        datasets["by_symbol"] = _rows(
            "SELECT symbol, COUNT(*) AS trades, "
            "       ROUND(SUM(pnl), 2) AS pnl, "
            "       SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) AS wins, "
            "       SUM(CASE WHEN pnl < 0 THEN 1 ELSE 0 END) AS losses "
            "FROM trades "
            "WHERE execution_status = 'CLOSED' AND pnl IS NOT NULL "
            "GROUP BY symbol ORDER BY pnl DESC"
        )

        if _has_columns("trades", "setup"):
            datasets["by_setup"] = _rows(
                "SELECT COALESCE(setup, '(none)') AS setup, COUNT(*) AS trades, "
                "       ROUND(SUM(pnl), 2) AS pnl "
                "FROM trades "
                "WHERE execution_status = 'CLOSED' AND pnl IS NOT NULL "
                "GROUP BY setup ORDER BY pnl DESC"
            )

        if _has_columns("trades", "market_regime"):
            datasets["by_regime"] = _rows(
                "SELECT COALESCE(market_regime, '(none)') AS market_regime, "
                "       COUNT(*) AS trades, ROUND(SUM(pnl), 2) AS pnl "
                "FROM trades "
                "WHERE execution_status = 'CLOSED' AND pnl IS NOT NULL "
                "GROUP BY market_regime ORDER BY pnl DESC"
            )

        # outcome split
        datasets["outcomes"] = _rows(
            "SELECT COALESCE(result, '(unknown)') AS result, COUNT(*) AS n "
            "FROM trades "
            "WHERE execution_status = 'CLOSED' "
            "GROUP BY result ORDER BY n DESC"
        )

    # ---- Execution-status split (all trades) -----------------------
    if _has_columns("trades", "execution_status"):
        datasets["status_split"] = _rows(
            "SELECT execution_status, COUNT(*) AS n "
            "FROM trades GROUP BY execution_status ORDER BY n DESC"
        )
        datasets["trades_per_day"] = _rows(
            "SELECT substr(created_at, 1, 10) AS day, COUNT(*) AS n "
            "FROM trades GROUP BY day ORDER BY day ASC LIMIT 365"
        )

    # ---- AI decision distribution ---------------------------------
    if _has_columns("decisions", "ai_signal"):
        datasets["ai_signals"] = _rows(
            "SELECT COALESCE(ai_signal, '(none)') AS ai_signal, COUNT(*) AS n "
            "FROM decisions GROUP BY ai_signal ORDER BY n DESC"
        )
    if _has_columns("decisions", "final_decision"):
        datasets["final_decisions"] = _rows(
            "SELECT COALESCE(final_decision, '(none)') AS final_decision, "
            "       COUNT(*) AS n "
            "FROM decisions GROUP BY final_decision ORDER BY n DESC"
        )
    if _has_columns("decisions", "symbol"):
        datasets["decisions_by_symbol"] = _rows(
            "SELECT symbol, COUNT(*) AS n FROM decisions "
            "GROUP BY symbol ORDER BY n DESC"
        )
    if _has_columns("decisions", "ai_score"):
        datasets["score_histogram"] = _rows(
            "SELECT CASE "
            "  WHEN ai_score >= 80 THEN '80-100' "
            "  WHEN ai_score >= 60 THEN '60-79' "
            "  WHEN ai_score >= 40 THEN '40-59' "
            "  WHEN ai_score >= 20 THEN '20-39' "
            "  ELSE '0-19' END AS bucket, COUNT(*) AS n "
            "FROM decisions WHERE ai_score IS NOT NULL "
            "GROUP BY bucket ORDER BY bucket"
        )

    # ---- Market state / price series per symbol -------------------
    if _has_columns("market_states", "created_at", "symbol", "last_close"):
        datasets["price_series"] = _rows(
            "SELECT created_at, symbol, last_close, atr, spread "
            "FROM market_states "
            "WHERE last_close IS NOT NULL "
            "ORDER BY id ASC LIMIT 5000"
        )

    return {
        "available": True,
        "datasets": datasets,
        "detected_fields": detect_quant_fields(),
        "notes": notes,
    }
