"""
Protocol messages for client-server communication.

All messages inherit from a base that includes message type discrimination.
"""

from enum import Enum
from typing import Optional, Literal, Union, Annotated
from pydantic import BaseModel, Field, Discriminator, ConfigDict
from datetime import datetime

from .base import (
    ClientAction,
    ErrorCode,
    TableStatus,
    Chips,
    Card,
    Seat,
    HandState,
    ActionEvent,
    StreetDealtEvent,
    HandStartedEvent,
    HandEndedEvent,
    SeatUpdateEvent,
)


# =============================================================================
# MESSAGE TYPE REGISTRY
# =============================================================================

class ClientMessageType(str, Enum):
    """All message types a client can send to the server."""
    AUTH = "AUTH"
    JOIN_POOL = "JOIN_POOL"
    JOIN_TABLE = "JOIN_TABLE"
    LEAVE_TABLE = "LEAVE_TABLE"
    ACTION = "ACTION"
    NEXT_HAND = "NEXT_HAND"
    PING = "PING"


class ServerMessageType(str, Enum):
    """All message types the server can send to clients."""
    AUTH_OK = "AUTH_OK"
    TABLE_LEFT = "TABLE_LEFT"
    PONG = "PONG"
    ERROR = "ERROR"
    ACTION_REQUEST = "ACTION_REQUEST"
    TABLE_SNAPSHOT = "TABLE_SNAPSHOT"
    STATE_DELTA = "STATE_DELTA"


# =============================================================================
# CLIENT → SERVER MESSAGES
# =============================================================================

class AuthMessage(BaseModel):
    """
    First message after WebSocket connect. Authenticates the session.

    Flow:
    1. Client opens WebSocket
    2. Client sends AUTH with token (from your existing auth system)
    3. Server validates token, responds AUTH_OK or ERROR

    The token should be a short-lived JWT or session token from your
    existing Firebase/Auth system—NOT the user's password.
    """
    model_config = ConfigDict(extra="forbid")  # Reject unknown fields

    type: Literal["AUTH"] = "AUTH"
    token: str = Field(
        ...,
        min_length=1,
        description="Session token from auth system (JWT or session ID)"
    )
    # Client can declare protocol version for future compatibility
    protocol_version: int = Field(
        default=1,
        ge=1,
        description="Protocol version client supports"
    )


class JoinPoolMessage(BaseModel):
    """
    Request to join a table at a specific stake level.

    The server handles matchmaking:
    1. Finds an existing table with open seats at this stake, OR
    2. Creates a new table if none available

    Player specifies buy-in amount within the stake's min/max range.
    Server responds with TABLE_SNAPSHOT once seated.
    """
    model_config = ConfigDict(extra="forbid")

    type: Literal["JOIN_POOL"] = "JOIN_POOL"
    stake_id: str = Field(
        ...,
        description="Stake level identifier (e.g., 'nlh_1_2' for $1/$2 NLH)"
    )
    buy_in_cents: int = Field(
        ...,
        gt=0,
        description="Desired buy-in amount in cents (must be within stake min/max)"
    )
    display_name: str = Field(
        ...,
        min_length=1,
        max_length=20,
        description="Player's display name at the table"
    )


class LeaveTableMessage(BaseModel):
    """
    Request to leave the current table.

    Behavior depends on game state:
    - Between hands: Immediate departure, chips returned
    - Mid-hand (still active): Marked to leave after hand completes
    - Mid-hand (folded/all-in): Can leave immediately

    Server responds with TABLE_LEFT or keeps player until hand ends.
    """
    model_config = ConfigDict(extra="forbid")

    type: Literal["LEAVE_TABLE"] = "LEAVE_TABLE"
    # No additional fields needed - server knows which table from connection state


