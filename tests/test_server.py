"""
Integration tests for the WebSocket server.

Tests the full server stack including authentication, joining, and actions.
"""

import asyncio
import pytest
from fastapi.testclient import TestClient

from src.server.app import app


@pytest.fixture
def client():
    """HTTP test client with proper lifecycle management."""
    # Use context manager to ensure lifespan events are triggered
    with TestClient(app) as c:
        yield c


class TestHealthEndpoint:
    """Tests for the /health endpoint."""

    def test_health_check(self, client):
        """Health endpoint returns ok status."""
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}


class TestWebSocketAuth:
    """Tests for WebSocket authentication."""

    def test_auth_with_valid_token(self, client):
        """Valid token authenticates successfully."""
        with client.websocket_connect("/ws") as ws:
            ws.send_json({
                "type": "AUTH",
                "token": "user_test1",
                "protocol_version": 1,
            })

            response = ws.receive_json()
            assert response["type"] == "AUTH_OK"
            assert response["user_id"] == "user_test1"

    def test_auth_with_invalid_token(self, client):
        """Invalid token returns error."""
        with client.websocket_connect("/ws") as ws:
            ws.send_json({
                "type": "AUTH",
                "token": "invalid_token",
                "protocol_version": 1,
            })

            response = ws.receive_json()
            assert response["type"] == "ERROR"
            assert response["code"] == "unauthorized"

    def test_first_message_must_be_auth(self, client):
        """First message must be AUTH or connection closes with error."""
        with client.websocket_connect("/ws") as ws:
            # Send non-AUTH message first
            ws.send_json({
                "type": "JOIN_POOL",
                "stake_id": "nlh_1_2",
                "buy_in_cents": 20000,
            })

            # Should get explicit error
            response = ws.receive_json()
            assert response["type"] == "ERROR"
            assert response["code"] == "bad_request"
            assert "AUTH" in response["message"]

            # Connection should be closed - next receive should raise
            with pytest.raises(Exception):
                ws.receive_json()


class TestJoinPool:
    """Tests for joining tables."""

    def test_join_pool_returns_snapshot(self, client):
        """JOIN_POOL returns TABLE_SNAPSHOT."""
        with client.websocket_connect("/ws") as ws:
            # Auth first
            ws.send_json({"type": "AUTH", "token": "user_join1"})
            ws.receive_json()  # AUTH_OK

            # Join pool
            ws.send_json({
                "type": "JOIN_POOL",
                "stake_id": "nlh_1_2",
                "buy_in_cents": 20000,
            })

            response = ws.receive_json()
            assert response["type"] == "TABLE_SNAPSHOT"
            assert "table_id" in response
            assert "your_seat" in response
            assert response["your_seat"] >= 0

    def test_two_players_same_table(self, client):
        """Two players joining same stake get same table."""
        with client.websocket_connect("/ws") as ws1:
            ws1.send_json({"type": "AUTH", "token": "user_same1"})
            ws1.receive_json()

            ws1.send_json({
                "type": "JOIN_POOL",
                "stake_id": "nlh_1_2",
                "buy_in_cents": 20000,
            })
            snap1 = ws1.receive_json()

            with client.websocket_connect("/ws") as ws2:
                ws2.send_json({"type": "AUTH", "token": "user_same2"})
                ws2.receive_json()

                ws2.send_json({
                    "type": "JOIN_POOL",
                    "stake_id": "nlh_1_2",
                    "buy_in_cents": 20000,
                })
                snap2 = ws2.receive_json()

                # Same table, different seats
                assert snap1["table_id"] == snap2["table_id"]
                assert snap1["your_seat"] != snap2["your_seat"]


class TestPingPong:
    """Tests for PING/PONG heartbeat."""

    def test_ping_returns_pong(self, client):
        """PING returns PONG with timestamps."""
        with client.websocket_connect("/ws") as ws:
            ws.send_json({"type": "AUTH", "token": "user_ping"})
            ws.receive_json()

            ws.send_json({
                "type": "PING",
                "client_ts": 1234567890,
            })

            response = ws.receive_json()
            assert response["type"] == "PONG"
            assert response["client_ts"] == 1234567890
            assert "server_ts" in response


