"""
Export the trading database to a self-contained HTML explorer.

Reads every table out of the SQLite file, embeds the rows into
scripts/db_explorer_template.html, and writes a single HTML file you can
open in a browser. Nothing is fetched at view time - the page carries a
frozen copy of the rows, so re-run this whenever you want fresh data.

The database is opened READ-ONLY, so this is safe to run while the bot
is trading (WAL mode already allows concurrent readers).

Usage
-----
    .venv/Scripts/python.exe scripts/export_db.py
    .venv/Scripts/python.exe scripts/export_db.py --open
    .venv/Scripts/python.exe scripts/export_db.py --out reports/db.html
    .venv/Scripts/python.exe scripts/export_db.py --fragment

--fragment writes the page WITHOUT the <!doctype>/<head> wrapper, which
is the form Claude needs to publish it as an Artifact. The default
(standalone) form is the one that opens correctly on your own machine.
"""

import argparse
import datetime
import json
import os
import sqlite3
import sys
import webbrowser
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent

TEMPLATE_PATH = PROJECT_ROOT / "scripts" / "db_explorer_template.html"

DEFAULT_OUT = PROJECT_ROOT / "data" / "db_explorer.html"

PLACEHOLDER = "__DATA__"


# ---------------------------------------------------------------------
# Locate the database
# ---------------------------------------------------------------------

def resolve_db_path(override=None):
    """
    Use the same resolution the app uses, so DB_PATH in .env is honoured.

    Falls back to data/quantbot.db if the backend package cannot be
    imported (for example when this runs outside the virtualenv).
    """

    if override:
        return Path(override).expanduser().resolve()

    sys.path.insert(0, str(PROJECT_ROOT))

    try:
        from backend.database.connection import get_db_path

        return Path(get_db_path())

    except Exception:                               # noqa: BLE001
        return PROJECT_ROOT / "data" / "quantbot.db"


# ---------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------

def read_database(db_path):
    """Pull every user table, its rows and its column types."""

    uri = "file:{}?mode=ro".format(db_path.as_posix())

    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row

    try:
        names = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%' "
                "ORDER BY name"
            )
        ]

        tables = {}
        schema = {}

        for name in names:
            cursor = connection.execute('SELECT * FROM "{}"'.format(name))

            tables[name] = [dict(row) for row in cursor.fetchall()]

            schema[name] = [
                {
                    "name": row[1],
                    "type": row[2],
                    "notnull": row[3],
                    "pk": row[5],
                }
                for row in connection.execute(
                    'PRAGMA table_info("{}")'.format(name)
                )
            ]

        journal = connection.execute("PRAGMA journal_mode").fetchone()[0]

    finally:
        connection.close()

    meta = {
        "path": os.path.relpath(db_path, PROJECT_ROOT).replace("\\", "/"),
        "size": db_path.stat().st_size,
        "mtime": datetime.datetime.now().isoformat(timespec="seconds"),
        "journal": journal,
        "sqlite": sqlite3.sqlite_version,
    }

    return {"tables": tables, "schema": schema, "meta": meta}


# ---------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------

STANDALONE_HEAD = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  :root{color-scheme:light dark}
  body{margin:0;font:14px system-ui,-apple-system,Segoe UI,sans-serif}
  img{max-width:100%}
  [hidden]{display:none!important}
</style>
"""


def render(payload, fragment=False):
    """Inject the data into the template."""

    if not TEMPLATE_PATH.exists():
        raise SystemExit("Template missing: {}".format(TEMPLATE_PATH))

    template = TEMPLATE_PATH.read_text(encoding="utf-8")

    if PLACEHOLDER not in template:
        raise SystemExit(
            "Template has no {} placeholder".format(PLACEHOLDER)
        )

    blob = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    # A literal </script> inside the JSON would close the tag early.
    # "\\/" is a valid JSON escape for "/", so this survives JSON.parse.
    blob = blob.replace("</", "<\\/")

    page = template.replace(PLACEHOLDER, blob)

    if fragment:
        return page

    return STANDALONE_HEAD + "<body>\n" + page + "\n</body>\n</html>\n"


# ---------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Export the trading database to an HTML explorer."
    )

    parser.add_argument(
        "--db",
        help="database file (default: the same one the app uses)",
    )
    parser.add_argument(
        "--out",
        default=str(DEFAULT_OUT),
        help="output HTML file (default: data/db_explorer.html)",
    )
    parser.add_argument(
        "--fragment",
        action="store_true",
        help="omit the <!doctype>/<head> wrapper, for publishing as an "
             "Artifact instead of opening locally",
    )
    parser.add_argument(
        "--open",
        action="store_true",
        dest="open_after",
        help="open the file in your browser when done",
    )

    args = parser.parse_args()

    db_path = resolve_db_path(args.db)

    if not db_path.exists():
        raise SystemExit(
            "Database not found: {}\n"
            "Start the backend once to create it.".format(db_path)
        )

    payload = read_database(db_path)

    out_path = Path(args.out).expanduser()

    if not out_path.is_absolute():
        out_path = PROJECT_ROOT / out_path

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(render(payload, args.fragment), encoding="utf-8")

    counts = {
        name: len(rows)
        for name, rows in payload["tables"].items()
        if rows
    }

    total = sum(len(rows) for rows in payload["tables"].values())

    print("Read    {}".format(db_path))
    print("Wrote   {}  ({:.1f} KB)".format(
        out_path, out_path.stat().st_size / 1024
    ))
    print("Rows    {} across {} tables".format(
        total, len(payload["tables"])
    ))

    for name in sorted(counts):
        print("          {:<18} {}".format(name, counts[name]))

    empty = [n for n, r in payload["tables"].items() if not r]

    if empty:
        print("          (empty: {})".format(", ".join(sorted(empty))))

    if args.open_after:
        webbrowser.open(out_path.as_uri())


if __name__ == "__main__":
    main()
