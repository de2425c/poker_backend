"""Generate poker insights using Claude."""

from __future__ import annotations

import os

from .schema import InsightRequest, InsightResponse


RANK_NAMES = {
    'A': 'Ace', 'K': 'King', 'Q': 'Queen', 'J': 'Jack', 'T': 'Ten',
    '9': 'Nine', '8': 'Eight', '7': 'Seven', '6': 'Six', '5': 'Five',
    '4': 'Four', '3': 'Three', '2': 'Two'
}
RANK_VALUES = {
    'A': 14, 'K': 13, 'Q': 12, 'J': 11, 'T': 10,
    '9': 9, '8': 8, '7': 7, '6': 6, '5': 5, '4': 4, '3': 3, '2': 2
}
SUIT_NAMES = {'h': 'hearts', 'd': 'diamonds', 'c': 'clubs', 's': 'spades'}


def describe_hand(hero_hand: str, board: str) -> str:
    """
    Describe hero's hand in plain English with board interaction.

    Args:
        hero_hand: e.g., "AsAd"
        board: e.g., "9d8d5s2d"

    Returns:
        Description like "Ace of spades, Ace of diamonds (pocket aces).
        With the board, hero has: overpair to the board, nut flush draw (4 diamonds)."
    """
    # Parse hero cards
    cards = []
    for i in range(0, len(hero_hand), 2):
        rank = hero_hand[i]
        suit = hero_hand[i + 1]
        cards.append((rank, suit))

    # Parse board cards
    board_cards = []
    board_clean = board.replace(" ", "").replace("-", "")
    for i in range(0, len(board_clean), 2):
        rank = board_clean[i]
        suit = board_clean[i + 1]
        board_cards.append((rank, suit))

    # Describe hole cards
    c1_name = f"{RANK_NAMES.get(cards[0][0], cards[0][0])} of {SUIT_NAMES.get(cards[0][1], cards[0][1])}"
    c2_name = f"{RANK_NAMES.get(cards[1][0], cards[1][0])} of {SUIT_NAMES.get(cards[1][1], cards[1][1])}"

    if cards[0][0] == cards[1][0]:
        hole_desc = f"{c1_name}, {c2_name} (pocket {RANK_NAMES.get(cards[0][0], cards[0][0]).lower()}s)"
    elif cards[0][1] == cards[1][1]:
        hole_desc = f"{c1_name}, {c2_name} (suited)"
    else:
        hole_desc = f"{c1_name}, {c2_name}"

    # Count suits for flush/flush draw detection
    all_cards = cards + board_cards
    suit_counts = {}
    for _, suit in all_cards:
        suit_counts[suit] = suit_counts.get(suit, 0) + 1

    hero_suits = [s for _, s in cards]

    # Build board interaction description
    interactions = []

    # Check for flush/flush draw
    for suit, count in suit_counts.items():
        if count >= 5 and suit in hero_suits:
            interactions.append(f"MADE FLUSH in {SUIT_NAMES[suit]}")
        elif count == 4 and suit in hero_suits:
            interactions.append(f"FLUSH DRAW (4 {SUIT_NAMES[suit]}, need 1 more for flush)")

    # Check for pairs with board
    board_ranks = [r for r, _ in board_cards]
    hero_ranks = [r for r, _ in cards]

    if cards[0][0] == cards[1][0]:  # Pocket pair
        pair_rank = cards[0][0]
        pair_value = RANK_VALUES.get(pair_rank, 0)
        max_board_value = max(RANK_VALUES.get(r, 0) for r in board_ranks) if board_ranks else 0

        if pair_rank in board_ranks:
            interactions.append("set")
        elif pair_value > max_board_value:
            interactions.append("overpair")
        else:
            interactions.append("underpair")
    else:
        for hr in hero_ranks:
            if hr in board_ranks:
                interactions.append(f"pair of {RANK_NAMES.get(hr, hr).lower()}s")
                break

    if interactions:
        return f"{hole_desc}. Board interaction: {', '.join(interactions)}."
    else:
        return f"{hole_desc}."


