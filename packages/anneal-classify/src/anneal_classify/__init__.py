"""anneal-classify — §7 routing (failure-signal-first, A12) + the §9 incident state machine
(attach/budget/heal-close/behavior-propagate/rollback/oscillation, D27/D37)."""
from .router import Lane, route

__all__ = ["route", "Lane"]