class TestLeaveTable:
    """Tests for leaving tables."""

    def test_leave_table_returns_chips(self, client):
        """LEAVE_TABLE returns final chip count."""
        with client.websocket_connect("/ws") as ws:
            ws.send_json({"type": "AUTH", "token": "user_leave"})
            ws.receive_json()

            ws.send_json({
                "type": "JOIN_POOL",
                "stake_id": "nlh_1_2",
                "buy_in_cents": 20000,
            })
            ws.receive_json()  # TABLE_SNAPSHOT

            ws.send_json({"type": "LEAVE_TABLE"})

            response = ws.receive_json()
            assert response["type"] == "TABLE_LEFT"
            assert response["final_chips"]["amount"] == 20000


class TestActionIdempotency:
    """Tests for action idempotency."""

    def test_duplicate_action_id_ignored(self, client):
        """Same action_id is processed only once - duplicate silently ignored."""
        with client.websocket_connect("/ws") as ws1:
            ws1.send_json({"type": "AUTH", "token": "user_idemp1"})
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
                ws2.send_json({"type": "AUTH", "token": "user_idemp2"})
                ws2.receive_json()
                ws2.send_json({
                    "type": "JOIN_POOL",
                    "stake_id": "nlh_1_2",
                    "buy_in_cents": 20000,
                })
                snap2 = ws2.receive_json()
                seat2 = snap2["your_seat"]

                # Start hand via debug endpoint
                response = client.post(f"/debug/start_hand/{table_id}")
                assert response.status_code == 200

                # Both clients receive STATE_DELTA for hand start
                delta1 = ws1.receive_json()
                delta2 = ws2.receive_json()
                assert delta1["type"] == "STATE_DELTA"
                assert delta2["type"] == "STATE_DELTA"
                initial_seq = delta1["seq"]

                # Determine who acts first from the hand_started event
                # PokerKit has BB acting first in heads-up preflop
                hand_started = next(
                    e for e in delta1.get("events", [])
                    if e.get("event_type") == "hand_started"
                )
                button_seat = hand_started["button_seat"]

                # In PokerKit heads-up, the non-button (BB) acts first preflop
                actor_seat = 1 - button_seat  # In 2-player, this is the other seat
                if seat1 == actor_seat:
                    actor_ws = ws1
                    other_ws = ws2
                else:
                    actor_ws = ws2
                    other_ws = ws1

                # Get the ACTION_REQUEST for the actor
                action_req = actor_ws.receive_json()
                assert action_req["type"] == "ACTION_REQUEST", \
                    f"Expected ACTION_REQUEST, got {action_req['type']}"

                # Now test idempotency: send same action twice
                action_id = "test_idemp_action_123"
                hand_id = delta1.get("hand_id", "")

                # First action - should be processed
                actor_ws.send_json({
                    "type": "ACTION",
                    "hand_id": hand_id,
                    "action_id": action_id,
                    "action": "fold",
                })

                # Collect the STATE_DELTA from first action (hand ends when SB folds)
                first_delta = actor_ws.receive_json()
                assert first_delta["type"] == "STATE_DELTA"
                first_seq = first_delta["seq"]
                assert first_seq > initial_seq, "Seq should advance after action"

                # Other player should also get this delta
                other_delta = other_ws.receive_json()
                assert other_delta["type"] == "STATE_DELTA"
                assert other_delta["seq"] == first_seq

                # Verify hand ended event in the delta
                has_ended = any(
                    e.get("event_type") == "hand_ended"
                    for e in first_delta.get("events", [])
                )
                assert has_ended, "First action (fold) should end the hand"

                # Now send DUPLICATE action with same action_id
                # Since hand ended, this should be silently ignored AND not cause errors
                actor_ws.send_json({
                    "type": "ACTION",
                    "hand_id": hand_id,
                    "action_id": action_id,  # Same action_id!
                    "action": "fold",
                })

                # Send a PING to flush the queue and verify no extra STATE_DELTA
                actor_ws.send_json({"type": "PING", "client_ts": 12345})
                pong = actor_ws.receive_json()

                # The response should be PONG (not a STATE_DELTA from the duplicate)
                assert pong["type"] == "PONG", \
                    f"Expected PONG after duplicate action, got {pong['type']} - duplicate was processed!"


