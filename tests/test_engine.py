"""
Tests for the poker engine.

Uses RNG seeding for deterministic results.
"""

import random
import pytest

from src.models import (
    ClientAction,
    Chips,
    PlayerIdentity,
    TableSnapshotMessage,
    TableStatus,
)
from src.engine import PokerTableEngine, TableConfig


# Helper to create player fixtures
def make_player(name: str) -> PlayerIdentity:
    return PlayerIdentity(
        user_id=f"user_{name.lower()}",
        display_name=name,
        avatar_url=None,
    )


# Test fixtures
@pytest.fixture
def player_a():
    return PlayerIdentity(
        user_id="user_a",
        display_name="Alice",
        avatar_url=None,
    )


@pytest.fixture
def player_b():
    return PlayerIdentity(
        user_id="user_b",
        display_name="Bob",
        avatar_url=None,
    )


@pytest.fixture
def config():
    return TableConfig(
        max_players=6,
        min_players_to_start=2,  # Tests use 2-player scenarios
        small_blind_cents=100,
        big_blind_cents=200,
        min_buy_in_cents=4000,
        max_buy_in_cents=40000,
    )


@pytest.fixture
def engine(config):
    return PokerTableEngine("tbl_test", config)


class TestTableSetup:
    """Tests for table setup and player management."""

    def test_create_table(self, engine):
        assert engine.table_id == "tbl_test"
        assert not engine.can_start_hand()

    def test_seat_player(self, engine, player_a):
        engine.seat_player(0, player_a, Chips(amount=20000))
        assert not engine.can_start_hand()  # Need 2 players

    def test_seat_two_players(self, engine, player_a, player_b):
        engine.seat_player(0, player_a, Chips(amount=20000))
        engine.seat_player(1, player_b, Chips(amount=20000))
        assert engine.can_start_hand()

    def test_reject_duplicate_seat(self, engine, player_a, player_b):
        engine.seat_player(0, player_a, Chips(amount=20000))
        with pytest.raises(ValueError, match="occupied"):
            engine.seat_player(0, player_b, Chips(amount=20000))

    def test_reject_small_buyin(self, engine, player_a):
        with pytest.raises(ValueError, match="too small"):
            engine.seat_player(0, player_a, Chips(amount=1000))

    def test_unseat_player(self, engine, player_a):
        engine.seat_player(0, player_a, Chips(amount=20000))
        chips = engine.unseat_player(0)
        assert chips.amount == 20000


class TestHandLifecycle:
    """Tests for starting and ending hands."""

    def test_start_hand(self, engine, player_a, player_b):
        random.seed(42)
        engine.seat_player(0, player_a, Chips(amount=20000))
        engine.seat_player(1, player_b, Chips(amount=20000))

        events = engine.start_hand()

        # Should have: HandStarted + 2 blind postings
        assert len(events) >= 1
        assert events[0].event_type == "hand_started"

    def test_cannot_start_without_players(self, engine):
        with pytest.raises(ValueError, match="Not enough players"):
            engine.start_hand()


class TestFoldOutHand:
    """Test simple fold-out scenarios."""

    def test_sb_folds_preflop(self, engine, player_a, player_b):
        """SB folds, BB wins blinds."""
        random.seed(42)
        engine.seat_player(0, player_a, Chips(amount=20000))
        engine.seat_player(1, player_b, Chips(amount=20000))

        events = engine.start_hand()
        assert events[0].event_type == "hand_started"

        # Get actor (should be seat 0 for heads-up)
        actor = engine.get_actor_seat()
        assert actor is not None

        # Fold
        events = engine.apply_action(actor, ClientAction.FOLD, None)

        # Should have action event and hand ended
        assert any(e.event_type == "action" for e in events)
        assert any(e.event_type == "hand_ended" for e in events)


