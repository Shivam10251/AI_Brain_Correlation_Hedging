"""
Trading engine.

    runtime     shared state, MT5 connection, single-writer lock, recovery
    engine      the per-cycle pipeline and risk gate
    reconciler  database <-> MT5 reconciliation
"""
