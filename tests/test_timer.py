"""
Tests for action timer service and timeout handling.

Tests both the timer service directly and integration with the server.
"""

import asyncio
import time
import pytest
from fastapi.testclient import TestClient

from src.server.app import app
from src.server.timer import ActionTimerService, PendingAction


class TestActionTimerService:
    """Unit tests for ActionTimerService."""

    def test_register_and_get_pending(self):
        """Registering a deadline stores the pending action."""
        timer = ActionTimerService()

        timer.register_deadline(
            table_id="table_1",
            user_id="user_1",
            hand_id="hand_1",
            seat=0,
            deadline_ms=int(time.time() * 1000) + 5000,
            facing_bet=True,
        )

        pending = timer.get_pending("user_1")
        assert pending is not None
        assert pending.table_id == "table_1"
        assert pending.user_id == "user_1"
        assert pending.hand_id == "hand_1"
        assert pending.seat == 0
        assert pending.facing_bet is True

    def test_clear_deadline(self):
        """Clearing a deadline removes the pending action."""
        timer = ActionTimerService()

        timer.register_deadline(
            table_id="table_1",
            user_id="user_1",
            hand_id="hand_1",
            seat=0,
            deadline_ms=int(time.time() * 1000) + 5000,
            facing_bet=True,
        )

        timer.clear_deadline("user_1")
        assert timer.get_pending("user_1") is None

    def test_is_expired_false_before_deadline(self):
        """Action is not expired before deadline passes."""
        timer = ActionTimerService()

        # Deadline 5 seconds in the future
        timer.register_deadline(
            table_id="table_1",
            user_id="user_1",
            hand_id="hand_1",
            seat=0,
            deadline_ms=int(time.time() * 1000) + 5000,
            facing_bet=True,
        )

        assert timer.is_expired("user_1") is False

    def test_is_expired_true_after_deadline(self):
        """Action is expired after deadline passes."""
        timer = ActionTimerService()

        # Deadline 1ms in the past
        timer.register_deadline(
            table_id="table_1",
            user_id="user_1",
            hand_id="hand_1",
            seat=0,
            deadline_ms=int(time.time() * 1000) - 1,
            facing_bet=True,
        )

        assert timer.is_expired("user_1") is True

    def test_is_expired_false_for_unknown_user(self):
        """Unknown user is not considered expired."""
        timer = ActionTimerService()
        assert timer.is_expired("unknown_user") is False

    @pytest.mark.asyncio
    async def test_tick_triggers_callback_on_expiry(self):
        """Tick loop triggers callback when deadline expires."""
        timer = ActionTimerService(tick_interval_ms=50)
        callback_triggered = []

        async def on_timeout(pending: PendingAction) -> None:
            callback_triggered.append(pending)

        timer.set_timeout_callback(on_timeout)

        # Register deadline that's already expired
        timer.register_deadline(
            table_id="table_1",
            user_id="user_1",
            hand_id="hand_1",
            seat=0,
            deadline_ms=int(time.time() * 1000) - 1,
            facing_bet=True,
        )

        timer.start()
        await asyncio.sleep(0.1)  # Wait for tick
        await timer.stop()

        assert len(callback_triggered) == 1
        assert callback_triggered[0].user_id == "user_1"

    @pytest.mark.asyncio
    async def test_start_stop(self):
        """Timer can be started and stopped cleanly."""
        timer = ActionTimerService(tick_interval_ms=50)

        timer.start()
        assert timer._running is True
        assert timer._task is not None

        await timer.stop()
        assert timer._running is False


@pytest.fixture
def client():
    """HTTP test client with proper lifecycle management."""
    with TestClient(app) as c:
        yield c