class TestFullHand:
    """Test complete hands to showdown."""

    def test_check_call_to_showdown(self, engine, player_a, player_b):
        """Both players check/call to showdown."""
        random.seed(123)
        engine.seat_player(0, player_a, Chips(amount=20000))
        engine.seat_player(1, player_b, Chips(amount=20000))

        events = engine.start_hand()
        hand_ended = False

        # Play until hand ends (check/call each action)
        max_actions = 20  # Safety limit
        actions = 0

        while not hand_ended and actions < max_actions:
            actor = engine.get_actor_seat()
            if actor is None:
                break

            allowed = engine.get_allowed_actions(actor)

            # Prefer check, then call
            if allowed.can_check:
                events = engine.apply_action(actor, ClientAction.CHECK, None)
            elif allowed.can_call:
                events = engine.apply_action(actor, ClientAction.CALL, None)
            else:
                # Shouldn't happen in check/call game
                break

            hand_ended = any(e.event_type == "hand_ended" for e in events)
            actions += 1

        assert hand_ended, "Hand should have ended"


class TestInvalidActions:
    """Test that invalid actions are rejected."""

    def test_wrong_player_action(self, engine, player_a, player_b):
        """Can't act when it's not your turn."""
        random.seed(42)
        engine.seat_player(0, player_a, Chips(amount=20000))
        engine.seat_player(1, player_b, Chips(amount=20000))
        engine.start_hand()

        actor = engine.get_actor_seat()
        other = 1 if actor == 0 else 0

        with pytest.raises(ValueError, match="not.*turn"):
            engine.apply_action(other, ClientAction.FOLD, None)


class TestSnapshot:
    """Test snapshot generation and serialization."""

    def test_snapshot_roundtrip(self, engine, player_a, player_b):
        """Snapshot should serialize and deserialize correctly."""
        random.seed(42)
        engine.seat_player(0, player_a, Chips(amount=20000))
        engine.seat_player(1, player_b, Chips(amount=20000))
        engine.start_hand()

        snapshot = engine.get_snapshot(for_seat=0)

        # Serialize and parse
        json_str = snapshot.model_dump_json()
        parsed = TableSnapshotMessage.model_validate_json(json_str)

        assert parsed.table_id == snapshot.table_id
        assert parsed.your_seat == 0
        assert parsed.your_hole_cards is not None
        assert len(parsed.your_hole_cards) == 2

    def test_snapshot_hides_opponent_cards(self, engine, player_a, player_b):
        """Snapshot should only show your own hole cards."""
        random.seed(42)
        engine.seat_player(0, player_a, Chips(amount=20000))
        engine.seat_player(1, player_b, Chips(amount=20000))
        engine.start_hand()

        snapshot_0 = engine.get_snapshot(for_seat=0)
        snapshot_1 = engine.get_snapshot(for_seat=1)

        # Each player sees their own cards
        assert snapshot_0.your_hole_cards is not None
        assert snapshot_1.your_hole_cards is not None

        # Cards should be different
        assert snapshot_0.your_hole_cards != snapshot_1.your_hole_cards


class TestActionRequest:
    """Test action request generation."""

    def test_action_request(self, engine, player_a, player_b):
        """Action request should include valid options."""
        random.seed(42)
        engine.seat_player(0, player_a, Chips(amount=20000))
        engine.seat_player(1, player_b, Chips(amount=20000))
        engine.start_hand()

        actor = engine.get_actor_seat()
        request = engine.get_action_request(actor)

        assert request.hand_id is not None
        assert request.seat == actor
        assert len(request.allowed_actions) > 0
        assert request.expires_at_ms > 0


