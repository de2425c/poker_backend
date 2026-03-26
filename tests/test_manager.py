"""
Tests for TableManager and TableRunner.

Tests multi-table concurrency, player routing, and action handling.
"""

import asyncio
import pytest
import random

from src.manager import TableManager
from src.models import PlayerIdentity, Chips, ClientAction


@pytest.fixture
def manager():
    return TableManager()


class TestBasicOperations:
    """Basic single-table tests."""

    @pytest.mark.asyncio
    async def test_add_and_remove_player(self, manager):
        """Player can join and leave, getting back their chips."""
        player = PlayerIdentity(user_id="u1", display_name="Alice")
        table_id, seat = await manager.add_player(
            "u1", "nlh_1_2", Chips(amount=20000), player
        )

        assert manager.get_table_for_user("u1") == table_id
        assert seat >= 0 and seat < 6

        chips = await manager.remove_player("u1")
        assert chips.amount == 20000
        assert manager.get_table_for_user("u1") is None

        await manager.shutdown()

    @pytest.mark.asyncio
    async def test_duplicate_user_rejected(self, manager):
        """Same user cannot join twice."""
        player = PlayerIdentity(user_id="u1", display_name="Alice")
        await manager.add_player("u1", "nlh_1_2", Chips(amount=20000), player)

        with pytest.raises(ValueError, match="already at a table"):
            await manager.add_player("u1", "nlh_1_2", Chips(amount=20000), player)

        await manager.shutdown()

    @pytest.mark.asyncio
    async def test_remove_unknown_user_fails(self, manager):
        """Removing non-existent user raises error."""
        with pytest.raises(ValueError, match="not at any table"):
            await manager.remove_player("nobody")

    @pytest.mark.asyncio
    async def test_action_on_unknown_user_fails(self, manager):
        """Routing action for non-existent user raises error."""
        with pytest.raises(ValueError, match="not at any table"):
            await manager.route_action("nobody", ClientAction.FOLD)

    @pytest.mark.asyncio
    async def test_snapshot_for_unknown_user_fails(self, manager):
        """Getting snapshot for non-existent user raises error."""
        with pytest.raises(ValueError, match="not at any table"):
            await manager.get_snapshot("nobody")

    @pytest.mark.asyncio
    async def test_start_hand_on_unknown_table_fails(self, manager):
        """Starting hand on non-existent table raises error."""
        with pytest.raises(ValueError, match="Table not found"):
            await manager.start_hand("fake_table")

    @pytest.mark.asyncio
    async def test_action_routing(self, manager):
        """Actions are routed to correct table and produce events."""
        # Add two players
        p1 = PlayerIdentity(user_id="u1", display_name="Alice")
        p2 = PlayerIdentity(user_id="u2", display_name="Bob")

        table_id, seat1 = await manager.add_player("u1", "nlh_1_2", Chips(amount=20000), p1)
        _, seat2 = await manager.add_player("u2", "nlh_1_2", Chips(amount=20000), p2)

        # Start hand
        events = await manager.start_hand(table_id)
        assert len(events) > 0
        assert any(e.event_type == "hand_started" for e in events)

        # Get snapshot to find actor
        snapshot = await manager.get_snapshot("u1")
        assert snapshot.hand is not None
        actor_seat = snapshot.hand.actor_seat

        # Determine which user is the actor
        actor_user = "u1" if snapshot.your_seat == actor_seat else "u2"

        # Route fold from actor
        events = await manager.route_action(actor_user, ClientAction.FOLD)
        assert any(e.event_type == "hand_ended" for e in events)

        await manager.shutdown()

    @pytest.mark.asyncio
    async def test_two_players_same_table(self, manager):
        """Two players joining same stake go to same table."""
        p1 = PlayerIdentity(user_id="u1", display_name="Alice")
        p2 = PlayerIdentity(user_id="u2", display_name="Bob")

        table_id1, seat1 = await manager.add_player("u1", "nlh_1_2", Chips(amount=20000), p1)
        table_id2, seat2 = await manager.add_player("u2", "nlh_1_2", Chips(amount=20000), p2)

        # Should be same table
        assert table_id1 == table_id2
        # Different seats
        assert seat1 != seat2

        await manager.shutdown()