class TestBroadcast:
    """Tests for broadcast correctness - all players receive updates."""

    def test_action_broadcasts_to_all_players(self, client):
        """When one player acts, ALL players receive the STATE_DELTA."""
        with client.websocket_connect("/ws") as ws1:
            ws1.send_json({"type": "AUTH", "token": "user_bcast1"})
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
                ws2.send_json({"type": "AUTH", "token": "user_bcast2"})
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

                # Both should receive STATE_DELTA for hand start
                delta1 = ws1.receive_json()
                delta2 = ws2.receive_json()
                assert delta1["type"] == "STATE_DELTA"
                assert delta2["type"] == "STATE_DELTA"
                assert delta1["seq"] == delta2["seq"], "Both should get same seq"
                hand_id = delta1.get("hand_id")
                initial_seq = delta1["seq"]

                # Determine who acts first from the hand_started event
                # PokerKit has BB acting first in heads-up preflop
                hand_started = next(
                    e for e in delta1.get("events", [])
                    if e.get("event_type") == "hand_started"
                )
                button_seat = hand_started["button_seat"]

                # In PokerKit heads-up, the non-button (BB) acts first preflop
                actor_seat = 1 - button_seat  # In 2-player, this is the other seat
                if seat1 == actor_seat:
                    actor_ws = ws1
                    other_ws = ws2
                else:
                    actor_ws = ws2
                    other_ws = ws1

                # Get ACTION_REQUEST for actor
                action_req = actor_ws.receive_json()
                assert action_req["type"] == "ACTION_REQUEST", \
                    f"Expected ACTION_REQUEST, got {action_req['type']}"

                # Actor folds
                actor_ws.send_json({
                    "type": "ACTION",
                    "hand_id": hand_id or "",
                    "action_id": "bcast_action_1",
                    "action": "fold",
                })

                # BOTH players should receive STATE_DELTA with hand_ended
                actor_delta = actor_ws.receive_json()
                other_delta = other_ws.receive_json()

                assert actor_delta["type"] == "STATE_DELTA", \
                    f"Actor should get STATE_DELTA, got {actor_delta['type']}"
                assert other_delta["type"] == "STATE_DELTA", \
                    f"Other should get STATE_DELTA, got {other_delta['type']}"

                # Both should have the same seq (and it should have advanced)
                assert actor_delta["seq"] == other_delta["seq"], \
                    f"Seq mismatch: actor={actor_delta['seq']}, other={other_delta['seq']}"
                assert actor_delta["seq"] > initial_seq, \
                    f"Seq should advance: was {initial_seq}, now {actor_delta['seq']}"

                # Both should have hand_ended event
                actor_events = actor_delta.get("events", [])
                other_events = other_delta.get("events", [])

                actor_has_ended = any(
                    e.get("event_type") == "hand_ended" for e in actor_events
                )
                other_has_ended = any(
                    e.get("event_type") == "hand_ended" for e in other_events
                )

                assert actor_has_ended, "Actor delta should have hand_ended event"
                assert other_has_ended, "Other delta should have hand_ended event"


class TestDebugEndpoints:
    """Tests for debug HTTP endpoints."""

    def test_debug_list_tables_empty(self, client):
        """Initially no tables exist."""
        response = client.get("/debug/tables")
        assert response.status_code == 200
        # Note: Other tests may have created tables, so just check structure
        assert "tables" in response.json()

    def test_debug_start_hand_invalid_table(self, client):
        """Starting hand on non-existent table fails."""
        response = client.post("/debug/start_hand/invalid_table_id")
        assert response.status_code == 400
