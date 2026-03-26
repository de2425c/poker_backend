"""
Hand logger for building and persisting hand history and chip ledger.

Converts buffered events to structured HandLog and LedgerEntry objects.
"""

import asyncio
import logging
from datetime import datetime
from typing import Optional

from pydantic import BaseModel

logger = logging.getLogger(__name__)

from ..models import (
    ActionEvent,
    StreetDealtEvent,
    HandEndedEvent,
    HandStartedEvent,
    Street,
)
from .models import (
    HandLog,
    SeatRecord,
    ActionRecord,
    WinnerRecord,
    LedgerEntry,
    LedgerReason,
)
from .firestore_client import FirestoreClient


class HandLogger:
    """
    Builds HandLog and LedgerEntries from events.

    Handles async persistence with retry queue for failures.
    """

    def __init__(self, firestore: FirestoreClient):
        self._firestore = firestore
        self._retry_queue: list[tuple[HandLog, list[LedgerEntry]]] = []

    @property
    def retry_queue_size(self) -> int:
        """Number of failed writes waiting for retry."""
        return len(self._retry_queue)

    def log_hand(
        self,
        table_id: str,
        stake_id: str,
        hand_id: str,
        events: list[BaseModel],
        seat_snapshot: list[SeatRecord],
        started_at: datetime,
        button_seat: int,
        small_blind: int,
        big_blind: int,
        hole_cards: dict[int, list[str]] | None = None,
    ) -> None:
        """
        Build and persist hand log + ledger entries.

        Args:
            table_id: Table where hand occurred
            stake_id: Stakes identifier (e.g., "nlh_1_2")
            hand_id: Unique hand identifier
            events: List of game events
            seat_snapshot: Seat states at hand start
            started_at: When hand started
            button_seat: Dealer button position
            small_blind: Small blind in cents
            big_blind: Big blind in cents
            hole_cards: Per-seat hole cards {seat_index: ["Ah", "Ks"]}
        """
        # Build HandLog from events
        hand_log = self._build_hand_log(
            hand_id=hand_id,
            table_id=table_id,
            stake_id=stake_id,
            events=events,
            seat_snapshot=seat_snapshot,
            started_at=started_at,
            button_seat=button_seat,
            small_blind=small_blind,
            big_blind=big_blind,
            hole_cards=hole_cards or {},
        )

        # Extract ledger entries from stack deltas
        ledger_entries = self._build_ledger_entries(hand_log, seat_snapshot)

        # Async write (best-effort, non-blocking)
        logger.info(f"Queuing hand {hand_id} for persistence (winners: {len(hand_log.winners)}, deltas: {len(hand_log.stack_deltas)})")
        asyncio.create_task(
            self._write_with_retry(hand_log, ledger_entries)
        )

    def _build_hand_log(
        self,
        hand_id: str,
        table_id: str,
        stake_id: str,
        events: list[BaseModel],
        seat_snapshot: list[SeatRecord],
        started_at: datetime,
        button_seat: int,
        small_blind: int,
        big_blind: int,
        hole_cards: dict[int, list[str]] | None = None,
    ) -> HandLog:
        """Convert event stream to HandLog."""
        actions: list[ActionRecord] = []
        board: list[str] = []
        winners: list[WinnerRecord] = []
        current_street = "preflop"

        # Track chips contributed per street (max per seat, since bet/raise amounts
        # are cumulative for the betting round)
        street_contrib: dict[int, int] = {s.seat_index: 0 for s in seat_snapshot}
        total_contributed: dict[int, int] = {s.seat_index: 0 for s in seat_snapshot}
        chips_won: dict[int, int] = {s.seat_index: 0 for s in seat_snapshot}

        # Track last aggressive action to detect uncalled bets
        last_aggressor_seat: int | None = None
        last_aggressor_amount: int = 0

        for event in events:
            if isinstance(event, HandStartedEvent):
                # Already have hand_id from parameter
                pass

            elif isinstance(event, ActionEvent):
                actions.append(ActionRecord(
                    seat=event.seat,
                    action=event.action.value,
                    amount=event.amount.amount if event.amount else None,
                    is_all_in=event.is_all_in,
                    street=current_street,
                    timestamp=datetime.utcnow(),
                ))

                # Track chips put into pot per street
                # Bet/raise amounts are cumulative for the betting round,
                # so we track the MAX per seat per street
                if event.amount:
                    current = street_contrib.get(event.seat, 0)
                    street_contrib[event.seat] = max(current, event.amount.amount)

                # Track last aggressor (bet/raise) to detect uncalled bets
                action = event.action.value
                if action in ("bet", "raise_to") and event.amount:
                    last_aggressor_seat = event.seat
                    last_aggressor_amount = event.amount.amount
                elif action in ("call", "check"):
                    # Action was called/checked, no longer uncalled
                    last_aggressor_seat = None
                    last_aggressor_amount = 0
                # fold doesn't reset - the bet remains uncalled

            elif isinstance(event, StreetDealtEvent):
                # Finalize previous street contributions
                for seat_idx, amount in street_contrib.items():
                    total_contributed[seat_idx] = (
                        total_contributed.get(seat_idx, 0) + amount
                    )
                # Reset for new street
                street_contrib = {s.seat_index: 0 for s in seat_snapshot}
                # Reset aggressor tracking for new street
                last_aggressor_seat = None
                last_aggressor_amount = 0
                # Update current street
                current_street = event.street.value
                # Add new cards to board
                for card in event.cards:
                    board.append(f"{card.rank}{card.suit}")

            elif isinstance(event, HandEndedEvent):
                # Finalize last street contributions before processing winners
                for seat_idx, amount in street_contrib.items():
                    total_contributed[seat_idx] = (
                        total_contributed.get(seat_idx, 0) + amount
                    )

                # Remove uncalled bet from winner's contributions
                # If hand ended with an uncalled bet (everyone folded), that money returns
                if last_aggressor_seat is not None and last_aggressor_amount > 0:
                    # The aggressor's contribution should only be up to what was called
                    # Find the max amount any other player put in this street
                    called_amount = 0
                    for seat_idx, amount in street_contrib.items():
                        if seat_idx != last_aggressor_seat:
                            called_amount = max(called_amount, amount)
                    # Reduce aggressor's contribution to what was actually called
                    uncalled = last_aggressor_amount - called_amount
                    if uncalled > 0:
                        total_contributed[last_aggressor_seat] = (
                            total_contributed.get(last_aggressor_seat, 0) - uncalled
                        )

                # Build winner records
                for w in event.winners:
                    # Find user_id from seat snapshot
                    user_id = ""
                    for s in seat_snapshot:
                        if s.seat_index == w.seat:
                            user_id = s.user_id
                            break

                    shown = None
                    if w.shown_cards:
                        shown = [f"{c.rank}{c.suit}" for c in w.shown_cards]

                    winners.append(WinnerRecord(
                        seat=w.seat,
                        user_id=user_id,
                        amount_won=w.amount.amount,
                        hand_description=w.hand_description,
                        shown_cards=shown,
                    ))

                    # Winner's amount is the payoff (profit), which IS the delta
                    # For non-winners, delta = -contributed
                    chips_won[w.seat] = chips_won.get(w.seat, 0) + w.amount.amount

        # Calculate stack deltas from contributions (ensures chip conservation)
        # Winner gains what others lost, loser loses what they contributed
        total_pot = sum(total_contributed.values())
        winner_seats = set(chips_won.keys()) if chips_won else set()

        stack_deltas: dict[int, int] = {}
        for seat in seat_snapshot:
            seat_idx = seat.seat_index
            contributed = total_contributed.get(seat_idx, 0)
            if chips_won.get(seat_idx, 0) > 0:
                # Winner: chips_won contains PokerKit payoffs which are already
                # net profit/loss (the delta). Use directly without subtracting.
                delta = chips_won[seat_idx]
            else:
                # Non-winner: lost what they contributed
                delta = -contributed
            if delta != 0:
                stack_deltas[seat_idx] = delta

        return HandLog(
            hand_id=hand_id,
            table_id=table_id,
            stake_id=stake_id,
            started_at=started_at,
            ended_at=datetime.utcnow(),
            seats=seat_snapshot,
            button_seat=button_seat,
            small_blind=small_blind,
            big_blind=big_blind,
            actions=actions,
            hole_cards=hole_cards or {},
            board=board,
            winners=winners,
            stack_deltas=stack_deltas,
        )

    def _build_ledger_entries(
        self,
        hand_log: HandLog,
        seats: list[SeatRecord],
    ) -> list[LedgerEntry]:
        """Create ledger entry per player based on stack delta."""
        entries = []

        # Create seat_index -> user_id mapping
        seat_to_user = {s.seat_index: s.user_id for s in seats}

        for seat_idx, delta in hand_log.stack_deltas.items():
            user_id = seat_to_user.get(seat_idx, "")
            if not user_id:
                continue

            # Determine reason based on delta sign
            reason = LedgerReason.WIN if delta > 0 else LedgerReason.BET

            entries.append(LedgerEntry.create(
                user_id=user_id,
                delta=delta,
                reason=reason,
                table_id=hand_log.table_id,
                hand_id=hand_log.hand_id,
            ))

        return entries

    async def _write_with_retry(
        self,
        hand_log: HandLog,
        entries: list[LedgerEntry],
    ) -> None:
        """Write to Firestore with retry on failure."""
        try:
            logger.info(f"Persisting hand {hand_log.hand_id} with {len(entries)} ledger entries")
            await self._firestore.write_hand_log(hand_log)
            await self._firestore.write_ledger_entries(entries)
            logger.info(f"Hand {hand_log.hand_id} persisted successfully")
        except Exception as e:
            # Add to retry queue (MVP: in-memory)
            logger.error(f"Failed to persist hand {hand_log.hand_id}: {e}")
            self._retry_queue.append((hand_log, entries))

    async def retry_failed_writes(self) -> int:
        """
        Attempt to retry failed writes.

        Returns:
            Number of successful retries
        """
        if not self._retry_queue:
            return 0

        succeeded = 0
        still_failed = []

        for hand_log, entries in self._retry_queue:
            try:
                await self._firestore.write_hand_log(hand_log)
                await self._firestore.write_ledger_entries(entries)
                succeeded += 1
            except Exception:
                still_failed.append((hand_log, entries))

        self._retry_queue = still_failed
        return succeeded
