"""
Base types and enums for the poker protocol.

These are the foundational building blocks that all other schemas depend on.
"""

from enum import Enum
from typing import Optional, Literal
from pydantic import BaseModel, Field, ConfigDict
from datetime import datetime
import uuid


# =============================================================================
# ENUMS
# =============================================================================

class ClientAction(str, Enum):
    """
    Actions a client can send to the server.

    This is the restricted set - clients pick from these.
    "All-in" is NOT here because it's a property of a bet/raise,
    not a separate action. Server infers is_all_in when amount == stack.
    """
    FOLD = "fold"
    CHECK = "check"
    CALL = "call"
    BET = "bet"          # Open betting (no prior bet this street)
    RAISE_TO = "raise_to"  # Raise to a total amount (not raise BY)


class ServerAction(str, Enum):
    """
    Actions in server→client events and logs.

    Superset of ClientAction - includes forced actions and metadata.
    Used in STATE_DELTA events and hand history.
    """
    # Player-initiated (mirrors ClientAction)
    FOLD = "fold"
    CHECK = "check"
    CALL = "call"
    BET = "bet"
    RAISE_TO = "raise_to"

    # Forced actions (server-initiated, not player-requested)
    POST_BLIND = "post_blind"
    POST_ANTE = "post_ante"


class Street(str, Enum):
    """
    Betting rounds in Hold'em.

    Useful for:
    - Filtering hand history by street
    - UI rendering (show board cards progressively)
    - Analytics (win rate by street)
    """
    PREFLOP = "preflop"
    FLOP = "flop"
    TURN = "turn"
    RIVER = "river"
    SHOWDOWN = "showdown"


class TableStatus(str, Enum):
    """
    Table lifecycle states.

    WAITING: Not enough players to start
    RUNNING: Hand in progress
    BETWEEN_HANDS: Hand complete, preparing next
    PAUSED: Admin pause (e.g., break time)
    CLOSED: Table shutting down
    """
    WAITING = "waiting"
    RUNNING = "running"
    BETWEEN_HANDS = "between_hands"
    PAUSED = "paused"
    CLOSED = "closed"


class SeatStatus(str, Enum):
    """
    Individual seat states within a table.

    EMPTY: No player
    RESERVED: Player joining (brief hold during matchmaking)
    SEATED: Player present but sitting out
    ACTIVE: Player in current hand
    ALL_IN: Player all-in (can't act but still in hand)
    FOLDED: Folded this hand
    """
    EMPTY = "empty"
    RESERVED = "reserved"
    SEATED = "seated"
    ACTIVE = "active"
    ALL_IN = "all_in"
    FOLDED = "folded"


class ErrorCode(str, Enum):
    """
    Structured error codes for client handling.

    Clients can switch on these codes for programmatic responses
    rather than parsing error messages.
    """
    # Connection/Auth errors (1xx)
    UNAUTHORIZED = "unauthorized"
    SESSION_EXPIRED = "session_expired"
    ALREADY_CONNECTED = "already_connected"

    # Protocol errors (2xx)
    BAD_REQUEST = "bad_request"
    INVALID_MESSAGE = "invalid_message"
    UNKNOWN_MESSAGE_TYPE = "unknown_message_type"

    # Game logic errors (3xx)
    NOT_YOUR_TURN = "not_your_turn"
    INVALID_ACTION = "invalid_action"
    INVALID_AMOUNT = "invalid_amount"
    INSUFFICIENT_CHIPS = "insufficient_chips"
    INSUFFICIENT_BALANCE = "insufficient_balance"
    HAND_ALREADY_COMPLETE = "hand_already_complete"
    ACTION_TIMEOUT = "action_timeout"

    # Table errors (4xx)
    TABLE_NOT_FOUND = "table_not_found"
    TABLE_FULL = "table_full"
    NOT_AT_TABLE = "not_at_table"
    ALREADY_AT_TABLE = "already_at_table"

    # Server errors (5xx)
    INTERNAL_ERROR = "internal_error"
    SERVICE_UNAVAILABLE = "service_unavailable"


# =============================================================================
# BASE MODELS
# =============================================================================

class Card(BaseModel):
    """
    Single playing card.

    Using separate rank/suit rather than "Ah" string because:
    - Type safety (can't pass invalid card)
    - Easier to work with in logic
    - PokerKit uses this format internally

    We'll add a .notation property for display ("Ah", "Kd", etc.)
    """
    rank: str = Field(..., pattern=r"^[2-9TJQKA]$", description="Card rank: 2-9, T, J, Q, K, A")
    suit: str = Field(..., pattern=r"^[shdc]$", description="Card suit: s(pades), h(earts), d(iamonds), c(lubs)")

    model_config = ConfigDict(frozen=True)  # Cards are immutable

    @property
    def notation(self) -> str:
        """Standard poker notation like 'Ah' for Ace of hearts."""
        return f"{self.rank}{self.suit}"


class Chips(BaseModel):
    """
    Chip amount with explicit denomination.

    Why a model instead of just int?
    - Prevents confusion between cents/dollars/BB
    - Can add validation (non-negative)
    - Future: multi-currency support

    All internal calculations use smallest unit (cents).
    Display conversion happens at the edge.
    """
    model_config = ConfigDict(frozen=True)

    amount: int = Field(..., ge=0, description="Amount in smallest unit (cents)")


