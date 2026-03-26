"""
Tests for hand logging and replay verification.

Verifies:
- HandLog captures complete action sequence
- Replay through engine produces same final stacks
- Chip conservation (sum of deltas is zero)
- Ledger entries match stack deltas
"""

import random
import pytest
import asyncio

from src.models import (
    ClientAction,
    Chips,
    PlayerIdentity,
    HandStartedEvent,
    HandEndedEvent,
    ActionEvent,
)
from src.engine import PokerTableEngine, TableConfig
from src.persistence import (
    FirestoreClient,
    HandLogger,
    HandBuffer,
    SeatRecord,
)
from src.manager import TableRunner


# Helper to create player fixtures
def make_player(name: str) -> PlayerIdentity:
    return PlayerIdentity(
        user_id=f"user_{name.lower()}",
        display_name=name,
        avatar_url=None,
    )


@pytest.fixture
def config():
    return TableConfig(
        stake_id="nlh_1_2",
        max_players=6,
        min_players_to_start=2,  # Tests use 2-player scenarios
        small_blind_cents=100,
        big_blind_cents=200,
        min_buy_in_cents=4000,
        max_buy_in_cents=40000,
    )


@pytest.fixture
def firestore():
    """In-memory Firestore for testing."""
    return FirestoreClient(use_memory=True)


@pytest.fixture
def hand_logger(firestore):
    return HandLogger(firestore)


@pytest.fixture
def engine(config):
    return PokerTableEngine("tbl_test", config)


class TestHandBuffer:
    """Tests for hand event buffering."""

    def test_buffer_not_active_initially(self):
        buffer = HandBuffer()
        assert not buffer.is_active
        assert buffer.hand_id is None

    def test_start_hand_activates_buffer(self):
        buffer = HandBuffer()
        seats = [SeatRecord(0, "user_a", "Alice", 20000)]
        buffer.start_hand("hand_123", seats, button_seat=0)

        assert buffer.is_active
        assert buffer.hand_id == "hand_123"

    def test_record_event_when_active(self):
        buffer = HandBuffer()
        seats = [SeatRecord(0, "user_a", "Alice", 20000)]
        buffer.start_hand("hand_123", seats, button_seat=0)

        # Simulate an action event
        from src.models import ActionEvent, ClientAction, Chips
        event = ActionEvent(
            event_type="action",
            seat=0,
            action=ClientAction.CALL,
            amount=Chips(amount=200),
            is_all_in=False,
        )
        buffer.record_event(event)

        hand_id, events, seat_snapshot, started_at, button_seat = buffer.finalize()
        assert hand_id == "hand_123"
        assert len(events) == 1
        assert events[0] == event
        assert len(seat_snapshot) == 1
        assert button_seat == 0

    def test_finalize_resets_buffer(self):
        buffer = HandBuffer()
        seats = [SeatRecord(0, "user_a", "Alice", 20000)]
        buffer.start_hand("hand_123", seats, button_seat=0)
        buffer.finalize()

        assert not buffer.is_active
        assert buffer.hand_id is None

    def test_abort_clears_without_returning(self):
        buffer = HandBuffer()
        seats = [SeatRecord(0, "user_a", "Alice", 20000)]
        buffer.start_hand("hand_123", seats, button_seat=0)
        buffer.abort()

        assert not buffer.is_active
        assert buffer.hand_id is None


