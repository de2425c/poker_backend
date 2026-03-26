"""
TableManager - Orchestrates multiple poker tables.

Routes players to tables, tracks user->table mapping,
and forwards commands to the appropriate TableRunner.
"""

import asyncio
from typing import Optional, TYPE_CHECKING

from ..engine import TableConfig
from ..models import PlayerIdentity, Chips, ClientAction, generate_table_id
from .runner import TableRunner
from .commands import (
    JoinTableCommand,
    LeaveTableCommand,
    PlayerActionCommand,
    StartHandCommand,
    GetSnapshotCommand,
    GetActionRequestCommand,
)

if TYPE_CHECKING:
    from ..persistence import HandLogger, FirestoreClient


class TableManager:
    """
    Orchestrates multiple poker tables.

    Routes players to tables, tracks user->table mapping,
    and forwards commands to the appropriate TableRunner.
    """

    def __init__(
        self,
        hand_logger: Optional["HandLogger"] = None,
        firestore: Optional["FirestoreClient"] = None,
    ):
        self._tables: dict[str, TableRunner] = {}  # table_id -> runner
        self._user_tables: dict[str, str] = {}     # user_id -> table_id
        self._hand_logger = hand_logger
        self._firestore = firestore
        self._stake_configs: dict[str, TableConfig] = {
            "nlh_1_2": TableConfig(
                stake_id="nlh_1_2",
                small_blind_cents=100,
                big_blind_cents=200,
                min_buy_in_cents=4000,
                max_buy_in_cents=40000,
                min_players_to_start=2,  # Allow 2-player games on 6-max
            ),
            "nlh_1_2_hu": TableConfig(
                stake_id="nlh_1_2_hu",
                small_blind_cents=100,
                big_blind_cents=200,
                min_buy_in_cents=4000,
                max_buy_in_cents=40000,
                max_players=2,
                min_players_to_start=2,
            ),
        }

    def create_table(self, stake_id: str) -> str:
        """Create a new table for a stake level."""
        config = self._stake_configs.get(stake_id)
        if config is None:
            raise ValueError(f"Unknown stake: {stake_id}")

        table_id = generate_table_id()

        runner = TableRunner(table_id, config, self._hand_logger)
        runner.start()
        self._tables[table_id] = runner

        return table_id

    async def add_player(
        self,
        user_id: str,
        stake_id: str,
        buy_in: Chips,
        player: PlayerIdentity,
        table_id: Optional[str] = None,
    ) -> tuple[str, int]:
        """
        Add a player to a table at the given stake level.

        Returns (table_id, seat).
        If table_id is provided, joins that specific table.
        Otherwise finds existing table with open seats or creates new one.
        """
        if user_id in self._user_tables:
            raise ValueError("User already at a table")

        # Check and deduct balance for non-bot players
        if self._firestore and not user_id.startswith(("bot_", "user_bot_")):
            balance = await self._firestore.get_user_balance(user_id)
            if balance < buy_in.amount:
                raise ValueError(
                    f"INSUFFICIENT_BALANCE: {balance} cents < {buy_in.amount} cents"
                )
            await self._firestore.deduct_balance(user_id, buy_in.amount)

        if table_id:
            runner = self._tables.get(table_id)
            if runner is None:
                raise ValueError(f"Table {table_id} not found")
        else:
            # Find or create table
            runner = self._find_table_with_seats(stake_id)
            if runner is None:
                table_id = self.create_table(stake_id)
                runner = self._tables[table_id]

        # Submit join command
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        await runner.submit(JoinTableCommand(
            user_id=user_id,
            player=player,
            buy_in=buy_in,
            result_future=future,
        ))

        seat, snapshot = await future
        self._user_tables[user_id] = runner.table_id

        return (runner.table_id, seat)

    async def remove_player(self, user_id: str) -> Chips:
        """Remove a player from their table. Returns final chips."""
        table_id = self._user_tables.get(user_id)
        if table_id is None:
            raise ValueError("User not at any table")

        runner = self._tables[table_id]

        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        await runner.submit(LeaveTableCommand(
            user_id=user_id,
            result_future=future,
        ))

        chips = await future
        del self._user_tables[user_id]

        # Clean up table if no human players remain
        if not runner.has_human_players():
            print(f"[TABLE] Cleaning up empty table {table_id}")
            await runner.stop()
            del self._tables[table_id]
            # Also remove any remaining bots from user_tables
            bot_users = [uid for uid, tid in self._user_tables.items() if tid == table_id]
            for bot_id in bot_users:
                del self._user_tables[bot_id]

        return chips

    async def route_action(
        self,
        user_id: str,
        hand_id: str,
        action: ClientAction,
        amount: Optional[Chips] = None
    ) -> list:
        """Route a player action to their table. Returns events."""
        table_id = self._user_tables.get(user_id)
        if table_id is None:
            raise ValueError("User not at any table")

        runner = self._tables[table_id]

        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        await runner.submit(PlayerActionCommand(
            user_id=user_id,
            hand_id=hand_id,
            action=action,
            amount=amount,
            result_future=future,
        ))

        return await future

    async def start_hand(self, table_id: str) -> list:
        """Start a new hand at a table. Returns events."""
        runner = self._tables.get(table_id)
        if runner is None:
            raise ValueError("Table not found")

        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        await runner.submit(StartHandCommand(result_future=future))

        return await future

    def get_table_for_user(self, user_id: str) -> Optional[str]:
        """Get the table ID for a user, or None if not seated."""
        return self._user_tables.get(user_id)

    async def get_snapshot(self, user_id: str):
        """Get table snapshot for a user."""
        table_id = self._user_tables.get(user_id)
        if table_id is None:
            raise ValueError("User not at any table")

        runner = self._tables[table_id]

        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        await runner.submit(GetSnapshotCommand(
            user_id=user_id,
            result_future=future,
        ))

        return await future

    async def get_action_request(self, user_id: str):
        """Get action request for a user who needs to act."""
        table_id = self._user_tables.get(user_id)
        if table_id is None:
            raise ValueError("User not at any table")

        runner = self._tables[table_id]

        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        await runner.submit(GetActionRequestCommand(
            user_id=user_id,
            result_future=future,
        ))

        return await future

    def _find_table_with_seats(self, stake_id: str) -> Optional[TableRunner]:
        """Find an existing table with open seats for the stake level."""
        for runner in self._tables.values():
            if runner.has_open_seats():
                # TODO: Check stake_id matches (need to track stake per table)
                return runner
        return None

    async def try_rebuy(self, user_id: str, table_id: str, seat: int) -> Optional[tuple[int, int]]:
        """
        Attempt auto-rebuy for a bust player.

        Tops up TO $200 (20000 cents), not adds $200.
        Returns (rebuy_amount, new_stack) or None if rebuy failed.
        """
        # Skip bots - they don't need real chip balance
        if not self._firestore or user_id.startswith(("bot_", "user_bot_")):
            return None

        runner = self._tables.get(table_id)
        if not runner:
            return None

        seat_state = runner._engine._seats[seat]
        if not seat_state:
            return None

        # Calculate amount needed to reach $200 (20000 cents)
        max_stack = 20000
        current_chips = seat_state.chips
        if current_chips >= max_stack:
            return None  # Already at or above max

        rebuy_amount = max_stack - current_chips

        try:
            balance = await self._firestore.get_user_balance(user_id)
            if balance < rebuy_amount:
                return None

            await self._firestore.deduct_balance(user_id, rebuy_amount)
            seat_state.chips = max_stack
            print(f"[REBUY] Topped up {user_id} by {rebuy_amount} cents to {max_stack}")
            return (rebuy_amount, max_stack)
        except Exception as e:
            print(f"[REBUY] Failed for {user_id}: {e}")
            return None

    async def request_topup(self, user_id: str) -> tuple[int, int]:
        """
        Request a manual top-up (queued for next hand start).

        Returns (topup_amount, projected_new_stack).
        Raises ValueError if user not at table, already at max, or insufficient balance.
        """
        table_id = self._user_tables.get(user_id)
        if table_id is None:
            raise ValueError("User not at any table")

        runner = self._tables.get(table_id)
        if not runner:
            raise ValueError("Table not found")

        # Find user's seat
        seat_state = None
        for seat in runner._engine._seats:
            if seat and seat.player and seat.player.user_id == user_id:
                seat_state = seat
                break

        if seat_state is None:
            raise ValueError("User not seated at table")

        # Calculate top-up amount: bring stack + pending back to 20000 ($200)
        max_stack = 20000
        current_effective = seat_state.chips + seat_state.pending_topup
        if current_effective >= max_stack:
            raise ValueError("Already at maximum stack")

        topup_amount = max_stack - current_effective

        # Check and deduct wallet balance
        if self._firestore and not user_id.startswith(("bot_", "user_bot_")):
            balance = await self._firestore.get_user_balance(user_id)
            if balance < topup_amount:
                raise ValueError(f"INSUFFICIENT_BALANCE: {balance} cents < {topup_amount} cents")
            await self._firestore.deduct_balance(user_id, topup_amount)

        # Queue the pending top-up (applied at next hand start)
        seat_state.pending_topup += topup_amount
        new_stack = seat_state.chips + seat_state.pending_topup

        print(f"[TOPUP] Queued {topup_amount} cents for {user_id}, new projected stack: {new_stack}")
        return (topup_amount, new_stack)

    async def shutdown(self) -> None:
        """Stop all table runners."""
        for runner in self._tables.values():
            await runner.stop()
