"""Schema for AI poker insight requests and responses."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class InsightRequest:
    """Standardized input for generating poker insights."""

    # Situation
    board: str  # "Ah Kd 7c"
    hero_hand: str  # "Qs Js"
    hero_position: str  # "BTN"
    villain_position: str  # "BB"
    street: str  # "flop", "turn", "river"
    pot_size_bb: float
    effective_stack_bb: float

    # Action context
    action_history: list[str]  # ["BTN raises 2.5bb", "BB calls"]
    hero_action_taken: str | None  # "bet 4.5bb" - None if asking what to do

    # Solver data
    optimal_action: str  # "check"
    action_frequencies: dict[str, float]  # {"check": 0.65, "bet_33": 0.35}
    ev_by_action: dict[str, float]  # {"check": 2.1, "bet_33": 1.8}
    hero_hand_equity: float | None  # 0.58 - optional

    # Range context (summarized, not full 1326)
    range_summary: dict[str, float] | None = None  # {"hero_has_top_pair_plus": 0.15}

    # Metadata
    hand_category: str = ""  # "top_pair", "flush_draw", etc.
    board_texture: str = ""  # "wet", "dry", "paired"


@dataclass
class InsightResponse:
    """Output from the insight generator."""

    insight: str  # The 2-3 sentence insight
    model_used: str  # e.g., "claude-3-5-sonnet-20241022"
    prompt_tokens: int = 0
    completion_tokens: int = 0

    # Optional structured data extracted from insight
    key_concepts: list[str] = field(default_factory=list)  # ["blocker", "position"]
