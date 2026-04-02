"""
Data models for hand logging and chip ledger.

These models are designed for Firestore storage and replay verification.
"""

from dataclasses import dataclass, field, asdict
from datetime import datetime
from enum import Enum
from typing import Optional
import uuid


class LedgerReason(str, Enum):
    """Reason for chip movement in ledger."""
    BLIND = "blind"
    BET = "bet"           # Includes calls and raises
    WIN = "win"
    BUYIN = "buyin"
    CASHOUT = "cashout"


@dataclass
class SeatRecord:
    """Snapshot of a seat at hand start."""
    seat_index: int
    user_id: str
    display_name: str
    starting_stack: int  # cents

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ActionRecord:
    """Single action in a hand."""
    seat: int
    action: str  # fold, check, call, bet, raise_to, post_blind
    amount: Optional[int]  # cents, None for fold/check
    is_all_in: bool
    street: str = "preflop"  # preflop, flop, turn, river
    timestamp: Optional[datetime] = None
    decision_metadata: Optional[dict] = None  # Bot decision context (solver data, ranges, etc.)

    def to_dict(self) -> dict:
        d = asdict(self)
        if d["timestamp"]:
            d["timestamp"] = d["timestamp"].isoformat()
        return d


@dataclass
class WinnerRecord:
    """Winner of a pot."""
    seat: int
    user_id: str
    amount_won: int  # cents
    hand_description: Optional[str] = None  # e.g., "Two Pair, Aces and Kings"
    shown_cards: Optional[list[str]] = None  # e.g., ["Ah", "Ks"]

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class HandLog:
    """Complete hand history for replay and audit."""
    hand_id: str
    table_id: str
    stake_id: str  # e.g., "nlh_1_2"

    # Timestamps
    started_at: datetime
    ended_at: datetime

    # Participants (snapshot at hand start)
    seats: list[SeatRecord]
    button_seat: int
    small_blind: int  # cents
    big_blind: int    # cents

    # Action sequence (ordered)
    actions: list[ActionRecord]

    # Hole cards per seat: {seat_index: ["Ah", "Ks"]}
    hole_cards: dict[int, list[str]] = field(default_factory=dict)

    # Board runout
    board: list[str] = field(default_factory=list)  # ["Ah", "Ks", "Qd", "Jc", "2h"]

    # Showdown / result
    winners: list[WinnerRecord] = field(default_factory=list)

    # Final deltas (computed) - seat_index -> delta in cents (signed)
    stack_deltas: dict[int, int] = field(default_factory=dict)

    def to_dict(self) -> dict:
        """Convert to dict for Firestore storage."""
        return {
            "hand_id": self.hand_id,
            "table_id": self.table_id,
            "stake_id": self.stake_id,
            "started_at": self.started_at.isoformat(),
            "ended_at": self.ended_at.isoformat(),
            "seats": [s.to_dict() for s in self.seats],
            "button_seat": self.button_seat,
            "small_blind": self.small_blind,
            "big_blind": self.big_blind,
            "actions": [a.to_dict() for a in self.actions],
            "hole_cards": {str(k): v for k, v in self.hole_cards.items()},
            "board": self.board,
            "winners": [w.to_dict() for w in self.winners],
            "stack_deltas": {str(k): v for k, v in self.stack_deltas.items()},
            # Flat array of user IDs for Firestore array-contains queries
            "participant_ids": [s.user_id for s in self.seats],
        }


@dataclass
class LedgerEntry:
    """Individual chip movement for accounting."""
    entry_id: str
    user_id: str
    delta: int  # Signed cents (+win, -loss)
    reason: LedgerReason
    hand_id: Optional[str]  # Null for BUYIN/CASHOUT
    table_id: str
    created_at: datetime

    @staticmethod
    def create(
        user_id: str,
        delta: int,
        reason: LedgerReason,
        table_id: str,
        hand_id: Optional[str] = None,
    ) -> "LedgerEntry":
        """Factory method to create a new ledger entry."""
        return LedgerEntry(
            entry_id=str(uuid.uuid4()),
            user_id=user_id,
            delta=delta,
            reason=reason,
            hand_id=hand_id,
            table_id=table_id,
            created_at=datetime.utcnow(),
        )

    def to_dict(self) -> dict:
        """Convert to dict for Firestore storage."""
        return {
            "entry_id": self.entry_id,
            "user_id": self.user_id,
            "delta": self.delta,
            "reason": self.reason.value,
            "hand_id": self.hand_id,
            "table_id": self.table_id,
            "created_at": self.created_at.isoformat(),
        }
