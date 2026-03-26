import random
"""
Poker table engine.

Manages table lifecycle and produces protocol messages.
"""

from dataclasses import dataclass
from typing import Optional

from .config import TableConfig
from .adapter import PokerKitAdapter, AllowedActions
from ..models import (
    ClientAction,
    ServerAction,
    Street,
    TableStatus,
    SeatStatus,
    Chips,
    Card,
    PlayerIdentity,
    Seat,
    Pot,
    HandState,
    ActionEvent,
    StreetDealtEvent,
    HandStartedEvent,
    HandEndedEvent,
    PotWinner,
    ShowdownHand,
    TableSnapshotMessage,
    ActionRequestMessage,
    generate_hand_id,
    generate_table_id,
)


@dataclass
class SeatState:
    """Internal state for a seat at the table."""
    player: PlayerIdentity
    chips: int  # Current stack in cents
    is_connected: bool = True
    pending_topup: int = 0  # Amount to add at next hand start
    auto_topup_enabled: bool = True  # Whether to auto top-up when below $200
    is_sitting_out: bool = False  # Track sitting out status


class PokerTableEngine:
    """
    Main poker table engine.

    Manages:
    - Table lifecycle (WAITING → RUNNING → BETWEEN_HANDS)
    - Player seats and stacks
    - Hand execution via PokerKitAdapter
    - Snapshot and event generation
    """

    def __init__(self, table_id: Optional[str] = None, config: Optional[TableConfig] = None):
        self._table_id = table_id or generate_table_id()
        self._config = config or TableConfig()
        self._seats: list[Optional[SeatState]] = [None] * self._config.max_players
        self._adapter = PokerKitAdapter(self._config)
        self._status = TableStatus.WAITING
        self._seq = 0
        self._hand_id: Optional[str] = None
        self._button_seat = 0
        # Maps PokerKit player index → table seat index
        self._active_seat_indices: list[int] = []
        # Cache hole cards at deal time (PokerKit discards on fold)
        self._dealt_hole_cards: dict[int, list[tuple[str, str]]] = {}  # pk_idx -> [(rank, suit)]
        # Track top-ups applied at hand start (for sending REBUY messages)
        self._applied_topups: list[tuple[int, int, int]] = []  # [(seat_idx, amount, new_stack), ...]
        # Track last completed hand for stale action detection
        self._last_completed_hand_id: Optional[str] = None
        # Track players who folded during current hand (pk_idx)
        self._folded_players: set[int] = set()

    @property
    def table_id(self) -> str:
        return self._table_id

    @property
    def status(self) -> TableStatus:
        return self._status

    @property
    def dealt_hole_cards_by_seat(self) -> dict[int, list[str]]:
        """Return hole cards mapped by seat index as card strings (e.g. ["Ah", "Ks"])."""
        result: dict[int, list[str]] = {}
        for pk_idx, cards in self._dealt_hole_cards.items():
            if pk_idx < len(self._active_seat_indices):
                seat = self._active_seat_indices[pk_idx]
                result[seat] = [f"{r}{s}" for r, s in cards]
        return result

    def get_and_clear_applied_topups(self) -> list[tuple[int, int, int]]:
        """Get and clear any pending top-ups that were applied at hand start.

        Returns list of (seat_idx, topup_amount, new_stack) tuples.
        """
        topups = self._applied_topups.copy()
        self._applied_topups = []
        return topups

    # -------------------------------------------------------------------------
    # Player Management
    # -------------------------------------------------------------------------

    def seat_player(self, seat: int, player: PlayerIdentity, chips: Chips) -> None:
        """
        Seat a player at the table.

        Args:
            seat: Seat index (0 to max_players-1)
            player: Player identity
            chips: Buy-in amount
        """
        if seat < 0 or seat >= self._config.max_players:
            raise ValueError(f"Invalid seat index: {seat}")
        if self._seats[seat] is not None:
            raise ValueError(f"Seat {seat} is occupied")
        if chips.amount < self._config.min_buy_in_cents:
            raise ValueError(f"Buy-in too small: {chips.amount} < {self._config.min_buy_in_cents}")
        if chips.amount > self._config.max_buy_in_cents:
            raise ValueError(f"Buy-in too large: {chips.amount} > {self._config.max_buy_in_cents}")

        self._seats[seat] = SeatState(player=player, chips=chips.amount)

    def unseat_player(self, seat: int) -> Chips:
        """
        Remove a player from the table.

        Returns:
            Player's final chip count
        """
        if seat < 0 or seat >= self._config.max_players:
            raise ValueError(f"Invalid seat index: {seat}")
        if self._seats[seat] is None:
            raise ValueError(f"Seat {seat} is empty")

        state = self._seats[seat]
        self._seats[seat] = None
        return Chips(amount=state.chips)

    def _get_active_seats(self) -> list[int]:
        """Get indices of seats with players."""
        return [i for i, s in enumerate(self._seats) if s is not None]

    # -------------------------------------------------------------------------
    # Hand Lifecycle
    # -------------------------------------------------------------------------

    def can_start_hand(self) -> bool:
        """True if we have enough players to start a hand."""
        return len(self._get_active_seats()) >= self._config.min_players_to_start

    def start_hand(self) -> list:
        """
        Start a new hand.

        Returns:
            List of events (HandStartedEvent, blind ActionEvents)
        """
        if not self.can_start_hand():
            raise ValueError("Not enough players to start hand")

        if self._status == TableStatus.RUNNING:
            raise ValueError("Hand already in progress")

        # Reset bot stacks to 100BB only if below threshold (allows accumulation)
        for seat in self._seats:
            if seat is not None and seat.player.user_id.startswith(("bot_", "user_bot_")):
                if seat.chips < 20000:
                    seat.chips = 20000

        # Apply pending top-ups (for manual top-up when auto top-up is disabled)
        # Track applied top-ups so handler can send REBUY messages
        self._applied_topups: list[tuple[int, int, int]] = []  # [(seat_idx, amount, new_stack), ...]
        for idx, seat in enumerate(self._seats):
            if seat is not None and seat.pending_topup > 0:
                topup_amount = seat.pending_topup
                seat.chips += topup_amount
                seat.pending_topup = 0
                self._applied_topups.append((idx, topup_amount, seat.chips))
                print(f"[TOPUP] Applied pending top-up: +{topup_amount} cents for {seat.player.user_id}")

        # Get active seats in numerical order first
        active_seats = self._get_active_seats()

        # Rotate button (if not first hand)
        if self._hand_id is not None:
            # Find next active seat after current button
            current_idx = active_seats.index(self._button_seat) if self._button_seat in active_seats else -1
            next_idx = (current_idx + 1) % len(active_seats)
            self._button_seat = active_seats[next_idx]
        else:
            # First hand: randomize button position
            self._button_seat = random.choice(active_seats)

        # Reorder active seats so SB (seat after button) is index 0
        # This maps PokerKit's player indices correctly:
        # - Player 0 = Small Blind (1 after button)
        # - Player 1 = Big Blind (2 after button)
        # - Player 2+ = Other positions
        button_idx = active_seats.index(self._button_seat)
        sb_idx = (button_idx + 1) % len(active_seats)
        self._active_seat_indices = active_seats[sb_idx:] + active_seats[:sb_idx]

        stacks = [self._seats[i].chips for i in self._active_seat_indices]

        # Generate hand ID and start
        self._hand_id = generate_hand_id()
        self._status = TableStatus.RUNNING
        self._adapter.start_hand(stacks)

        # Cache hole cards now (before any folds clear them in PokerKit)
        self._dealt_hole_cards = {}
        self._folded_players = set()  # Reset folded players tracking
        for pk_idx in range(len(self._active_seat_indices)):
            cards = self._adapter.get_hole_cards(pk_idx)
            if cards:
                self._dealt_hole_cards[pk_idx] = cards

        # Generate events
        events = []

        # Hand started event
        events.append(HandStartedEvent(
            hand_id=self._hand_id,
            button_seat=self._button_seat,
        ))

        # Blind posting events
        # In PokerKit with automations, blinds are already posted
        # Generate events for SB and BB
        if len(self._active_seat_indices) >= 2:
            sb_seat = self._active_seat_indices[0]  # First active seat posts SB
            bb_seat = self._active_seat_indices[1]  # Second active seat posts BB

            events.append(ActionEvent(
                seat=sb_seat,
                action=ServerAction.POST_BLIND,
                amount=Chips(amount=self._config.small_blind_cents),
                is_all_in=self._seats[sb_seat].chips <= self._config.small_blind_cents,
            ))
            events.append(ActionEvent(
                seat=bb_seat,
                action=ServerAction.POST_BLIND,
                amount=Chips(amount=self._config.big_blind_cents),
                is_all_in=self._seats[bb_seat].chips <= self._config.big_blind_cents,
            ))

        self._seq += 1
        return events

    def get_actor_seat(self) -> Optional[int]:
        """Get the table seat index of the current actor, or None if no action needed."""
        pk_actor = self._adapter.actor_index
        if pk_actor is None:
            return None
        return self._active_seat_indices[pk_actor]

    def get_allowed_actions(self, seat: int) -> AllowedActions:
        """Get what actions a seat can take."""
        if seat not in self._active_seat_indices:
            raise ValueError(f"Seat {seat} is not in the current hand")

        actor_seat = self.get_actor_seat()
        if actor_seat != seat:
            raise ValueError(f"It's not seat {seat}'s turn (actor is {actor_seat})")

        return self._adapter.get_allowed_actions()

    def apply_action(self, seat: int, action: ClientAction, amount: Optional[Chips] = None) -> list:
        """
        Apply a player action.

        Args:
            seat: Table seat index
            action: The action to take
            amount: Required for BET/RAISE_TO

        Returns:
            List of events generated
        """
        if self._status != TableStatus.RUNNING:
            raise ValueError("No hand in progress")

        actor_seat = self.get_actor_seat()
        if actor_seat != seat:
            raise ValueError(f"It's not seat {seat}'s turn (actor is {actor_seat})")

        # Capture state before action
        old_board_len = len(self._adapter.get_board_cards())
        pk_index = self._active_seat_indices.index(seat)
        stack_before = self._adapter.get_stacks()[pk_index]
        bet_before = self._adapter.get_bets()[pk_index] if pk_index < len(self._adapter.get_bets()) else 0

        # Apply the action
        amount_cents = amount.amount if amount else None
        self._adapter.apply_action(action, amount_cents)

        # Track folds for showdown card display
        if action == ClientAction.FOLD:
            self._folded_players.add(pk_index)

        events = []

        # Map action to ServerAction
        server_action = self._map_client_to_server_action(action)
        current_stack = self._adapter.get_stacks()[pk_index]
        is_all_in = False

        if action in (ClientAction.BET, ClientAction.RAISE_TO):
            is_all_in = current_stack == 0

        # Compute effective amount as the TOTAL bet this street (prior bet + new spend).
        # This is what the client expects for updating seat bet displays.
        # We use stack difference because PokerKit may have already advanced the
        # street and reset bets by this point.
        effective_amount = amount
        if action in (ClientAction.CALL, ClientAction.BET, ClientAction.RAISE_TO):
            spent = stack_before - current_stack
            if spent > 0:
                effective_amount = Chips(amount=bet_before + spent)
            if action == ClientAction.CALL:
                is_all_in = current_stack == 0

        # Check if this action triggers an all-in runout (reveal hole cards immediately)
        action_showdown_hands = None
        if self._adapter.is_all_in_runout():
            action_showdown_hands = self._get_showdown_hands()

        events.append(ActionEvent(
            seat=seat,
            action=server_action,
            amount=effective_amount,
            is_all_in=is_all_in,
            showdown_hands=action_showdown_hands,
        ))

        # Check for street change
        new_board_len = len(self._adapter.get_board_cards())
        if new_board_len > old_board_len:
            street = self._get_current_street()
            board_cards = self._get_board_cards_as_model()
            # Only include the new cards
            new_cards = board_cards[old_board_len:]

            # Include showdown_hands during all-in runouts
            showdown_hands = None
            if self._adapter.is_all_in_runout():
                showdown_hands = self._get_showdown_hands()

            events.append(StreetDealtEvent(
                street=street,
                cards=new_cards,
                showdown_hands=showdown_hands,
            ))

        # Check for hand complete
        if self._adapter.is_complete:
            events.extend(self._finalize_hand())

        self._seq += 1
        return events

    def _map_client_to_server_action(self, action: ClientAction) -> ServerAction:
        """Map ClientAction to ServerAction."""
        mapping = {
            ClientAction.FOLD: ServerAction.FOLD,
            ClientAction.CHECK: ServerAction.CHECK,
            ClientAction.CALL: ServerAction.CALL,
            ClientAction.BET: ServerAction.BET,
            ClientAction.RAISE_TO: ServerAction.RAISE_TO,
        }
        return mapping[action]

    def _get_current_street(self) -> Street:
        """Get current street as our Street enum."""
        name = self._adapter.get_street_name()
        mapping = {
            "preflop": Street.PREFLOP,
            "flop": Street.FLOP,
            "turn": Street.TURN,
            "river": Street.RIVER,
        }
        return mapping.get(name, Street.PREFLOP)

    def _get_board_cards_as_model(self) -> list[Card]:
        """Convert PokerKit board cards to our Card model."""
        return [Card(rank=r, suit=s) for r, s in self._adapter.get_board_cards()]

    def _get_showdown_hands(self) -> list[ShowdownHand]:
        """Get hole cards for all active (non-folded) players.

        Uses cached dealt cards since PokerKit discards on fold.
        Only includes players who haven't folded.

        Returns:
            List of ShowdownHand for each active player.
        """
        showdown_hands = []
        for pk_idx, seat_idx in enumerate(self._active_seat_indices):
            # Skip players who have folded (tracked during hand, not via PokerKit status)
            if pk_idx in self._folded_players:
                continue
            hole_cards = self._dealt_hole_cards.get(pk_idx, [])
            if hole_cards:
                cards = [Card(rank=r, suit=s) for r, s in hole_cards]
                showdown_hands.append(ShowdownHand(
                    seat=seat_idx,
                    cards=cards,
                    hand_description=None,
                ))
        return showdown_hands

    def cancel_hand(self) -> list:
        """
        Cancel current hand after user fold (blitz mode).

        Awards pot to remaining active (non-folded) players.

        Returns:
            List of events (HandEndedEvent with was_blitz flag)
        """
        if self._status != TableStatus.RUNNING:
            raise ValueError("No hand in progress")

        # Get current pot total
        pot = self._adapter.get_pot_amount()

        # Find remaining active players (not folded)
        active_seats = []
        for pk_idx, seat_idx in enumerate(self._active_seat_indices):
            if pk_idx not in self._folded_players:
                active_seats.append((pk_idx, seat_idx))

        # Award pot to remaining active players
        winners = []
        if active_seats and pot > 0:
            share = pot // len(active_seats)
            for pk_idx, seat_idx in active_seats:
                self._seats[seat_idx].chips += share
                winners.append(PotWinner(
                    seat=seat_idx,
                    amount=Chips(amount=share),
                    hand_description="Blitz fold",
                    shown_cards=None,
                ))

        # Track this hand as last completed (for stale action detection)
        self._last_completed_hand_id = self._hand_id

        # Transition to between hands
        self._status = TableStatus.BETWEEN_HANDS

        return [HandEndedEvent(
            hand_id=self._hand_id,
            winners=winners,
            showdown_hands=None,
        )]

    def is_action_stale(self, hand_id: str) -> bool:
        """Check if an action is for a hand that just ended (race condition)."""
        print(f"[STALE CHECK] hand_id={hand_id} status={self._status} last_completed={self._last_completed_hand_id} current={self._hand_id}")
        if self._status == TableStatus.RUNNING:
            return False  # Hand in progress, not stale

        # Action is stale if it matches the hand that just ended
        return hand_id == self._last_completed_hand_id

    def _finalize_hand(self) -> list:
        """
        Finalize hand and generate end events.

        Returns:
            List of events (HandEndedEvent)
        """
        events = []

        # Track this hand as last completed (for stale action detection)
        self._last_completed_hand_id = self._hand_id

        # Get payoffs
        payoffs = self._adapter.get_payoffs()

        # Determine winners
        winners = []
        for pk_idx, payoff in enumerate(payoffs):
            if payoff > 0:
                seat = self._active_seat_indices[pk_idx]
                # Get shown cards (if any)
                shown = None
                hole_cards = self._adapter.get_hole_cards(pk_idx)
                if hole_cards:
                    shown = [Card(rank=r, suit=s) for r, s in hole_cards]

                winners.append(PotWinner(
                    seat=seat,
                    amount=Chips(amount=payoff),
                    hand_description=None,  # TODO: get from PokerKit hand evaluator
                    shown_cards=shown,
                ))

        # Collect hole cards for all active players (showdown)
        showdown_hands = self._get_showdown_hands()

        events.append(HandEndedEvent(
            hand_id=self._hand_id,
            winners=winners,
            showdown_hands=showdown_hands if showdown_hands else None,
        ))

        # Update player stacks
        final_stacks = self._adapter.get_stacks()
        for pk_idx, stack in enumerate(final_stacks):
            seat = self._active_seat_indices[pk_idx]
            # payoff is the delta, add to original stack
            self._seats[seat].chips = self._seats[seat].chips + payoffs[pk_idx]

        # Transition to between hands
        self._status = TableStatus.BETWEEN_HANDS

        return events

    # -------------------------------------------------------------------------
    # Snapshots
    # -------------------------------------------------------------------------

    def get_snapshot(self, for_seat: int) -> TableSnapshotMessage:
        """
        Get full table state for a player.

        Args:
            for_seat: The seat requesting the snapshot
        """
        # Build seats
        seats = []
        for i in range(self._config.max_players):
            state = self._seats[i]
            if state is None:
                seats.append(Seat(
                    seat_index=i,
                    status=SeatStatus.EMPTY,
                    player=None,
                    chips=Chips(amount=0),
                    bet=Chips(amount=0),
                    is_button=False,
                    is_connected=True,
                ))
            else:
                # Determine seat status and current stack
                current_stack = state.chips  # Default to table-level stack

                if self._status != TableStatus.RUNNING:
                    status = SeatStatus.SEATED
                elif i in self._active_seat_indices:
                    pk_idx = self._active_seat_indices.index(i)
                    # Use PokerKit's stack during a hand (reflects blinds/bets)
                    current_stack = self._adapter.get_stacks()[pk_idx]
                    if current_stack == 0:
                        status = SeatStatus.ALL_IN
                    else:
                        status = SeatStatus.ACTIVE
                else:
                    status = SeatStatus.SEATED

                bet = Chips(amount=0)
                if self._status == TableStatus.RUNNING and i in self._active_seat_indices:
                    pk_idx = self._active_seat_indices.index(i)
                    bets = self._adapter.get_bets()
                    if pk_idx < len(bets):
                        bet = Chips(amount=bets[pk_idx])

                seats.append(Seat(
                    seat_index=i,
                    status=status,
                    player=state.player,
                    chips=Chips(amount=current_stack),
                    bet=bet,
                    is_button=(i == self._button_seat),
                    is_connected=state.is_connected,
                ))

        # Build hand state if running
        hand = None
        if self._status == TableStatus.RUNNING and self._hand_id is not None:
            pots = [Pot(
                amount=Chips(amount=self._adapter.get_pot_amount()),
                eligible_seats=self._active_seat_indices.copy(),
            )]
            hand = HandState(
                hand_id=self._hand_id,
                street=self._get_current_street(),
                board=self._get_board_cards_as_model(),
                pots=pots,
                current_bet=Chips(amount=self._adapter.call_amount or 0),
                actor_seat=self.get_actor_seat(),
            )

        # Get hole cards for requesting player
        hole_cards = None
        if self._status == TableStatus.RUNNING and for_seat in self._active_seat_indices:
            pk_idx = self._active_seat_indices.index(for_seat)
            cards = self._adapter.get_hole_cards(pk_idx)
            print(f"[SNAPSHOT] seat={for_seat} pk_idx={pk_idx} raw_cards={cards}")
            if cards:
                hole_cards = [Card(rank=r, suit=s) for r, s in cards]
                print(f"[SNAPSHOT] hole_cards for seat {for_seat}: {[(c.rank, c.suit) for c in hole_cards]}")
        else:
            print(f"[SNAPSHOT] No hole cards: status={self._status}, for_seat={for_seat}, active_seats={self._active_seat_indices}")

        return TableSnapshotMessage(
            table_id=self._table_id,
            status=self._status,
            stake_id="nlh_1_2",  # TODO: make configurable
            small_blind=Chips(amount=self._config.small_blind_cents),
            big_blind=Chips(amount=self._config.big_blind_cents),
            seats=seats,
            hand=hand,
            your_seat=for_seat,
            your_hole_cards=hole_cards,
            seq=self._seq,
        )

    def get_action_request(self, seat: int) -> ActionRequestMessage:
        """
        Get action request for the current actor.

        Args:
            seat: Must be the current actor's seat
        """
        if self._status != TableStatus.RUNNING:
            raise ValueError("No hand in progress")

        actor_seat = self.get_actor_seat()
        if actor_seat != seat:
            raise ValueError(f"It's not seat {seat}'s turn")

        allowed = self._adapter.get_allowed_actions()

        import time

        # Determine timeout: bots get shorter timeout on preflop/flop
        timeout_seconds = self._config.action_timeout_seconds
        seat_state = self._seats[seat]
        is_bot = False
        if seat_state is not None:
            user_id = seat_state.player.user_id
            is_bot = user_id.startswith(("bot_", "user_bot_"))
            street = self._get_current_street()
            if is_bot and street in (Street.PREFLOP, Street.FLOP):
                timeout_seconds = self._config.bot_early_street_timeout_seconds

        now_ms = int(time.time() * 1000)
        expires_at = now_ms + (timeout_seconds * 1000)
        print(f"[TIMER_CALC] now={now_ms} timeout={timeout_seconds}s expires_at={expires_at}", flush=True)

        # For bots: include opponent hole cards for quick fold optimization
        opponent_hole_cards = None
        if is_bot:
            opponent_hole_cards = {}
            all_hole_cards = self.dealt_hole_cards_by_seat
            for other_seat, cards in all_hole_cards.items():
                if other_seat != seat and other_seat in self._active_seat_indices:
                    # cards is list of strings like ["Ah", "Ks"]
                    opponent_hole_cards[other_seat] = [
                        Card(rank=c[0], suit=c[1].lower()) for c in cards
                    ]

        return ActionRequestMessage(
            hand_id=self._hand_id,
            request_id=f"req_{self._hand_id}_{self._seq}",
            seat=seat,
            allowed_actions=allowed.to_client_actions(),
            call_amount=allowed.call_amount if allowed.can_call else None,
            min_raise=allowed.min_raise if allowed.can_raise else None,
            max_raise=allowed.max_raise if allowed.can_raise else None,
            pot=Chips(amount=self._adapter.get_pot_amount()),
            expires_at_ms=expires_at,
            opponent_hole_cards=opponent_hole_cards,
        )

    def set_sitting_out(self, seat: int, sitting_out: bool):
        """Mark a seat as sitting out/in.

        Returns:
            SeatUpdateEvent if state changed, None otherwise.
        """
        from ..models import SeatUpdateEvent

        seat_state = self._seats[seat]
        if seat_state is None:
            return None

        if seat_state.is_sitting_out == sitting_out:
            return None  # No change

        seat_state.is_sitting_out = sitting_out

        return SeatUpdateEvent(
            seat=seat,
            is_sitting_out=sitting_out,
        )
