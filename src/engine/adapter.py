"""
PokerKit adapter layer.

Thin mapping between our protocol and PokerKit's API.
"""

from dataclasses import dataclass
from typing import Optional

from pokerkit import NoLimitTexasHoldem, Automation, State

from .config import TableConfig
from ..models import ClientAction, Chips


@dataclass
class AllowedActions:
    """
    What actions a player can take.

    Used to generate ACTION_REQUEST messages.
    """
    can_fold: bool
    can_check: bool
    can_call: bool
    call_amount: Chips
    can_raise: bool
    min_raise: Chips
    max_raise: Chips

    def to_client_actions(self) -> list[ClientAction]:
        """Convert to list of ClientAction enum values."""
        actions = []
        if self.can_fold:
            actions.append(ClientAction.FOLD)
        if self.can_check:
            actions.append(ClientAction.CHECK)
        if self.can_call:
            actions.append(ClientAction.CALL)
        if self.can_raise:
            if self.call_amount.amount == 0:
                actions.append(ClientAction.BET)
            else:
                actions.append(ClientAction.RAISE_TO)
        return actions


class PokerKitAdapter:
    """
    Thin adapter between our protocol and PokerKit.

    Responsibilities:
    - Create PokerKit state with correct automations
    - Map ClientAction → PokerKit method calls
    - Extract state into our schema
    - Never mutate state directly (only via public methods)
    """

    # Automations to enable for hands
    # These handle all the bookkeeping so we only deal with player actions
    AUTOMATIONS = (
        Automation.ANTE_POSTING,
        Automation.BET_COLLECTION,
        Automation.BLIND_OR_STRADDLE_POSTING,
        Automation.CARD_BURNING,
        Automation.HOLE_DEALING,
        Automation.BOARD_DEALING,
        Automation.HOLE_CARDS_SHOWING_OR_MUCKING,
        Automation.HAND_KILLING,
        Automation.CHIPS_PUSHING,
        Automation.CHIPS_PULLING,
    )

    def __init__(self, config: TableConfig):
        self._config = config
        self._state: Optional[State] = None

    def start_hand(self, stacks: list[int]) -> None:
        """
        Create new PokerKit state for a hand.

        Args:
            stacks: Starting stack for each player (in cents)
        """
        if len(stacks) < 2:
            raise ValueError("Need at least 2 players to start a hand")

        self._state = NoLimitTexasHoldem.create_state(
            automations=self.AUTOMATIONS,
            ante_trimming_status=True,
            raw_antes=0,
            raw_blinds_or_straddles=(
                self._config.small_blind_cents,
                self._config.big_blind_cents,
            ),
            min_bet=self._config.big_blind_cents,
            raw_starting_stacks=tuple(stacks),
            player_count=len(stacks),
        )

    @property
    def is_complete(self) -> bool:
        """True if hand is over."""
        return self._state is None or not self._state.status

    @property
    def actor_index(self) -> Optional[int]:
        """Index of player to act, None if hand complete."""
        if self._state is None:
            return None
        return self._state.actor_index

    @property
    def call_amount(self) -> Optional[int]:
        """Amount to call, None if can check or no active hand."""
        if self._state is None:
            return None
        return self._state.checking_or_calling_amount

    @property
    def min_raise_to(self) -> Optional[int]:
        """Minimum raise-to amount."""
        if self._state is None:
            return None
        return self._state.min_completion_betting_or_raising_to_amount

    @property
    def max_raise_to(self) -> Optional[int]:
        """Maximum raise-to amount (player's stack)."""
        if self._state is None:
            return None
        return self._state.max_completion_betting_or_raising_to_amount

    def can_fold(self) -> bool:
        """True if current actor can fold."""
        return self._state is not None and self._state.can_fold()

    def can_check_or_call(self) -> bool:
        """True if current actor can check or call."""
        return self._state is not None and self._state.can_check_or_call()

    def can_raise_to(self, amount: Optional[int] = None) -> bool:
        """True if current actor can raise (to specified amount if given)."""
        return self._state is not None and self._state.can_complete_bet_or_raise_to(amount)

    def get_allowed_actions(self) -> AllowedActions:
        """Get what actions the current actor can take."""
        call_amt = self.call_amount or 0
        return AllowedActions(
            can_fold=self.can_fold(),
            can_check=call_amt == 0 and self.can_check_or_call(),
            can_call=call_amt > 0 and self.can_check_or_call(),
            call_amount=Chips(amount=call_amt),
            can_raise=self.can_raise_to(),
            min_raise=Chips(amount=self.min_raise_to or 0),
            max_raise=Chips(amount=self.max_raise_to or 0),
        )

    def apply_action(self, action: ClientAction, amount: Optional[int] = None) -> None:
        """
        Execute an action on the state.

        Args:
            action: The action to take
            amount: Required for BET/RAISE_TO, ignored otherwise
        """
        if self._state is None:
            raise ValueError("No active hand")

        if action == ClientAction.FOLD:
            if not self.can_fold():
                raise ValueError("Cannot fold")
            self._state.fold()
        elif action == ClientAction.CHECK:
            if not self.can_check_or_call() or (self.call_amount or 0) > 0:
                raise ValueError("Cannot check")
            self._state.check_or_call()
        elif action == ClientAction.CALL:
            if not self.can_check_or_call() or (self.call_amount or 0) == 0:
                raise ValueError("Cannot call")
            self._state.check_or_call()
        elif action == ClientAction.BET or action == ClientAction.RAISE_TO:
            if not self.can_raise_to(amount):
                raise ValueError(f"Cannot raise to {amount}")
            self._state.complete_bet_or_raise_to(amount)
        else:
            raise ValueError(f"Unknown action: {action}")

    def get_stacks(self) -> list[int]:
        """Current stack for each player."""
        if self._state is None:
            return []
        return list(self._state.stacks)

    def get_bets(self) -> list[int]:
        """Current bet for each player this street."""
        if self._state is None:
            return []
        return list(self._state.bets)

    def get_pot_amount(self) -> int:
        """Total pot including outstanding bets."""
        if self._state is None:
            return 0
        return self._state.total_pot_amount

    def get_board_cards(self) -> list[tuple[str, str]]:
        """
        Community cards as (rank, suit) tuples.

        Returns:
            List of (rank, suit) like [('A', 'h'), ('K', 's'), ...]
        """
        if self._state is None:
            return []
        cards = []
        for card in self._state.get_board_cards(0):
            cards.append((str(card.rank), str(card.suit)))
        return cards

    def get_hole_cards(self, player_index: int) -> list[tuple[str, str]]:
        """
        Player's hole cards as (rank, suit) tuples.

        Returns:
            List of (rank, suit) like [('A', 'h'), ('K', 's')]
        """
        if self._state is None:
            return []
        cards = []
        for card in self._state.hole_cards[player_index]:
            cards.append((str(card.rank), str(card.suit)))
        return cards

    def get_payoffs(self) -> list[int]:
        """Net profit/loss for each player after hand completes."""
        if self._state is None:
            return []
        return list(self._state.payoffs)

    def get_street_name(self) -> Optional[str]:
        """Current street name (preflop, flop, turn, river)."""
        if self._state is None or self._state.street is None:
            return None
        board_len = len(self.get_board_cards())
        if board_len == 0:
            return "preflop"
        elif board_len == 3:
            return "flop"
        elif board_len == 4:
            return "turn"
        elif board_len == 5:
            return "river"
        return None

    def has_folded(self, player_index: int) -> bool:
        """Check if a player has folded."""
        if self._state is None:
            return True
        # PokerKit tracks folded players via statuses
        return not self._state.statuses[player_index]

    def is_all_in_runout(self) -> bool:
        """Check if all remaining players are all-in (no more betting possible).

        Only returns True when:
        1. No one is left to act (betting round complete), AND
        2. At most 1 player has chips remaining (no future betting possible)
        """
        if self._state is None:
            return False
        # If someone still needs to act, it's not a runout yet
        if self._state.actor_index is not None:
            return False
        # Count active players (not folded) with chips remaining
        active_with_chips = 0
        for i, (status, stack) in enumerate(zip(self._state.statuses, self._state.stacks)):
            if status and stack > 0:  # Active and has chips
                active_with_chips += 1
        # All-in runout if 0 or 1 players have chips (no more betting possible)
        return active_with_chips <= 1
