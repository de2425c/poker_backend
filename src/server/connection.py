"""
WebSocket connection manager.

Tracks connected clients and provides broadcasting functionality.
"""

from typing import Optional
from fastapi import WebSocket


class ConnectionManager:
    """Manages WebSocket connections and broadcasts."""

    def __init__(self):
        self._connections: dict[str, WebSocket] = {}  # user_id -> ws
        self._table_users: dict[str, set[str]] = {}   # table_id -> {user_ids}
        self._user_tables: dict[str, str] = {}        # user_id -> table_id

    async def connect(self, user_id: str, websocket: WebSocket) -> None:
        """
        Register a new connection.

        Handles reconnect by closing any existing connection for this user.
        """
        # Disconnect existing connection if any (handles reconnect)
        if user_id in self._connections:
            old_ws = self._connections[user_id]
            try:
                await old_ws.close()
            except Exception:
                pass
        self._connections[user_id] = websocket

    def disconnect(self, user_id: str) -> None:
        """Remove a connection."""
        self._connections.pop(user_id, None)
        table_id = self._user_tables.pop(user_id, None)
        if table_id and table_id in self._table_users:
            self._table_users[table_id].discard(user_id)

    def join_table(self, user_id: str, table_id: str) -> None:
        """Track user joining a table for broadcasts."""
        self._user_tables[user_id] = table_id
        if table_id not in self._table_users:
            self._table_users[table_id] = set()
        self._table_users[table_id].add(user_id)

    def leave_table(self, user_id: str) -> None:
        """Track user leaving a table."""
        table_id = self._user_tables.pop(user_id, None)
        if table_id and table_id in self._table_users:
            self._table_users[table_id].discard(user_id)

    def get_user_table(self, user_id: str) -> Optional[str]:
        """Get the table_id for a user, or None if not at a table."""
        return self._user_tables.get(user_id)

    async def send_to_user(self, user_id: str, message: dict) -> bool:
        """
        Send a message to a specific user.

        Returns True if sent successfully, False if user not connected.
        """
        ws = self._connections.get(user_id)
        if ws:
            try:
                await ws.send_json(message)
                return True
            except Exception:
                self.disconnect(user_id)
        return False

    async def broadcast_to_table(
        self, table_id: str, message: dict, exclude: Optional[str] = None
    ) -> None:
        """Broadcast a message to all users at a table."""
        user_ids = self._table_users.get(table_id, set()).copy()
        for user_id in user_ids:
            if user_id != exclude:
                await self.send_to_user(user_id, message)

    def get_table_users(self, table_id: str) -> set[str]:
        """Get all user_ids at a table."""
        return self._table_users.get(table_id, set()).copy()

    def is_connected(self, user_id: str) -> bool:
        """Check if a user is currently connected."""
        return user_id in self._connections
