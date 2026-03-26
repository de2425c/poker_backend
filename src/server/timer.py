"""
Action timer service for server-authoritative turn deadlines.

Tracks action deadlines and triggers auto-actions on timeout.
"""

import asyncio
import time
from dataclasses import dataclass
from typing import Optional, Callable, Awaitable


@dataclass
class PendingAction:
    """Tracks a pending action with deadline."""

    table_id: str
    user_id: str
    hand_id: str
    seat: int
    deadline_ms: int
    facing_bet: bool  # True = auto-fold, False = auto-check
    deadline_id: int = 0  # Monotonic token to detect stale callbacks


class ActionTimerService:
    """
    Server-authoritative action timer with auto-action on timeout.

    Runs a background tick loop that checks for expired deadlines
    and triggers auto-actions (fold if facing bet, check otherwise).
    """

    def __init__(self, tick_interval_ms: int = 250):
        self._tick_interval = tick_interval_ms / 1000
        self._pending: dict[str, PendingAction] = {}  # user_id -> pending
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._on_timeout: Optional[Callable[[PendingAction], Awaitable[None]]] = None
        self._next_deadline_id: int = 1  # Monotonic counter for deadline tokens

    def set_timeout_callback(
        self, callback: Callable[[PendingAction], Awaitable[None]]
    ) -> None:
        """Set callback for when timeout occurs."""
        self._on_timeout = callback

    def start(self) -> None:
        """Start the timer tick loop."""
        if not self._running:
            self._running = True
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        """Stop the timer tick loop."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    def register_deadline(
        self,
        table_id: str,
        user_id: str,
        hand_id: str,
        seat: int,
        deadline_ms: int,
        facing_bet: bool,
    ) -> int:
        """Register a new action deadline. Returns deadline_id for validation."""
        deadline_id = self._next_deadline_id
        self._next_deadline_id += 1

        self._pending[user_id] = PendingAction(
            table_id=table_id,
            user_id=user_id,
            hand_id=hand_id,
            seat=seat,
            deadline_ms=deadline_ms,
            facing_bet=facing_bet,
            deadline_id=deadline_id,
        )
        return deadline_id

    def clear_deadline(self, user_id: str) -> None:
        """Clear deadline when action received in time."""
        self._pending.pop(user_id, None)

    def is_expired(self, user_id: str) -> bool:
        """Check if user's deadline has passed."""
        pending = self._pending.get(user_id)
        if not pending:
            return False
        return int(time.time() * 1000) > pending.deadline_ms

    def get_pending(self, user_id: str) -> Optional[PendingAction]:
        """Get pending action for a user."""
        return self._pending.get(user_id)

    async def _run(self) -> None:
        """Main tick loop - checks for expired deadlines."""
        while self._running:
            try:
                await asyncio.sleep(self._tick_interval)
                await self._tick()
            except asyncio.CancelledError:
                break

    async def _tick(self) -> None:
        """Check all pending actions for expiration."""
        now_ms = int(time.time() * 1000)
        expired = []

        for user_id, pending in list(self._pending.items()):
            if now_ms > pending.deadline_ms:
                expired.append(pending)

        for pending in expired:
            self._pending.pop(pending.user_id, None)
            if self._on_timeout:
                try:
                    print(f"[TIMER] Timeout expired for {pending.user_id} seat={pending.seat} facing_bet={pending.facing_bet}", flush=True)
                    await self._on_timeout(pending)
                    print(f"[TIMER] Timeout handled for {pending.user_id}", flush=True)
                except Exception as e:
                    # Log but don't let one timeout failure stop others
                    print(f"[TIMER] Error handling timeout for {pending.user_id}: {e}", flush=True)