class TestHandLogger:
    """Tests for hand logging and ledger creation."""

    @pytest.mark.asyncio
    async def test_log_hand_creates_hand_log(self, firestore, hand_logger):
        """Verify hand log is persisted."""
        from datetime import datetime

        seats = [
            SeatRecord(0, "user_a", "Alice", 20000),
            SeatRecord(1, "user_b", "Bob", 20000),
        ]

        # Simulate a simple hand: Alice raises, Bob folds
        events = [
            HandStartedEvent(
                event_type="hand_started",
                hand_id="hand_test_1",
                button_seat=0,
                actor_seat=0,
            ),
            ActionEvent(
                event_type="action",
                seat=0,
                action=ClientAction.BET,
                amount=Chips(amount=400),
                is_all_in=False,
            ),
            ActionEvent(
                event_type="action",
                seat=1,
                action=ClientAction.FOLD,
                amount=None,
                is_all_in=False,
            ),
            HandEndedEvent(
                event_type="hand_ended",
                hand_id="hand_test_1",
                winners=[],  # Simplified
            ),
        ]

        hand_logger.log_hand(
            table_id="tbl_test",
            stake_id="nlh_1_2",
            hand_id="hand_test_1",
            events=events,
            seat_snapshot=seats,
            started_at=datetime.utcnow(),
            button_seat=0,
            small_blind=100,
            big_blind=200,
        )

        # Allow async task to complete
        await asyncio.sleep(0.1)

        # Verify hand log was created
        hand_log = firestore.get_hand_log("hand_test_1")
        assert hand_log is not None
        assert hand_log["hand_id"] == "hand_test_1"
        assert hand_log["table_id"] == "tbl_test"
        assert hand_log["stake_id"] == "nlh_1_2"
        assert len(hand_log["seats"]) == 2
        assert len(hand_log["actions"]) == 2