class ActionMessage(BaseModel):
    """
    Player submits a game action (fold, check, call, bet, raise_to).

    Idempotency: The (hand_id, action_id) pair ensures that if the client
    retries due to network issues, the server returns the same result
    rather than applying the action twice.

    Amount semantics:
    - FOLD, CHECK, CALL: No amount (server computes call amount)
    - BET: amount_cents = total bet size
    - RAISE_TO: amount_cents = total raise TO (not raise BY)

    All-in handling:
    - Client sends BET or RAISE_TO with amount = their stack
    - Server infers is_all_in = true when amount == remaining_stack
    - There is no "ALL_IN" action type

    Server validates:
    - It's this player's turn
    - The action is legal (via PokerKit can_* methods)
    - The amount is valid (min raise, max = stack)

    On success: Server broadcasts STATE_DELTA to all players.
    On failure: Server returns ERROR to this player only.
    """
    model_config = ConfigDict(extra="forbid")

    type: Literal["ACTION"] = "ACTION"
    hand_id: str = Field(
        ...,
        description="Hand this action applies to (prevents stale actions)"
    )
    action_id: str = Field(
        ...,
        description="Client-generated unique ID for idempotency"
    )
    action: ClientAction = Field(
        ...,
        description="Action type from ClientAction enum"
    )
    amount_cents: Optional[int] = Field(
        None,
        gt=0,
        description="Required for BET/RAISE_TO (total amount). Must be None for FOLD/CHECK/CALL."
    )


class PingMessage(BaseModel):
    """
    Heartbeat / latency check.

    Client sends PING periodically to:
    1. Keep WebSocket alive (prevent idle timeout)
    2. Measure round-trip latency

    Client includes a timestamp; server echoes it back in PONG
    so client can calculate RTT without clock sync issues.
    """
    model_config = ConfigDict(extra="forbid")

    type: Literal["PING"] = "PING"
    client_ts: int = Field(
        ...,
        description="Client timestamp in milliseconds (Unix epoch). Echoed in PONG for RTT calc."
    )


# =============================================================================
# SERVER → CLIENT MESSAGES
# =============================================================================

class PongMessage(BaseModel):
    """
    Response to PING.

    Echoes client's timestamp so they can calculate RTT.
    Also includes server time for optional clock sync.
    """
    type: Literal["PONG"] = "PONG"
    client_ts: int = Field(
        ...,
        description="Echoed from PING - client uses for RTT calculation"
    )
    server_ts: int = Field(
        ...,
        description="Server timestamp in milliseconds (Unix epoch)"
    )


class TableLeftMessage(BaseModel):
    """
    Confirms player has left the table.

    Sent when departure is complete (possibly after hand finishes).
    Includes final chip count for record-keeping.
    """
    type: Literal["TABLE_LEFT"] = "TABLE_LEFT"
    final_chips: Chips = Field(
        ...,
        description="Chips returned to player's bankroll"
    )


class AuthOkMessage(BaseModel):
    """
    Successful authentication response.

    Returns minimal user info so client confirms identity,
    plus server state like which table they're at (if reconnecting).
    """
    type: Literal["AUTH_OK"] = "AUTH_OK"
    user_id: str = Field(..., description="Authenticated user's ID")
    # If user was at a table (reconnecting), include table_id
    # so client knows to expect TABLE_SNAPSHOT next
    current_table_id: Optional[str] = Field(
        None,
        description="Table ID if user is already seated (reconnect scenario)"
    )
    server_time: datetime = Field(
        default_factory=datetime.utcnow,
        description="Server timestamp for client clock sync"
    )


class ErrorMessage(BaseModel):
    """
    Structured error response.

    Sent when a client request fails. Includes:
    - code: Machine-readable error type for programmatic handling
    - message: Human-readable explanation
    - ref_msg_id: Links to the client's action_id for correlation
    - details: Optional structured context (e.g., {"min_raise": 400})
    """
    type: Literal["ERROR"] = "ERROR"
    code: ErrorCode = Field(..., description="Error code from ErrorCode enum")
    message: str = Field(..., description="Human-readable error message")
    ref_msg_id: Optional[str] = Field(
        None,
        description="Client's action_id this error relates to (for correlation)"
    )
    details: Optional[dict] = Field(
        None,
        description="Additional structured context about the error"
    )