class TestTimeoutIntegration:
    """Integration tests for timeout handling in the server."""

    def test_late_action_rejected(self, client):
        """Action submitted after deadline expires is rejected with ACTION_TIMEOUT."""
        # Setup: Two players join and start a hand
        with client.websocket_connect("/ws") as ws1:
            ws1.send_json({"type": "AUTH", "token": "user_timeout1"})
            ws1.receive_json()  # AUTH_OK

            ws1.send_json({
                "type": "JOIN_POOL",
                "stake_id": "nlh_1_2",
                "buy_in_cents": 20000,
            })
            snap1 = ws1.receive_json()
            table_id = snap1["table_id"]
            seat1 = snap1["your_seat"]

            with client.websocket_connect("/ws") as ws2:
                ws2.send_json({"type": "AUTH", "token": "user_timeout2"})
                ws2.receive_json()

                ws2.send_json({
                    "type": "JOIN_POOL",
                    "stake_id": "nlh_1_2",
                    "buy_in_cents": 20000,
                })
                snap2 = ws2.receive_json()
                seat2 = snap2["your_seat"]

                # Start hand
                response = client.post(f"/debug/start_hand/{table_id}")
                assert response.status_code == 200

                # Both receive STATE_DELTA
                delta1 = ws1.receive_json()
                delta2 = ws2.receive_json()
                assert delta1["type"] == "STATE_DELTA"
                hand_id = delta1.get("hand_id", "")

                # Determine who acts first from the hand_started event
                hand_started = next(
                    e for e in delta1.get("events", [])
                    if e.get("event_type") == "hand_started"
                )
                button_seat = hand_started["button_seat"]
                actor_seat = 1 - button_seat

                if seat1 == actor_seat:
                    actor_ws = ws1
                else:
                    actor_ws = ws2

                # Get ACTION_REQUEST
                action_req = actor_ws.receive_json()
                assert action_req["type"] == "ACTION_REQUEST"
                expires_at_ms = action_req.get("expires_at_ms")

                # Force the deadline to expire by accessing app's timer
                # and manually marking it as expired
                from src.server.app import timer
                pending = timer.get_pending(
                    "user_timeout1" if seat1 == actor_seat else "user_timeout2"
                )
                if pending:
                    # Manually set deadline to past (preserve deadline_id)
                    timer._pending[pending.user_id] = PendingAction(
                        table_id=pending.table_id,
                        user_id=pending.user_id,
                        hand_id=pending.hand_id,
                        seat=pending.seat,
                        deadline_ms=int(time.time() * 1000) - 1000,  # 1 second ago
                        facing_bet=pending.facing_bet,
                        deadline_id=pending.deadline_id,
                    )

                # Now submit action - should be rejected as expired
                actor_ws.send_json({
                    "type": "ACTION",
                    "hand_id": hand_id,
                    "action_id": "late_action_1",
                    "action": "call",
                })

                response = actor_ws.receive_json()
                assert response["type"] == "ERROR", \
                    f"Expected ERROR for late action, got {response['type']}"
                assert response["code"] == "action_timeout", \
                    f"Expected action_timeout code, got {response['code']}"

    def test_action_in_time_clears_deadline(self, client):
        """Action submitted before deadline clears the pending deadline."""
        with client.websocket_connect("/ws") as ws1:
            ws1.send_json({"type": "AUTH", "token": "user_timely1"})
            ws1.receive_json()

            ws1.send_json({
                "type": "JOIN_POOL",
                "stake_id": "nlh_1_2",
                "buy_in_cents": 20000,
            })
            snap1 = ws1.receive_json()
            table_id = snap1["table_id"]
            seat1 = snap1["your_seat"]

            with client.websocket_connect("/ws") as ws2:
                ws2.send_json({"type": "AUTH", "token": "user_timely2"})
                ws2.receive_json()

                ws2.send_json({
                    "type": "JOIN_POOL",
                    "stake_id": "nlh_1_2",
                    "buy_in_cents": 20000,
                })
                snap2 = ws2.receive_json()
                seat2 = snap2["your_seat"]

                # Start hand
                response = client.post(f"/debug/start_hand/{table_id}")
                assert response.status_code == 200

                # Both receive STATE_DELTA
                delta1 = ws1.receive_json()
                delta2 = ws2.receive_json()
                hand_id = delta1.get("hand_id", "")

                # Determine actor
                hand_started = next(
                    e for e in delta1.get("events", [])
                    if e.get("event_type") == "hand_started"
                )
                button_seat = hand_started["button_seat"]
                actor_seat = 1 - button_seat

                actor_user = "user_timely1" if seat1 == actor_seat else "user_timely2"
                actor_ws = ws1 if seat1 == actor_seat else ws2

                # Get ACTION_REQUEST
                action_req = actor_ws.receive_json()
                assert action_req["type"] == "ACTION_REQUEST"

                # Verify deadline is registered
                from src.server.app import timer
                pending = timer.get_pending(actor_user)
                assert pending is not None, "Deadline should be registered"

                # Submit action before deadline
                actor_ws.send_json({
                    "type": "ACTION",
                    "hand_id": hand_id,
                    "action_id": "timely_action_1",
                    "action": "fold",
                })

                # Should get STATE_DELTA (hand ends)
                response = actor_ws.receive_json()
                assert response["type"] == "STATE_DELTA", \
                    f"Expected STATE_DELTA, got {response['type']}"

                # Verify deadline is cleared
                pending = timer.get_pending(actor_user)
                assert pending is None, "Deadline should be cleared after action"


