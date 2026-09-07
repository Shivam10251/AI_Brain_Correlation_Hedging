"""
Shared SQLite helpers used by the repository modules.
"""

import json
from datetime import datetime, timezone

from .connection import get_connection, write_lock


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------

def utc_now():
    """Current UTC time as an ISO-8601 string (sorts lexicographically)."""

    return datetime.now(timezone.utc).isoformat()


def _json(value):
    """Serialise to JSON, tolerating non-serialisable values."""

    if value is None:
        return None

    try:
        return json.dumps(value, default=str)
    except (TypeError, ValueError):
        return json.dumps({"unserialisable": str(value)})


def _rows(cursor):
    return [dict(row) for row in cursor.fetchall()]


def _insert(table, data):
    """Generic INSERT returning the new row id."""

    columns = list(data.keys())

    sql = (
        f"INSERT INTO {table} ({', '.join(columns)}) "
        f"VALUES ({', '.join('?' for _ in columns)})"
    )

    conn = get_connection()

    with write_lock:
        cursor = conn.execute(sql, [data[c] for c in columns])
        return cursor.lastrowid


def _update(table, row_id, data):
    """Generic UPDATE by primary key. Ignores None-only payloads."""

    fields = {k: v for k, v in data.items() if v is not None}

    if not fields:
        return 0

    assignments = ", ".join(f"{k} = ?" for k in fields)

    sql = f"UPDATE {table} SET {assignments} WHERE id = ?"

    conn = get_connection()

    with write_lock:
        cursor = conn.execute(sql, list(fields.values()) + [row_id])
        return cursor.rowcount
