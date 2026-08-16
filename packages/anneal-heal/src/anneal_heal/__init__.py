"""anneal-heal — the WARM lane (D28). Ladder-first heal from captured fallbacks; an
LLM-over-DOM re-observation only when the ladder is fully dead. Implements ports.Healer."""
from .healer import HealOutcome, LadderHealer

__all__ = ["LadderHealer", "HealOutcome"]