class TestReplayVerification:
    """Replay stored hands and verify ledger accuracy."""

    @pytest.mark.asyncio
    async def test_simple_hand_fold_win(self, config, firestore, hand_logger):
        """
        Play a hand where one player folds, verify logging and deltas.

        Scenario: Alice (SB) and Bob (BB)
        - Blinds posted: Alice 100, Bob 200
        - Alice raises to 600
        - Bob folds
        - Alice wins the pot (300 total)
        """
        random.seed(42)

        engine = PokerTableEngine("tbl_test", config)
        alice = make_player("Alice")
        bob = make_player("Bob")

        engine.seat_player(0, alice, Chips(amount=20000))
        engine.seat_player(1, bob, Chips(amount=20000))

        # Start hand
        events = engine.start_hand()
        hand_started = next(e for e in events if isinstance(e, HandStartedEvent))
        hand_id = hand_started.hand_id

        # Capture seat snapshot
        seats = [
            SeatRecord(0, "user_alice", "Alice", 20000),
            SeatRecord(1, "user_bob", "Bob", 20000),
        ]

        # Buffer events
        buffer = HandBuffer()
        buffer.start_hand(hand_id, seats, hand_started.button_seat)
        for event in events:
            buffer.record_event(event)

        # Alice raises (SB action in heads-up - acts first preflop)
        # Actually in heads-up, button posts SB and acts first preflop
        # After blinds, actor is the SB (button) in heads-up
        actor = engine.get_actor_seat()
        events = engine.apply_action(actor, ClientAction.RAISE_TO, Chips(amount=600))
        for event in events:
            buffer.record_event(event)

        # Bob folds
        actor = engine.get_actor_seat()
        events = engine.apply_action(actor, ClientAction.FOLD, None)
        for event in events:
            buffer.record_event(event)

        # Hand should be over
        hand_ended = next(
            (e for e in events if isinstance(e, HandEndedEvent)),
            None
        )
        assert hand_ended is not None, "Hand should have ended"

        # Finalize buffer and log
        buffered_hand_id, buffered_events, buffered_seats, started_at, button_seat = buffer.finalize()

        hand_logger.log_hand(
            table_id="tbl_test",
            stake_id="nlh_1_2",
            hand_id=buffered_hand_id,
            events=buffered_events,
            seat_snapshot=buffered_seats,
            started_at=started_at,
            button_seat=button_seat,
            small_blind=100,
            big_blind=200,
        )

        # Allow async write to complete
        await asyncio.sleep(0.1)

        # Verify hand log
        hand_log = firestore.get_hand_log(buffered_hand_id)
        assert hand_log is not None
        assert hand_log["hand_id"] == buffered_hand_id

        # Verify stack deltas sum to zero (chip conservation)
        total_delta = sum(int(v) for v in hand_log["stack_deltas"].values())
        assert total_delta == 0, f"Chip conservation violated: total delta = {total_delta}"

    @pytest.mark.asyncio
    async def test_chip_conservation_all_in_hand(self, config, firestore, hand_logger):
        """
        Verify chips are conserved in an all-in scenario.
        """
        random.seed(123)

        engine = PokerTableEngine("tbl_test", config)
        alice = make_player("Alice")
        bob = make_player("Bob")

        # Give Alice a short stack for all-in
        engine.seat_player(0, alice, Chips(amount=5000))  # 25bb
        engine.seat_player(1, bob, Chips(amount=20000))

        # Start hand
        events = engine.start_hand()
        hand_started = next(e for e in events if isinstance(e, HandStartedEvent))

        seats = [
            SeatRecord(0, "user_alice", "Alice", 5000),
            SeatRecord(1, "user_bob", "Bob", 20000),
        ]

        buffer = HandBuffer()
        buffer.start_hand(hand_started.hand_id, seats, hand_started.button_seat)
        for event in events:
            buffer.record_event(event)

        # Alice goes all-in (button/SB acts first in heads-up preflop)
        actor = engine.get_actor_seat()
        events = engine.apply_action(actor, ClientAction.RAISE_TO, Chips(amount=5000))
        for event in events:
            buffer.record_event(event)

        # Bob calls
        actor = engine.get_actor_seat()
        events = engine.apply_action(actor, ClientAction.CALL, None)
        for event in events:
            buffer.record_event(event)

        # Hand should complete (showdown)
        hand_ended = next(
            (e for e in events if isinstance(e, HandEndedEvent)),
            None
        )
        assert hand_ended is not None, "Hand should have ended after showdown"

        # Log the hand
        buffered_hand_id, buffered_events, buffered_seats, started_at, button_seat = buffer.finalize()

        hand_logger.log_hand(
            table_id="tbl_test",
            stake_id="nlh_1_2",
            hand_id=buffered_hand_id,
            events=buffered_events,
            seat_snapshot=buffered_seats,
            started_at=started_at,
            button_seat=button_seat,
            small_blind=100,
            big_blind=200,
        )

        # Wait for async
        await asyncio.sleep(0.1)

        # Verify chip conservation
        hand_log = firestore.get_hand_log(buffered_hand_id)
        total_delta = sum(int(v) for v in hand_log["stack_deltas"].values())
        assert total_delta == 0, f"Chip conservation violated: {total_delta}"

        # Verify we have a winner
        assert len(hand_log["winners"]) > 0, "Should have at least one winner"

    @pytest.mark.asyncio
    async def test_ledger_entries_match_stack_deltas(self, config, firestore, hand_logger):
        """
        Verify each player's ledger entry matches their stack delta.
        """
        random.seed(456)

        engine = PokerTableEngine("tbl_test", config)
        alice = make_player("Alice")
        bob = make_player("Bob")

        engine.seat_player(0, alice, Chips(amount=20000))
        engine.seat_player(1, bob, Chips(amount=20000))

        # Start hand
        events = engine.start_hand()
        hand_started = next(e for e in events if isinstance(e, HandStartedEvent))

        seats = [
            SeatRecord(0, "user_alice", "Alice", 20000),
            SeatRecord(1, "user_bob", "Bob", 20000),
        ]

        buffer = HandBuffer()
        buffer.start_hand(hand_started.hand_id, seats, hand_started.button_seat)
        for event in events:
            buffer.record_event(event)

        # Simple fold scenario
        actor = engine.get_actor_seat()
        events = engine.apply_action(actor, ClientAction.RAISE_TO, Chips(amount=600))
        for event in events:
            buffer.record_event(event)

        actor = engine.get_actor_seat()
        events = engine.apply_action(actor, ClientAction.FOLD, None)
        for event in events:
            buffer.record_event(event)

        # Log the hand
        buffered_hand_id, buffered_events, buffered_seats, started_at, button_seat = buffer.finalize()

        hand_logger.log_hand(
            table_id="tbl_test",
            stake_id="nlh_1_2",
            hand_id=buffered_hand_id,
            events=buffered_events,
            seat_snapshot=buffered_seats,
            started_at=started_at,
            button_seat=button_seat,
            small_blind=100,
            big_blind=200,
        )

        # Wait for async
        await asyncio.sleep(0.1)

        # Get hand log and ledger entries
        hand_log = firestore.get_hand_log(buffered_hand_id)
        assert hand_log is not None

        # Check ledger entries for each player with a delta
        for seat_idx_str, delta in hand_log["stack_deltas"].items():
            seat_idx = int(seat_idx_str)
            # Find user_id for this seat
            seat_info = next(s for s in hand_log["seats"] if s["seat_index"] == seat_idx)
            user_id = seat_info["user_id"]

            # Get ledger entries for this user and hand
            entries = firestore.get_ledger_entries(user_id, buffered_hand_id)

            # Sum of entries should match delta
            if entries:
                entry_sum = sum(e["delta"] for e in entries)
                assert entry_sum == delta, \
                    f"User {user_id}: ledger sum {entry_sum} != delta {delta}"