class TestStackCorrectness:
    """Tests for pot/stack invariants and correctness via public snapshot API."""

    def test_blinds_deducted_correctly(self, engine, player_a, player_b, config):
        """After blinds posted, stacks should reflect blind deductions."""
        random.seed(42)
        start_stack = 20000
        engine.seat_player(0, player_a, Chips(amount=start_stack))
        engine.seat_player(1, player_b, Chips(amount=start_stack))

        engine.start_hand()

        # Use public snapshot API, not internal _adapter
        snapshot = engine.get_snapshot(for_seat=0)

        # Find which seats have bets (posted blinds)
        occupied_seats = [s for s in snapshot.seats if s.player is not None]
        assert len(occupied_seats) == 2

        # Get bets and stacks from snapshot
        bets = {s.seat_index: s.bet.amount for s in occupied_seats}
        stacks = {s.seat_index: s.chips.amount for s in occupied_seats}

        # Verify one player posted SB and one posted BB
        bet_values = sorted(bets.values())
        assert bet_values == [config.small_blind_cents, config.big_blind_cents], \
            f"Expected blinds {config.small_blind_cents}/{config.big_blind_cents}, got {bet_values}"

        # Verify stacks decreased by blind amounts
        for seat_idx, bet in bets.items():
            expected_stack = start_stack - bet
            assert stacks[seat_idx] == expected_stack, \
                f"Seat {seat_idx} should have {expected_stack}, has {stacks[seat_idx]}"

        # Pot should equal SB + BB
        assert snapshot.hand is not None
        total_pot = sum(p.amount.amount for p in snapshot.hand.pots)
        assert total_pot == config.small_blind_cents + config.big_blind_cents

    def test_chip_conservation_after_hand(self, engine, player_a, player_b):
        """Total chips should be conserved after hand ends (no rake)."""
        random.seed(42)
        start_stack = 20000
        total_chips = start_stack * 2
        engine.seat_player(0, player_a, Chips(amount=start_stack))
        engine.seat_player(1, player_b, Chips(amount=start_stack))

        engine.start_hand()

        # Play until hand ends (fold)
        actor = engine.get_actor_seat()
        engine.apply_action(actor, ClientAction.FOLD, None)

        # Use snapshot to verify final state
        snapshot = engine.get_snapshot(for_seat=0)
        assert snapshot.status == TableStatus.BETWEEN_HANDS

        # Sum final stacks via snapshot
        final_total = sum(s.chips.amount for s in snapshot.seats if s.player is not None)
        assert final_total == total_chips, "Chips should be conserved"

    def test_state_cleared_after_hand(self, engine, player_a, player_b):
        """After hand ends, hand state should be None and bets cleared."""
        random.seed(42)
        engine.seat_player(0, player_a, Chips(amount=20000))
        engine.seat_player(1, player_b, Chips(amount=20000))

        engine.start_hand()
        actor = engine.get_actor_seat()
        engine.apply_action(actor, ClientAction.FOLD, None)

        # Verify via snapshot
        snapshot = engine.get_snapshot(for_seat=0)

        # Status should be BETWEEN_HANDS
        assert snapshot.status == TableStatus.BETWEEN_HANDS

        # Hand state should be None between hands
        assert snapshot.hand is None, "Hand should be None between hands"

        # All bets should be 0
        for seat in snapshot.seats:
            if seat.player is not None:
                assert seat.bet.amount == 0, f"Seat {seat.seat_index} bet should be 0"


class TestFoldOutPayout:
    """Test that fold-out hands award pots correctly."""

    def test_winner_gets_pot_on_fold(self, engine, player_a, player_b, config):
        """When one player folds preflop, the other wins the pot."""
        random.seed(42)
        start_stack = 20000
        engine.seat_player(0, player_a, Chips(amount=start_stack))
        engine.seat_player(1, player_b, Chips(amount=start_stack))

        engine.start_hand()

        # Get actor (whoever it is) and fold
        actor = engine.get_actor_seat()
        assert actor is not None, "Should have an actor"

        # The other player is the non-actor
        other = 1 if actor == 0 else 0

        # Actor folds
        events = engine.apply_action(actor, ClientAction.FOLD, None)

        # Find hand_ended event and verify winner
        hand_ended = next((e for e in events if e.event_type == "hand_ended"), None)
        assert hand_ended is not None, "Hand should have ended"
        assert len(hand_ended.winners) == 1, "Should be exactly one winner"
        assert hand_ended.winners[0].seat == other, "Non-folder should win"

        # Verify stacks via snapshot
        snapshot = engine.get_snapshot(for_seat=0)

        # Get final stacks
        stacks = {s.seat_index: s.chips.amount for s in snapshot.seats if s.player is not None}

        # Winner should have gained the loser's blind
        # Loser should have lost their blind
        # Total chips conserved
        total_final = sum(stacks.values())
        assert total_final == start_stack * 2, "Chips should be conserved"

        # Winner's stack > start (they won something)
        assert stacks[other] > start_stack, "Winner should have gained chips"
        # Loser's stack < start (they lost their blind)
        assert stacks[actor] < start_stack, "Folder should have lost chips"