class PlayerIdentity(BaseModel):
    """
    Minimal player info for protocol messages.

    Separate from full User model because:
    - Only send what's needed over the wire
    - Decouple game protocol from user system
    - Privacy: don't leak extra user data
    """
    user_id: str = Field(..., description="Unique user identifier")
    display_name: str = Field(..., max_length=20, description="Table display name")
    avatar_url: Optional[str] = Field(None, description="Avatar image URL")


# =============================================================================
# TABLE STATE MODELS
# =============================================================================

class Seat(BaseModel):
    """
    State of a single seat at the table.

    Combines player identity, chip count, and current hand state.
    is_connected is separate from SeatStatus to avoid conflating
    network state with game state.
    """
    seat_index: int = Field(..., ge=0, le=5, description="Seat position 0-5 for 6-max")
    status: SeatStatus = Field(..., description="Game state of seat")
    player: Optional[PlayerIdentity] = Field(None, description="Player info if occupied")
    chips: Chips = Field(..., description="Current stack")
    bet: Chips = Field(default_factory=lambda: Chips(amount=0), description="Current bet this street")
    is_button: bool = Field(default=False, description="True if this seat has the dealer button")
    is_connected: bool = Field(default=True, description="Network connectivity state")


class Pot(BaseModel):
    """
    A pot (main or side pot).

    Side pots occur when a player is all-in for less than others.
    eligible_seats tracks who can win each pot.
    """
    amount: Chips = Field(..., description="Total chips in this pot")
    eligible_seats: list[int] = Field(..., description="Seat indices that can win this pot")


class HandState(BaseModel):
    """
    State of the current hand in progress.

    This is pure world state - no action prompts.
    Action prompts (allowed_actions, min/max raise) live in ACTION_REQUEST.
    """
    hand_id: str = Field(..., description="Unique hand identifier")
    street: Street = Field(..., description="Current betting round")
    board: list[Card] = Field(default_factory=list, description="Community cards (0-5)")
    pots: list[Pot] = Field(default_factory=list, description="Main pot + side pots")
    current_bet: Chips = Field(..., description="Current bet to call")
    actor_seat: Optional[int] = Field(None, description="Seat index of player to act (None between streets)")


# =============================================================================
# GAME EVENTS (for STATE_DELTA)
# =============================================================================

class ActionEvent(BaseModel):
    """A player performed an action."""
    event_type: Literal["action"] = "action"
    seat: int = Field(..., ge=0, le=5, description="Seat that acted")
    action: ServerAction = Field(..., description="Action taken")
    amount: Optional[Chips] = Field(None, description="Amount for bet/raise/call")
    is_all_in: bool = Field(default=False, description="True if this action put player all-in")
    showdown_hands: Optional[list["ShowdownHand"]] = Field(
        None, description="Hole cards revealed when this action triggers an all-in runout"
    )


class StreetDealtEvent(BaseModel):
    """New community cards dealt (flop/turn/river)."""
    event_type: Literal["street_dealt"] = "street_dealt"
    street: Street = Field(..., description="The street that was dealt")
    cards: list[Card] = Field(..., description="The new board cards")
    showdown_hands: Optional[list["ShowdownHand"]] = Field(
        None, description="Hole cards for all active players (sent during all-in runouts)"
    )


class HandStartedEvent(BaseModel):
    """A new hand has begun."""
    event_type: Literal["hand_started"] = "hand_started"
    hand_id: str = Field(..., description="Unique hand identifier")
    button_seat: int = Field(..., ge=0, le=5, description="Seat with dealer button")


class PotWinner(BaseModel):
    """Winner of a pot (or portion of pot)."""
    seat: int = Field(..., ge=0, le=5, description="Winning seat")
    amount: Chips = Field(..., description="Amount won")
    hand_description: Optional[str] = Field(None, description="e.g., 'Two Pair, Aces and Kings'")
    shown_cards: Optional[list[Card]] = Field(None, description="Cards shown (None if mucked)")


class ShowdownHand(BaseModel):
    """Hole cards revealed at showdown for a player."""
    seat: int = Field(..., ge=0, le=5, description="Seat index")
    cards: list[Card] = Field(..., description="Hole cards")
    hand_description: Optional[str] = Field(None, description="e.g., 'Two Pair, Aces and Kings'")


class HandEndedEvent(BaseModel):
    """Hand has completed, pots awarded."""
    event_type: Literal["hand_ended"] = "hand_ended"
    hand_id: str = Field(..., description="Hand that ended")
    winners: list[PotWinner] = Field(..., description="List of pot winners")
    showdown_hands: Optional[list[ShowdownHand]] = Field(None, description="Hole cards for all players at showdown")


class SeatUpdateEvent(BaseModel):
    """Player seat status changed (sitting out, etc)."""
    event_type: Literal["seat_update"] = "seat_update"
    seat: int = Field(..., ge=0, le=5, description="Seat that changed")
    is_sitting_out: bool = Field(..., description="Whether player is sitting out")


# =============================================================================
# ID GENERATION
# =============================================================================

def generate_hand_id() -> str:
    """Generate unique hand identifier. Prefixed for easy log grepping."""
    return f"hand_{uuid.uuid4().hex[:12]}"

def generate_action_id() -> str:
    """Generate unique action identifier for idempotency."""
    return f"act_{uuid.uuid4().hex[:8]}"

def generate_table_id() -> str:
    """Generate unique table identifier."""
    return f"tbl_{uuid.uuid4().hex[:10]}"
