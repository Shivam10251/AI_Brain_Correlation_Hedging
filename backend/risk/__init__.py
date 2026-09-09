"""
Risk layer.

Phase 2 adds `money`: converting a stop distance into a currency figure
using the broker's own tick specification.

Phase 3 adds `engine`: the module with sole authority over whether an
order may be sent.
"""

from . import money

__all__ = ["money"]