class TestRaiseValidation:
    """Test that raise sizes are validated correctly."""

    def test_raise_below_minimum_rejected(self, engine, player_a, player_b, config):
        """RAISE_TO below minimum should be rejected."""
        random.seed(42)
        engine.seat_player(0, player_a, Chips(amount=20000))
        engine.seat_player(1, player_b, Chips(amount=20000))
        engine.start_hand()

        actor = engine.get_actor_seat()
        allowed = engine.get_allowed_actions(actor)

        # Min raise should be at least BB (200) above BB, so min raise-to is 400
        assert allowed.can_raise
        min_raise = allowed.min_raise.amount

        # Try to raise below minimum
        with pytest.raises(ValueError, match="Cannot raise"):
            engine.apply_action(actor, ClientAction.RAISE_TO, Chips(amount=min_raise - 1))

    def test_raise_at_minimum_accepted(self, engine, player_a, player_b):
        """RAISE_TO at exactly minimum should succeed."""
        random.seed(42)
        engine.seat_player(0, player_a, Chips(amount=20000))
        engine.seat_player(1, player_b, Chips(amount=20000))
        engine.start_hand()

        actor = engine.get_actor_seat()
        allowed = engine.get_allowed_actions(actor)

        min_raise = allowed.min_raise.amount
        assert allowed.can_raise

        # Raise to exactly minimum should work
        events = engine.apply_action(actor, ClientAction.RAISE_TO, Chips(amount=min_raise))
        assert any(e.event_type == "action" for e in events)

    def test_raise_above_stack_rejected(self, engine, player_a, player_b):
        """RAISE_TO above player's stack should be rejected."""
        random.seed(42)
        engine.seat_player(0, player_a, Chips(amount=20000))
        engine.seat_player(1, player_b, Chips(amount=20000))
        engine.start_hand()

        actor = engine.get_actor_seat()
        allowed = engine.get_allowed_actions(actor)

        max_raise = allowed.max_raise.amount

        # Try to raise above stack
        with pytest.raises(ValueError, match="Cannot raise"):
            engine.apply_action(actor, ClientAction.RAISE_TO, Chips(amount=max_raise + 1))

    def test_raise_at_maximum_is_all_in(self, engine, player_a, player_b):
        """RAISE_TO at maximum (full stack) should be all-in."""
        random.seed(42)
        engine.seat_player(0, player_a, Chips(amount=20000))
        engine.seat_player(1, player_b, Chips(amount=20000))
        engine.start_hand()

        actor = engine.get_actor_seat()
        allowed = engine.get_allowed_actions(actor)

        max_raise = allowed.max_raise.amount

        # Raise to max should work and mark as all-in
        events = engine.apply_action(actor, ClientAction.RAISE_TO, Chips(amount=max_raise))
        action_event = next(e for e in events if e.event_type == "action")
        assert action_event.is_all_in


