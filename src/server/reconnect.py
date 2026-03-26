"""
Reconnection grace period manager.

Allows players to reconnect within a grace period after disconnection
without losing their seat at the table.
"""

import asyncio
from dataclasses import dataclass
from typing import Callable, Awaitable, Optional
from datetime import datetime


@dataclass
class PendingDisconnect:
    """Tracks a pending disconnection that's in grace period."""
    user_id: str
    table_id: str
    disconnect_time: datetime
    task: asyncio.Task


class ReconnectManager:
    """
    Manages reconnection grace periods.

    When a player disconnects, instead of immediately removing them,
    we start a grace period timer. If they reconnect within the grace
    period, we cancel the removal. If the timer expires, we execute
    the actual removal.
    """

    def __init__(self, grace_period_seconds: float = 60.0):
        self._grace_period = grace_period_seconds
        self._pending: dict[str, PendingDisconnect] = {}  # user_id -> PendingDisconnect
        self._on_grace_expired: Optional[Callable[[str, str], Awaitable[None]]] = None

    def set_expiry_callback(
        self,
        callback: Callable[[str, str], Awaitable[None]]
    ) -> None:
        """
        Set callback to be called when grace period expires.

        Callback signature: async def callback(user_id: str, table_id: str)
        """
        self._on_grace_expired = callback

    def start_grace_period(self, user_id: str, table_id: str) -> None:
        """
        Start a grace period for a disconnected player.

        If they're already in a grace period, this resets the timer.
        """
        # Cancel existing grace period if any
        self.cancel_grace_period(user_id)

        # Start new grace period task
        task = asyncio.create_task(
            self._grace_period_task(user_id, table_id)
        )

        self._pending[user_id] = PendingDisconnect(
            user_id=user_id,
            table_id=table_id,
            disconnect_time=datetime.now(),
            task=task,
        )

        print(f"[RECONNECT] Grace period started for {user_id} at table {table_id} ({self._grace_period}s)")

    def cancel_grace_period(self, user_id: str) -> bool:
        """
        Cancel a pending disconnection (player reconnected).

        Returns True if there was a pending disconnection that was cancelled.
        """
        pending = self._pending.pop(user_id, None)
        if pending:
            pending.task.cancel()
            print(f"[RECONNECT] Grace period cancelled for {user_id} (reconnected)")
            return True
        return False

    def is_in_grace_period(self, user_id: str) -> bool:
        """Check if a player is currently in a grace period."""
        return user_id in self._pending

    def get_table_for_disconnected(self, user_id: str) -> Optional[str]:
        """Get the table_id for a disconnected player in grace period."""
        pending = self._pending.get(user_id)
        return pending.table_id if pending else None

    async def _grace_period_task(self, user_id: str, table_id: str) -> None:
        """Background task that waits for grace period then triggers removal."""
        try:
            await asyncio.sleep(self._grace_period)

            # Grace period expired - remove from pending and trigger callback
            self._pending.pop(user_id, None)

            print(f"[RECONNECT] Grace period expired for {user_id} - removing from table {table_id}")

            if self._on_grace_expired:
                await self._on_grace_expired(user_id, table_id)

        except asyncio.CancelledError:
            # Grace period was cancelled (player reconnected)
            pass
