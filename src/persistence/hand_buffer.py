"""
Hand event buffer for collecting events during a hand.

Collects all events that occur during a hand for later persistence.
"""

from datetime import datetime
from typing import Optional
from pydantic import BaseModel

from .models import SeatRecord


class HandBuffer:
    """
    Buffers hand events for batch persistence.

    Collects events during a hand, then finalizes them for logging
    when the hand ends.
    """

    def __init__(self):
        self._current_hand: Optional[str] = None
        self._events: list[BaseModel] = []
        self._started_at: Optional[datetime] = None
        self._seat_snapshot: list[SeatRecord] = []
        self._button_seat: int = 0

    @property
    def is_active(self) -> bool:
        """Check if a hand is currently being buffered."""
        return self._current_hand is not None

    @property
    def hand_id(self) -> Optional[str]:
        """Get current hand ID being buffered."""
        return self._current_hand

    def start_hand(
        self,
        hand_id: str,
        seats: list[SeatRecord],
        button_seat: int,
    ) -> None:
        """
        Begin buffering for a new hand.

        Args:
            hand_id: Unique identifier for this hand
            seats: Snapshot of seats at hand start
            button_seat: Seat index with the button
        """
        self._current_hand = hand_id
        self._events = []
        self._started_at = datetime.utcnow()
        self._seat_snapshot = list(seats)  # Copy to avoid mutation
        self._button_seat = button_seat

    def record_event(self, event: BaseModel) -> None:
        """
        Add event to buffer.

        Args:
            event: Game event (ActionEvent, StreetDealtEvent, etc.)
        """
        if self._current_hand is not None:
            self._events.append(event)

    def finalize(self) -> tuple[
        Optional[str],
        list[BaseModel],
        list[SeatRecord],
        Optional[datetime],
        int,
    ]:
        """
        Return buffered data and reset.

        Returns:
            Tuple of (hand_id, events, seat_snapshot, started_at, button_seat)
        """
        result = (
            self._current_hand,
            self._events,
            self._seat_snapshot,
            self._started_at,
            self._button_seat,
        )

        # Reset state
        self._current_hand = None
        self._events = []
        self._seat_snapshot = []
        self._started_at = None
        self._button_seat = 0

        return result

    def abort(self) -> None:
        """Discard current buffer without finalizing."""
        self._current_hand = None
        self._events = []
        self._seat_snapshot = []
        self._started_at = None
        self._button_seat = 0