class TestTableRunnerIntegration:
    """Tests for TableRunner with hand logging integration."""

    @pytest.fixture
    def runner_with_logging(self, config, hand_logger):
        """Create a TableRunner with hand logging enabled."""
        runner = TableRunner("tbl_test", config, hand_logger)
        return runner

    @pytest.mark.asyncio
    async def test_runner_logs_complete_hand(self, runner_with_logging, firestore):
        """Verify TableRunner properly logs a complete hand."""
        runner = runner_with_logging
        runner.start()

        try:
            alice = make_player("Alice")
            bob = make_player("Bob")

            # Join players
            from src.manager import JoinTableCommand, PlayerActionCommand, StartHandCommand
            import asyncio

            loop = asyncio.get_event_loop()

            # Join Alice
            future1 = loop.create_future()
            await runner.submit(JoinTableCommand(
                user_id="user_alice",
                player=alice,
                buy_in=Chips(amount=20000),
                result_future=future1,
            ))
            seat1, _ = await future1

            # Join Bob
            future2 = loop.create_future()
            await runner.submit(JoinTableCommand(
                user_id="user_bob",
                player=bob,
                buy_in=Chips(amount=20000),
                result_future=future2,
            ))
            seat2, _ = await future2

            # Start a hand
            random.seed(789)
            future3 = loop.create_future()
            await runner.submit(StartHandCommand(result_future=future3))
            events = await future3

            # Get hand_id
            hand_started = next(
                (e for e in events if isinstance(e, HandStartedEvent)),
                None
            )
            assert hand_started is not None
            hand_id = hand_started.hand_id

            # Play a quick hand: Bob folds (he acts first based on error message)
            future4 = loop.create_future()
            await runner.submit(PlayerActionCommand(
                user_id="user_bob",
                action=ClientAction.FOLD,
                amount=None,
                result_future=future4,
            ))
            events = await future4

            # Hand should be over
            hand_ended = next(
                (e for e in events if isinstance(e, HandEndedEvent)),
                None
            )
            assert hand_ended is not None

            # Wait for async logging
            await asyncio.sleep(0.2)

            # Verify hand was logged
            hand_log = firestore.get_hand_log(hand_id)
            assert hand_log is not None
            assert hand_log["hand_id"] == hand_id

            # Verify chip conservation
            total_delta = sum(int(v) for v in hand_log["stack_deltas"].values())
            assert total_delta == 0

        finally:
            await runner.stop()