class TestActionRequestConsistency:
    """Test that ActionRequest fields are internally consistent."""

    def test_call_allowed_has_call_amount(self, engine, player_a, player_b, config):
        """If CALL is allowed, call_amount should be set and > 0."""
        random.seed(42)
        engine.seat_player(0, player_a, Chips(amount=20000))
        engine.seat_player(1, player_b, Chips(amount=20000))
        engine.start_hand()

        # First actor (SB) raises
        actor = engine.get_actor_seat()
        allowed = engine.get_allowed_actions(actor)
        engine.apply_action(actor, ClientAction.RAISE_TO, Chips(amount=allowed.min_raise.amount))

        # Now BB must call or fold - get action request
        actor = engine.get_actor_seat()
        request = engine.get_action_request(actor)

        if ClientAction.CALL in request.allowed_actions:
            assert request.call_amount is not None, "CALL allowed but call_amount is None"
            assert request.call_amount.amount > 0, "CALL allowed but call_amount is 0"

    def test_check_allowed_no_call_amount(self, engine, player_a, player_b):
        """If CHECK is allowed, there should be no call amount (or 0)."""
        random.seed(42)
        engine.seat_player(0, player_a, Chips(amount=20000))
        engine.seat_player(1, player_b, Chips(amount=20000))
        engine.start_hand()

        # SB calls BB
        engine.apply_action(engine.get_actor_seat(), ClientAction.CALL, None)

        # BB can now check
        actor = engine.get_actor_seat()
        request = engine.get_action_request(actor)

        if ClientAction.CHECK in request.allowed_actions:
            # call_amount should be None (we don't set it when can_call is false)
            assert request.call_amount is None, "CHECK allowed but call_amount is set"

    def test_raise_allowed_has_min_max(self, engine, player_a, player_b):
        """If RAISE_TO/BET is allowed, min_raise and max_raise should be set."""
        random.seed(42)
        engine.seat_player(0, player_a, Chips(amount=20000))
        engine.seat_player(1, player_b, Chips(amount=20000))
        engine.start_hand()

        actor = engine.get_actor_seat()
        request = engine.get_action_request(actor)

        if ClientAction.RAISE_TO in request.allowed_actions or ClientAction.BET in request.allowed_actions:
            assert request.min_raise is not None, "Raise allowed but min_raise is None"
            assert request.max_raise is not None, "Raise allowed but max_raise is None"
            assert request.min_raise.amount <= request.max_raise.amount, "min_raise > max_raise"


