"""Independent switches for method ablations."""

from dataclasses import dataclass


@dataclass(frozen=True)
class MethodOptions:
    candidate_count: int = 5
    relevance: bool = True
    diversity: bool = True
    effect_feedback: bool = True
    verification_rounds: int = 2
