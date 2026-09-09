"""
Persistence layer for the AI Hedge Fund Bot.

Usage:

    from database import repository as repo, initialize_database

    initialize_database()
    repo.insert_equity_snapshot({"equity": 10_000.0})
"""

from .connection import (
    close_connection,
    get_connection,
    get_db_path,
    initialize_database,
)

from . import analytics, repo_ledger, repo_meta, repository

__all__ = [
    "analytics",
    "repo_ledger",
    "repo_meta",
    "repository",
    "initialize_database",
    "get_connection",
    "get_db_path",
    "close_connection",
]