class TestAutoAction:
    """Tests for automatic action on timeout."""

    def test_timeout_triggers_auto_fold_when_facing_bet(self, client):
        """When facing a bet and timeout occurs, player is auto-folded."""
        with client.websocket_connect("/ws") as ws1:
            ws1.send_json({"type": "AUTH", "token": "user_autofold1"})
            ws1.receive_json()

            ws1.send_json({
                "type": "JOIN_POOL",
                "stake_id": "nlh_1_2",
                "buy_in_cents": 20000,
            })
            snap1 = ws1.receive_json()
            table_id = snap1["table_id"]
            seat1 = snap1["your_seat"]

            with client.websocket_connect("/ws") as ws2:
                ws2.send_json({"type": "AUTH", "token": "user_autofold2"})
                ws2.receive_json()

                ws2.send_json({
                    "type": "JOIN_POOL",
                    "stake_id": "nlh_1_2",
                    "buy_in_cents": 20000,
                })
                snap2 = ws2.receive_json()
                seat2 = snap2["your_seat"]

                # Start hand
                response = client.post(f"/debug/start_hand/{table_id}")
                assert response.status_code == 200

                # Both receive STATE_DELTA
                delta1 = ws1.receive_json()
                delta2 = ws2.receive_json()
                hand_id = delta1.get("hand_id", "")

                # Determine actor
                hand_started = next(
                    e for e in delta1.get("events", [])
                    if e.get("event_type") == "hand_started"
                )
                button_seat = hand_started["button_seat"]
                actor_seat = 1 - button_seat

                actor_user = "user_autofold1" if seat1 == actor_seat else "user_autofold2"
                actor_ws = ws1 if seat1 == actor_seat else ws2
                other_ws = ws2 if seat1 == actor_seat else ws1

                # Get ACTION_REQUEST
                action_req = actor_ws.receive_json()
                assert action_req["type"] == "ACTION_REQUEST"

                # Force timeout via debug endpoint
                response = client.post(f"/debug/force_timeout/{actor_user}")
                assert response.status_code == 200, \
                    f"Force timeout failed: {response.json()}"

                # Both should receive STATE_DELTA with fold event
                # The actor's fold ends the hand
                actor_delta = actor_ws.receive_json()
                assert actor_delta["type"] == "STATE_DELTA", \
                    f"Expected STATE_DELTA from timeout, got {actor_delta['type']}"

                # Check for fold event (event_type is "action", not "player_action")
                has_fold = any(
                    e.get("event_type") == "action" and e.get("action") == "fold"
                    for e in actor_delta.get("events", [])
                )
                assert has_fold, f"Expected fold event in delta: {actor_delta.get('events', [])}"

                # Should also have hand_ended
                has_ended = any(
                    e.get("event_type") == "hand_ended"
                    for e in actor_delta.get("events", [])
                )
                assert has_ended, "Hand should end after fold in heads-up"

                # Other player should also get the delta
                other_delta = other_ws.receive_json()
                assert other_delta["type"] == "STATE_DELTA"
