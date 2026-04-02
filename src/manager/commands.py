"""
Command dataclasses for table operations.

Commands are submitted to TableRunner queues and processed serially.
Each command includes a Future for async result delivery.
"""

from dataclasses import dataclass
from typing import Optional, Union
import asyncio

from ..models import PlayerIdentity, Chips, ClientAction


@dataclass
class JoinTableCommand:
    """Request to join a table."""
    user_id: str
    player: PlayerIdentity
    buy_in: Chips
    result_future: asyncio.Future  # Resolves to (seat, snapshot) or raises


@dataclass
class LeaveTableCommand:
    """Request to leave a table."""
    user_id: str
    result_future: asyncio.Future  # Resolves to final_chips or raises


@dataclass
class PlayerActionCommand:
    """Player game action (fold, check, call, bet, raise)."""
    user_id: str
    hand_id: str  # Track which hand this action is for (stale action detection)
    action: ClientAction
    amount: Optional[Chips]
    result_future: asyncio.Future  # Resolves to list[events] or raises
    decision_metadata: Optional[dict] = None  # Bot decision context


@dataclass
class StartHandCommand:
    """Request to start a new hand."""
    result_future: asyncio.Future  # Resolves to list[events] or raises


@dataclass
class GetSnapshotCommand:
    """Request table snapshot for a user."""
    user_id: str
    result_future: asyncio.Future  # Resolves to TableSnapshotMessage or raises


@dataclass
class GetActionRequestCommand:
    """Request action request for a user who needs to act."""
    user_id: str
    result_future: asyncio.Future  # Resolves to ActionRequestMessage or raises


@dataclass
class TimeoutActionCommand:
    """Server-initiated timeout action (auto-fold/auto-check)."""
    user_id: str
    hand_id: str
    seat: int
    facing_bet: bool  # True = fold, False = check
    result_future: asyncio.Future  # Resolves to list[events]


# Union type for all commands
TableCommand = Union[
    JoinTableCommand,
    LeaveTableCommand,
    PlayerActionCommand,
    StartHandCommand,
    GetSnapshotCommand,
    GetActionRequestCommand,
    TimeoutActionCommand,
]