SYSTEM_PROMPT = """You are a poker coach. Read the "Hero hand description" field carefully - it tells you EXACTLY what hero has.

CRITICAL: A flush requires 5 cards of the same suit. If it says "FLUSH DRAW" that means hero does NOT have a flush yet.

Output ONLY one educational insight (1-2 sentences) explaining why the solver recommends this action. No preamble.

Good insights:
- "With the nut flush draw and an overpair, jamming applies maximum pressure and denies villain's equity."
- "Your As blocks the nut flush draws villain would fold, making this a poor bluff candidate."
- "On paired boards, overpairs should check more because villain's raising range is full-house heavy."

Avoid:
- EV numbers (beginners don't understand them)
- Saying hero has a flush when description says "FLUSH DRAW"
- Generic advice that doesn't reference this specific hand"""


def build_user_prompt(request: InsightRequest) -> str:
    """Build the user prompt from an InsightRequest."""
    lines = []

    # Situation
    lines.append(f"Board: {request.board}")
    lines.append(f"Hero hand: {request.hero_hand}")
    lines.append(f"Hero hand description: {describe_hand(request.hero_hand, request.board)}")
    lines.append(f"Hero position: {request.hero_position}")
    lines.append(f"Villain position: {request.villain_position}")
    lines.append(f"Street: {request.street}")
    lines.append(f"Pot: {request.pot_size_bb:.1f}bb")
    lines.append(f"Effective stack: {request.effective_stack_bb:.1f}bb")
    lines.append("")

    # Action history
    if request.action_history:
        lines.append("Action history:")
        for action in request.action_history:
            lines.append(f"  - {action}")
        lines.append("")

    # What hero did (if applicable)
    if request.hero_action_taken:
        lines.append(f"Hero chose: {request.hero_action_taken}")
        lines.append("")

    # Solver data
    lines.append(f"Solver optimal action: {request.optimal_action}")
    lines.append("")

    lines.append("Action frequencies:")
    for action, freq in sorted(request.action_frequencies.items(), key=lambda x: -x[1]):
        lines.append(f"  {action}: {freq*100:.0f}%")
    lines.append("")

    lines.append("EV by action (in bb):")
    for action, ev in sorted(request.ev_by_action.items(), key=lambda x: -x[1]):
        lines.append(f"  {action}: {ev:+.2f}")
    lines.append("")

    # Optional context
    if request.hand_category:
        lines.append(f"Hand category: {request.hand_category}")
    if request.board_texture:
        lines.append(f"Board texture: {request.board_texture}")
    if request.hero_hand_equity is not None:
        lines.append(f"Hero equity: {request.hero_hand_equity*100:.0f}%")

    if request.range_summary:
        lines.append("")
        lines.append("Range context:")
        for key, val in request.range_summary.items():
            lines.append(f"  {key}: {val*100:.0f}%")

    return "\n".join(lines)


class InsightGenerator:
    """Generate poker insights using Claude API."""

    def __init__(self, api_key: str | None = None, model: str = "claude-sonnet-4-20250514"):
        """
        Initialize the insight generator.

        Args:
            api_key: Anthropic API key. If not provided, reads from ANTHROPIC_API_KEY env var.
            model: Model to use for generation.
        """
        # Import here to allow module-level functions (build_user_prompt, SYSTEM_PROMPT)
        # to be used without requiring anthropic to be installed
        from anthropic import Anthropic

        self.api_key = api_key or os.getenv("ANTHROPIC_API_KEY")
        if not self.api_key:
            raise ValueError("ANTHROPIC_API_KEY not set")

        self.client = Anthropic(api_key=self.api_key)
        self.model = model

    def generate(self, request: InsightRequest) -> InsightResponse:
        """
        Generate an insight for the given request.

        Args:
            request: InsightRequest with all the poker situation data.

        Returns:
            InsightResponse with the generated insight.
        """
        user_prompt = build_user_prompt(request)

        message = self.client.messages.create(
            model=self.model,
            max_tokens=150,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}],
        )

        insight_text = message.content[0].text if message.content else ""

        return InsightResponse(
            insight=insight_text,
            model_used=self.model,
            prompt_tokens=message.usage.input_tokens,
            completion_tokens=message.usage.output_tokens,
        )

    def generate_batch(self, requests: list[InsightRequest]) -> list[InsightResponse]:
        """
        Generate insights for multiple requests.

        Args:
            requests: List of InsightRequests.

        Returns:
            List of InsightResponses in the same order.
        """
        return [self.generate(req) for req in requests]
