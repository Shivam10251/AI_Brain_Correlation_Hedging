"""
AI Hedge Fund Bot - backend.

    config      settings, loaded from .env
    core/       the trading engine: cycle, runtime state, reconciliation
    ai/         DeepSeek analysis, prompts, experience memory
    market/     MT5 data, order execution, news extension point
    database/   SQLite persistence and analytics

Entry point: backend.main:app
"""