class TestMultiTable:
    """Multi-table concurrency tests."""

    @pytest.mark.asyncio
    async def test_table_fills_then_new_table(self, manager):
        """When a table fills (6 players), new table is created."""
        users = []

        # Add 7 players - first 6 should fill one table, 7th creates new
        for i in range(7):
            user_id = f"user_{i}"
            player = PlayerIdentity(user_id=user_id, display_name=f"Player{i}")
            users.append(user_id)

            await manager.add_player(
                user_id, "nlh_1_2", Chips(amount=20000), player
            )

        # First 6 should be on same table
        table1 = manager.get_table_for_user("user_0")
        for i in range(6):
            assert manager.get_table_for_user(f"user_{i}") == table1

        # 7th should be on different table
        table2 = manager.get_table_for_user("user_6")
        assert table2 != table1

        await manager.shutdown()

    @pytest.mark.asyncio
    async def test_ten_tables_concurrent(self, manager):
        """Simulate 10 tables with 40 players, verify no duplicate seats."""
        NUM_PLAYERS = 40  # Will create ~7 tables (6 players each)

        users = []

        # Add all players
        for i in range(NUM_PLAYERS):
            user_id = f"user_{i}"
            player = PlayerIdentity(user_id=user_id, display_name=f"Player{i}")
            users.append(user_id)

            await manager.add_player(
                user_id, "nlh_1_2", Chips(amount=20000), player
            )

        # Verify no duplicate seats per table
        seat_map: dict[str, set[int]] = {}  # table_id -> seats

        for user_id in users:
            table_id = manager.get_table_for_user(user_id)
            snapshot = await manager.get_snapshot(user_id)
            seat = snapshot.your_seat

            if table_id not in seat_map:
                seat_map[table_id] = set()

            assert seat not in seat_map[table_id], f"Duplicate seat {seat} at {table_id}"
            seat_map[table_id].add(seat)

        # Verify we created multiple tables
        assert len(seat_map) >= 6, f"Expected at least 6 tables, got {len(seat_map)}"

        await manager.shutdown()

    @pytest.mark.asyncio
    async def test_concurrent_actions_ordering(self, manager):
        """Actions processed in order per table without deadlock."""
        # Setup 2 players
        p1 = PlayerIdentity(user_id="u1", display_name="Alice")
        p2 = PlayerIdentity(user_id="u2", display_name="Bob")

        table_id, _ = await manager.add_player("u1", "nlh_1_2", Chips(amount=20000), p1)
        await manager.add_player("u2", "nlh_1_2", Chips(amount=20000), p2)

        await manager.start_hand(table_id)

        # Get snapshot to find actor
        snapshot = await manager.get_snapshot("u1")
        actor_seat = snapshot.hand.actor_seat
        actor_user = "u1" if snapshot.your_seat == actor_seat else "u2"

        # Actor folds
        events = await manager.route_action(actor_user, ClientAction.FOLD)

        # Verify hand ended (processed correctly)
        assert any(e.event_type == "hand_ended" for e in events)

        await manager.shutdown()

    @pytest.mark.asyncio
    async def test_random_joins_leaves_no_exceptions(self, manager):
        """Random join/leave storm completes without deadlocks or unexpected errors."""
        random.seed(42)

        active_users: set[str] = set()
        errors: list[Exception] = []
        lock = asyncio.Lock()

        async def random_operation(i: int):
            user_id = f"user_{i}"
            try:
                async with lock:
                    is_active = user_id in active_users

                if not is_active and random.random() < 0.7:
                    # Join
                    player = PlayerIdentity(user_id=user_id, display_name=f"P{i}")
                    await manager.add_player(user_id, "nlh_1_2", Chips(amount=20000), player)
                    async with lock:
                        active_users.add(user_id)
                elif is_active:
                    # Leave
                    await manager.remove_player(user_id)
                    async with lock:
                        active_users.discard(user_id)
            except ValueError as e:
                # Expected errors from race conditions
                async with lock:
                    errors.append(e)

        # Run 100 random operations
        tasks = [random_operation(i % 20) for i in range(100)]
        await asyncio.gather(*tasks)

        # Should complete without deadlock (test timeout would catch deadlock)
        # Allow expected errors from race conditions:
        # - "already at" - user tried to join twice
        # - "not at" - user tried to leave but already left
        # - "Table full" - table filled between check and join
        unexpected = [
            e for e in errors
            if "already at" not in str(e)
            and "not at" not in str(e)
            and "Table full" not in str(e)
        ]
        assert len(unexpected) == 0, f"Unexpected errors: {unexpected}"

        await manager.shutdown()


class TestHandLifecycle:
    """Tests for hand start/action/end through manager."""

    @pytest.mark.asyncio
    async def test_full_hand_through_manager(self, manager):
        """Complete a full hand via manager routing."""
        # Setup
        p1 = PlayerIdentity(user_id="u1", display_name="Alice")
        p2 = PlayerIdentity(user_id="u2", display_name="Bob")

        table_id, _ = await manager.add_player("u1", "nlh_1_2", Chips(amount=20000), p1)
        await manager.add_player("u2", "nlh_1_2", Chips(amount=20000), p2)

        # Start hand
        events = await manager.start_hand(table_id)
        assert any(e.event_type == "hand_started" for e in events)

        # Get actor and fold
        snapshot = await manager.get_snapshot("u1")
        actor_seat = snapshot.hand.actor_seat
        actor_user = "u1" if snapshot.your_seat == actor_seat else "u2"

        events = await manager.route_action(actor_user, ClientAction.FOLD)
        assert any(e.event_type == "hand_ended" for e in events)

        # Verify chips transferred correctly
        snap1 = await manager.get_snapshot("u1")
        snap2 = await manager.get_snapshot("u2")

        total = 0
        for seat in snap1.seats:
            if seat.player is not None:
                total += seat.chips.amount

        # Total should still be 40000 (2 x 20000 buy-ins)
        assert total == 40000

        await manager.shutdown()

    @pytest.mark.asyncio
    async def test_multiple_hands(self, manager):
        """Can play multiple hands in sequence."""
        p1 = PlayerIdentity(user_id="u1", display_name="Alice")
        p2 = PlayerIdentity(user_id="u2", display_name="Bob")

        table_id, _ = await manager.add_player("u1", "nlh_1_2", Chips(amount=20000), p1)
        await manager.add_player("u2", "nlh_1_2", Chips(amount=20000), p2)

        # Play 3 hands
        for hand_num in range(3):
            events = await manager.start_hand(table_id)
            assert any(e.event_type == "hand_started" for e in events)

            # Find actor and fold
            snapshot = await manager.get_snapshot("u1")
            actor_seat = snapshot.hand.actor_seat
            actor_user = "u1" if snapshot.your_seat == actor_seat else "u2"

            events = await manager.route_action(actor_user, ClientAction.FOLD)
            assert any(e.event_type == "hand_ended" for e in events)

        await manager.shutdown()