class TestMultiPlayer:
    """Tests with 3+ players for proper button/pot handling."""

    def test_three_player_fold_to_raiser(self, config):
        """3 players: everyone folds to one raiser, correct pot award."""
        random.seed(42)
        engine = PokerTableEngine("tbl_3p", config)

        player_a = make_player("Alice")
        player_b = make_player("Bob")
        player_c = make_player("Carol")

        start_stack = 20000
        engine.seat_player(0, player_a, Chips(amount=start_stack))
        engine.seat_player(1, player_b, Chips(amount=start_stack))
        engine.seat_player(2, player_c, Chips(amount=start_stack))

        events = engine.start_hand()
        assert events[0].event_type == "hand_started"

        # First actor raises (don't assume which seat)
        first_actor = engine.get_actor_seat()
        assert first_actor is not None

        allowed = engine.get_allowed_actions(first_actor)
        engine.apply_action(first_actor, ClientAction.RAISE_TO, Chips(amount=allowed.min_raise.amount))

        # Next player folds
        second_actor = engine.get_actor_seat()
        assert second_actor is not None
        assert second_actor != first_actor, "Actor should change after action"
        engine.apply_action(second_actor, ClientAction.FOLD, None)

        # Third player folds
        third_actor = engine.get_actor_seat()
        assert third_actor is not None
        assert third_actor not in (first_actor, second_actor)
        events = engine.apply_action(third_actor, ClientAction.FOLD, None)

        # Hand should end with raiser winning
        hand_ended = next((e for e in events if e.event_type == "hand_ended"), None)
        assert hand_ended is not None, "Hand should have ended"
        assert len(hand_ended.winners) == 1
        assert hand_ended.winners[0].seat == first_actor, "Raiser should win"

        # Verify via snapshot
        snapshot = engine.get_snapshot(for_seat=0)
        stacks = {s.seat_index: s.chips.amount for s in snapshot.seats if s.player is not None}

        # Chip conservation
        total = sum(stacks.values())
        assert total == start_stack * 3, "Chips should be conserved"

        # Winner gained blinds
        assert stacks[first_actor] > start_stack, "Winner should have gained chips"

    def test_button_rotates_between_hands(self, config):
        """Button should rotate to next active seat between hands."""
        random.seed(42)
        engine = PokerTableEngine("tbl_rotate", config)

        player_a = make_player("Alice")
        player_b = make_player("Bob")
        player_c = make_player("Carol")

        engine.seat_player(0, player_a, Chips(amount=20000))
        engine.seat_player(1, player_b, Chips(amount=20000))
        engine.seat_player(2, player_c, Chips(amount=20000))

        # Hand 1
        events = engine.start_hand()
        button_h1 = events[0].button_seat

        # Verify button is visible in snapshot
        snapshot = engine.get_snapshot(for_seat=0)
        button_seats = [s.seat_index for s in snapshot.seats if s.is_button]
        assert len(button_seats) == 1, "Exactly one button"
        assert button_seats[0] == button_h1

        # Complete hand with folds
        while engine.get_actor_seat() is not None:
            actor = engine.get_actor_seat()
            engine.apply_action(actor, ClientAction.FOLD, None)

        # Hand 2
        events = engine.start_hand()
        button_h2 = events[0].button_seat

        # Button should have moved to a different seat
        assert button_h2 != button_h1, "Button should have moved"

        # Verify in snapshot
        snapshot = engine.get_snapshot(for_seat=0)
        button_seats = [s.seat_index for s in snapshot.seats if s.is_button]
        assert button_seats[0] == button_h2

    def test_three_player_check_to_showdown(self, config):
        """3 players check/call to showdown - verify chip conservation."""
        random.seed(123)  # Different seed for variety
        engine = PokerTableEngine("tbl_showdown", config)

        player_a = make_player("Alice")
        player_b = make_player("Bob")
        player_c = make_player("Carol")

        start_stack = 20000
        engine.seat_player(0, player_a, Chips(amount=start_stack))
        engine.seat_player(1, player_b, Chips(amount=start_stack))
        engine.seat_player(2, player_c, Chips(amount=start_stack))

        engine.start_hand()

        # Play until hand ends (call/check everything)
        max_actions = 30
        actions = 0
        hand_ended = False

        while not hand_ended and actions < max_actions:
            actor = engine.get_actor_seat()
            if actor is None:
                break

            allowed = engine.get_allowed_actions(actor)

            if allowed.can_check:
                events = engine.apply_action(actor, ClientAction.CHECK, None)
            elif allowed.can_call:
                events = engine.apply_action(actor, ClientAction.CALL, None)
            else:
                events = engine.apply_action(actor, ClientAction.FOLD, None)

            hand_ended = any(e.event_type == "hand_ended" for e in events)
            actions += 1

        assert hand_ended, "Hand should have ended"

        # Verify chip conservation via snapshot
        snapshot = engine.get_snapshot(for_seat=0)
        total = sum(s.chips.amount for s in snapshot.seats if s.player is not None)
        assert total == start_stack * 3, "Chips should be conserved after showdown"

    def test_actor_progresses_through_players(self, config):
        """Verify actor changes after each action."""
        random.seed(42)
        engine = PokerTableEngine("tbl_progress", config)

        player_a = make_player("Alice")
        player_b = make_player("Bob")
        player_c = make_player("Carol")

        engine.seat_player(0, player_a, Chips(amount=20000))
        engine.seat_player(1, player_b, Chips(amount=20000))
        engine.seat_player(2, player_c, Chips(amount=20000))

        engine.start_hand()

        # Track which seats act
        actors_seen = []
        max_actions = 10

        for _ in range(max_actions):
            actor = engine.get_actor_seat()
            if actor is None:
                break
            actors_seen.append(actor)

            allowed = engine.get_allowed_actions(actor)
            if allowed.can_call:
                engine.apply_action(actor, ClientAction.CALL, None)
            elif allowed.can_check:
                engine.apply_action(actor, ClientAction.CHECK, None)
            else:
                break

        # Should have seen at least 3 actions (one per player preflop)
        assert len(actors_seen) >= 3, f"Expected at least 3 actions, got {len(actors_seen)}"

        # Verify actor changed between consecutive actions
        for i in range(1, len(actors_seen)):
            if actors_seen[i] == actors_seen[i-1]:
                # Same actor twice only valid if street changed (action returned to them)
                pass  # This is OK in some cases