class ActionRequestMessage(BaseModel):
    """
    Server prompts a player to act.

    This is the ONLY place action prompts live (not in TABLE_SNAPSHOT).
    Contains everything the client needs to render valid action buttons:
    - Which actions are legal
    - Call amount (if applicable)
    - Min/max raise (if raising is allowed)
    - Time remaining to act

    Sent to the specific player whose turn it is.
    """
    type: Literal["ACTION_REQUEST"] = "ACTION_REQUEST"
    hand_id: str = Field(..., description="Current hand identifier")
    request_id: str = Field(
        ...,
        description="Server-generated ID, client can reference in ACTION"
    )
    seat: int = Field(..., ge=0, le=5, description="Seat that should act")
    allowed_actions: list[ClientAction] = Field(
        ...,
        description="Legal actions for this player"
    )
    call_amount: Optional[Chips] = Field(
        None,
        description="Amount to call (None if check is available)"
    )
    min_raise: Optional[Chips] = Field(
        None,
        description="Minimum raise amount (None if can't raise)"
    )
    max_raise: Optional[Chips] = Field(
        None,
        description="Maximum raise = player's stack"
    )
    pot: Chips = Field(
        ...,
        description="Current pot size (authoritative from server)"
    )
    expires_at_ms: int = Field(
        ...,
        description="Unix timestamp (ms) when action times out"
    )
    opponent_hole_cards: Optional[dict[int, list[Card]]] = Field(
        None,
        description="Opponent hole cards by seat (bot-only optimization for quick folds)"
    )


class TableSnapshotMessage(BaseModel):
    """
    Complete table state.

    Sent on:
    - Initial join (after JOIN_POOL)
    - Reconnect
    - New hand start (with hole cards)

    This is pure world state - NO action prompts.
    Action prompts come separately via ACTION_REQUEST.
    """
    type: Literal["TABLE_SNAPSHOT"] = "TABLE_SNAPSHOT"
    table_id: str = Field(..., description="Unique table identifier")
    status: TableStatus = Field(..., description="Table lifecycle state")
    stake_id: str = Field(..., description="Stake level (e.g., 'nlh_1_2')")
    small_blind: Chips = Field(..., description="Small blind amount")
    big_blind: Chips = Field(..., description="Big blind amount")
    seats: list[Seat] = Field(..., description="All 6 seats")
    hand: Optional[HandState] = Field(
        None,
        description="Current hand state (None if between hands)"
    )
    your_seat: int = Field(..., ge=0, le=5, description="Recipient's seat index")
    your_hole_cards: Optional[list[Card]] = Field(
        None,
        description="Recipient's private hole cards (None if not dealt yet)"
    )
    seq: int = Field(..., ge=0, description="Current sequence number for this table")


# Discriminated union for game events
# Uses event_type field as discriminator
GameEvent = Annotated[
    Union[ActionEvent, StreetDealtEvent, HandStartedEvent, HandEndedEvent, SeatUpdateEvent],
    Field(discriminator="event_type")
]


class StateDeltaMessage(BaseModel):
    """
    Incremental state update.

    Broadcast to all players at the table after each game event.
    Contains ordered list of events that occurred.

    Clients use seq for:
    - Ordering (monotonically increasing)
    - Gap detection (request snapshot if seq jumps)
    """
    type: Literal["STATE_DELTA"] = "STATE_DELTA"
    table_id: str = Field(..., description="Table this update is for")
    hand_id: Optional[str] = Field(
        None,
        description="Hand ID (None for between-hand events)"
    )
    seq: int = Field(..., ge=0, description="Monotonic sequence number")
    events: list[GameEvent] = Field(..., description="Ordered list of game events")
    actor_seat: Optional[int] = Field(None, description="Seat index of next actor, or None if no action needed")


class QuipMessage(BaseModel):
    """
    Bot quip message for trash talk.

    Sent by bot clients and broadcast to all players at the table.
    Contains AI-generated context-aware quip based on game state.
    """
    type: Literal["QUIP"] = "QUIP"
    hand_id: str = Field(..., description="Hand ID this quip relates to")
    seat: int = Field(..., ge=0, le=5, description="Seat of the bot sending the quip")
    text: str = Field(..., min_length=1, max_length=100, description="The quip text")
