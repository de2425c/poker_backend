"""
Message handler for WebSocket protocol.

Routes client messages to TableManager and orchestrates broadcasts.
"""

import time
from typing import Optional

import asyncio
from typing import TYPE_CHECKING

from ..models import (
    ClientAction,
    Chips,
    ErrorCode,
    ErrorMessage,
    AuthOkMessage,
    StateDeltaMessage,
    TableLeftMessage,
    PongMessage,
    PlayerIdentity,
)
from ..manager import TableManager, TimeoutActionCommand
from .connection import ConnectionManager
from .auth import AuthService

if TYPE_CHECKING:
    from .timer import ActionTimerService, PendingAction


class MessageHandler:
    """Handles client messages and orchestrates responses."""

    def __init__(
        self,
        manager: TableManager,
        connections: ConnectionManager,
        auth: AuthService,
        timer: Optional["ActionTimerService"] = None,
    ):
        self._manager = manager
        self._connections = connections
        self._auth = auth
        self._timer = timer
        self._processed_actions: dict[str, float] = {}  # action_id -> timestamp
        self._processed_actions_max_age = 60.0  # Clear entries older than 60s

    async def handle_auth(self, user_id: str, token: str, protocol_version: int) -> dict:
        """Handle AUTH message. Returns response dict."""
        verified_user = self._auth.verify_token(token)
        if verified_user is None or verified_user != user_id:
            return ErrorMessage(
                code=ErrorCode.UNAUTHORIZED,
                message="Invalid token",
            ).model_dump(mode="json")

        # Check if user already at a table (reconnect)
        table_id = self._manager.get_table_for_user(user_id)

        return AuthOkMessage(
            user_id=user_id,
            current_table_id=table_id,
        ).model_dump(mode="json")

    async def handle_join_pool(
        self, user_id: str, stake_id: str, buy_in_cents: int, display_name: str
    ) -> tuple[dict, str, int]:
        """Handle JOIN_POOL message. Returns (TABLE_SNAPSHOT, table_id, seat) or (ERROR, None, None)."""
        try:
            player = PlayerIdentity(
                user_id=user_id,
                display_name=display_name,
                avatar_url=None,
            )
            table_id, seat = await self._manager.add_player(
                user_id, stake_id, Chips(amount=buy_in_cents), player
            )

            # Get snapshot FIRST (before registering for broadcasts)
            snapshot = await self._manager.get_snapshot(user_id)

            # Auto-start hand when we reach min players (if no hand in progress)
            runner = self._manager._tables.get(table_id)
            if runner:
                player_count = runner.player_count
                min_players = runner._config.min_players_to_start
                status = runner._engine._status
                has_hand = status.value == "running"
                print(f"[AUTO-START CHECK] table={table_id} players={player_count} min={min_players} status={status} has_hand={has_hand}")
                if player_count >= min_players and not has_hand:
                    print(f"[AUTO-START] Triggering auto-start for table {table_id}")
                    asyncio.create_task(self._auto_start_next_hand(table_id, delay=1.5))

            # Return snapshot and join info for caller to complete registration
            return (snapshot.model_dump(mode="json"), table_id, seat, display_name, buy_in_cents)

        except ValueError as e:
            return (ErrorMessage(
                code=ErrorCode.BAD_REQUEST,
                message=str(e),
            ).model_dump(mode="json"), None, None, None, None)

    async def handle_join_table(
        self, user_id: str, table_id: str, stake_id: str, buy_in_cents: int, display_name: str
    ) -> tuple:
        """Handle JOIN_TABLE message. Joins a specific table by ID.

        Returns (snapshot, table_id, seat, display_name, buy_in_cents) or (error, None, None, None, None).
        """
        try:
            player = PlayerIdentity(
                user_id=user_id,
                display_name=display_name,
                avatar_url=None,
            )
            result_table_id, seat = await self._manager.add_player(
                user_id, stake_id, Chips(amount=buy_in_cents), player,
                table_id=table_id,
            )

            # Get snapshot FIRST (before registering for broadcasts)
            snapshot = await self._manager.get_snapshot(user_id)

            # Auto-start hand when we reach min players
            runner = self._manager._tables.get(result_table_id)
            if runner:
                player_count = runner.player_count
                min_players = runner._config.min_players_to_start
                status = runner._engine._status
                has_hand = status.value == "running"
                if player_count >= min_players and not has_hand:
                    asyncio.create_task(self._auto_start_next_hand(result_table_id, delay=1.5))

            return (snapshot.model_dump(mode="json"), result_table_id, seat, display_name, buy_in_cents)

        except ValueError as e:
            return (ErrorMessage(
                code=ErrorCode.BAD_REQUEST,
                message=str(e),
            ).model_dump(mode="json"), None, None, None, None)

    async def complete_join(
        self, user_id: str, table_id: str, seat: int, display_name: str, buy_in_cents: int
    ) -> None:
        """Complete join by registering for broadcasts and notifying other players.

        Call this AFTER sending the snapshot response to avoid race conditions.
        """
        # Register for broadcasts
        self._connections.join_table(user_id, table_id)

        # Broadcast SEAT_UPDATE to other players at the table
        seat_update = {
            "type": "SEAT_UPDATE",
            "seat": {
                "seat_index": seat,
                "status": "seated",
                "player": {
                    "user_id": user_id,
                    "display_name": display_name,
                    "avatar_url": None,
                },
                "chips": {"amount": buy_in_cents},
                "bet": {"amount": 0},
                "is_button": False,
                "is_connected": True,
            },
            "seq": None,
        }
        await self._connections.broadcast_to_table(table_id, seat_update, exclude=user_id)

    async def handle_action(
        self,
        user_id: str,
        hand_id: str,
        action_id: str,
        action: str,
        amount_cents: Optional[int],
    ) -> Optional[dict]:
        """
        Handle ACTION message.

        Returns error dict or None (broadcasts results on success).
        """
        # Clean old entries from idempotency cache
        now = time.time()
        old_keys = [k for k, ts in self._processed_actions.items()
                    if now - ts > self._processed_actions_max_age]
        for k in old_keys:
            del self._processed_actions[k]

        # Idempotency check
        if action_id in self._processed_actions:
            print(f"[IDEMPOTENT] Ignoring duplicate action_id={action_id} user={user_id}", flush=True)
            return None  # Already processed, ignore

        # Deadline check - reject if expired
        if self._timer and self._timer.is_expired(user_id):
            return ErrorMessage(
                code=ErrorCode.ACTION_TIMEOUT,
                message="Action deadline expired",
                ref_msg_id=action_id,
            ).model_dump(mode="json")

        # Clear deadline since action arrived in time
        if self._timer:
            self._timer.clear_deadline(user_id)

        # Capture table_id BEFORE processing - user might be removed during action
        table_id = self._manager.get_table_for_user(user_id)
        if not table_id:
            return ErrorMessage(
                code=ErrorCode.NOT_AT_TABLE,
                message="User not at any table",
                ref_msg_id=action_id,
            ).model_dump(mode="json")

        try:
            client_action = ClientAction(action)
            amount = Chips(amount=amount_cents) if amount_cents else None

            events = await self._manager.route_action(user_id, hand_id, client_action, amount)

            # Mark as processed (with timestamp for cleanup)
            self._processed_actions[action_id] = time.time()

            # Broadcast to table (table_id captured before action processing)
            await self._broadcast_events(table_id, hand_id, events)

            return None  # Success - no direct response, just broadcasts

        except ValueError as e:
            error_code = self._map_error(str(e))
            print(f"[ACTION ERROR] user={user_id} hand={hand_id} action={action} error={str(e)}")
            return ErrorMessage(
                code=error_code,
                message=str(e),
                ref_msg_id=action_id,
            ).model_dump(mode="json")

    async def handle_leave_table(self, user_id: str) -> dict:
        """Handle LEAVE_TABLE message. Returns TABLE_LEFT or ERROR."""
        try:
            # Get table_id and seat before removing
            table_id = self._manager.get_table_for_user(user_id)
            seat_index = None
            if table_id:
                runner = self._manager._tables.get(table_id)
                if runner:
                    for i, seat in enumerate(runner._engine._seats):
                        if seat and seat.player and seat.player.user_id == user_id:
                            seat_index = i
                            break

            chips = await self._manager.remove_player(user_id)
            self._connections.leave_table(user_id)

            # Credit balance back to wallet (skip for bots)
            if (
                chips.amount > 0
                and self._manager._firestore
                and not user_id.startswith(("bot_", "user_bot_"))
            ):
                await self._manager._firestore.add_balance(user_id, chips.amount)

            # Broadcast SEAT_UPDATE (empty seat) to remaining players
            if table_id and seat_index is not None:
                seat_update = {
                    "type": "SEAT_UPDATE",
                    "seat": {
                        "seat_index": seat_index,
                        "status": "empty",
                        "player": None,
                        "chips": {"amount": 0},
                        "bet": {"amount": 0},
                        "is_button": False,
                        "is_connected": False,
                    },
                    "seq": None,
                }
                await self._connections.broadcast_to_table(table_id, seat_update)

            return TableLeftMessage(final_chips=chips).model_dump(mode="json")

        except ValueError as e:
            return ErrorMessage(
                code=ErrorCode.NOT_AT_TABLE,
                message=str(e),
            ).model_dump(mode="json")

    async def handle_ping(self, user_id: str, client_ts: int) -> dict:
        """Handle PING message. Returns PONG."""
        return PongMessage(
            client_ts=client_ts,
            server_ts=int(time.time() * 1000),
        ).model_dump(mode="json")

    async def handle_next_hand(self, user_id: str) -> Optional[dict]:
        """Handle NEXT_HAND message from client. Starts the next hand."""
        table_id = self._manager.get_table_for_user(user_id)
        if not table_id:
            return ErrorMessage(
                code=ErrorCode.NOT_AT_TABLE,
                message="Not at a table",
            ).model_dump(mode="json")

        try:
            events = await self._manager.start_hand(table_id)
            await self._broadcast_events(table_id, None, events)
            return None
        except ValueError as e:
            return ErrorMessage(
                code=ErrorCode.BAD_REQUEST,
                message=str(e),
            ).model_dump(mode="json")

    async def handle_topup_request(self, user_id: str, request_id: str) -> dict:
        """Handle TOP_UP_REQUEST message. Queue a manual top-up for next hand."""
        try:
            topup_amount, new_stack = await self._manager.request_topup(user_id)
            return {
                "type": "TOP_UP_PENDING",
                "request_id": request_id,
                "topup_amount": {"amount": topup_amount},
                "new_stack": {"amount": new_stack},
            }
        except ValueError as e:
            return ErrorMessage(
                code=ErrorCode.BAD_REQUEST,
                message=str(e),
            ).model_dump(mode="json")

    async def handle_set_auto_top_up(self, user_id: str, enabled: bool) -> dict:
        """Handle SET_AUTO_TOP_UP message. Toggle auto top-up for a player mid-session."""
        try:
            table_id = self._manager._user_tables.get(user_id)
            if table_id is None:
                raise ValueError("User not at any table")

            runner = self._manager._tables.get(table_id)
            if runner is None:
                raise ValueError("Table not found")

            # Find user's seat and update auto_topup_enabled
            for seat_state in runner._engine._seats:
                if seat_state is not None and seat_state.player.user_id == user_id:
                    seat_state.auto_topup_enabled = enabled
                    print(f"[AUTO_TOP_UP] Set auto_topup_enabled={enabled} for user {user_id[:20]}...")
                    return {
                        "type": "AUTO_TOP_UP_SET",
                        "enabled": enabled,
                    }

            raise ValueError("User not seated at table")
        except ValueError as e:
            return ErrorMessage(
                code=ErrorCode.BAD_REQUEST,
                message=str(e),
            ).model_dump(mode="json")

    async def handle_start_hand(self, table_id: str) -> Optional[dict]:
        """
        Handle start hand request (from debug endpoint).

        Returns error dict or None (broadcasts results on success).
        """
        try:
            events = await self._manager.start_hand(table_id)
            await self._broadcast_events(table_id, None, events)
            return None
        except ValueError as e:
            return ErrorMessage(
                code=ErrorCode.BAD_REQUEST,
                message=str(e),
            ).model_dump(mode="json")

    async def _broadcast_events(
        self, table_id: str, hand_id: Optional[str], events: list
    ) -> None:
        """Broadcast STATE_DELTA to all users at table."""
        user_ids = self._connections.get_table_users(table_id)
        if not user_ids:
            # Log this case to help debug disconnection issues
            event_types = [e.get("event_type") if isinstance(e, dict) else getattr(e, "event_type", "?") for e in events]
            print(f"[BROADCAST] SKIPPED table={table_id} events={event_types} (no connected users)", flush=True)
            return

        # Check if this is a hand_started event - if so, send snapshots with hole cards
        is_hand_start = any(
            getattr(e, 'event_type', None) == 'hand_started' or
            (isinstance(e, dict) and e.get('event_type') == 'hand_started')
            for e in events
        )

        # Check if this is a hand_ended event - if so, auto-start next hand
        is_hand_end = any(
            getattr(e, 'event_type', None) == 'hand_ended' or
            (isinstance(e, dict) and e.get('event_type') == 'hand_ended')
            for e in events
        )

        # Get seq from first user's snapshot
        first_user = next(iter(user_ids))
        try:
            snapshot = await self._manager.get_snapshot(first_user)
            seq = snapshot.seq

            # Determine hand_id from snapshot if not provided
            if hand_id is None and snapshot.hand:
                hand_id = snapshot.hand.hand_id

            actor_seat = snapshot.hand.actor_seat if snapshot.hand else None
        except Exception:
            seq = 0
            actor_seat = None

        # Build STATE_DELTA
        delta = StateDeltaMessage(
            table_id=table_id,
            hand_id=hand_id,
            seq=seq,
            events=events,
            actor_seat=actor_seat,
        )
        delta_dict = delta.model_dump(mode="json")

        event_types = [e.get("event_type") if isinstance(e, dict) else getattr(e, "event_type", "?") for e in events]
        print(f"[BROADCAST] table={table_id} events={event_types} is_hand_end={is_hand_end}", flush=True)

        # If hand just started, check for applied top-ups and broadcast REBUY messages
        if is_hand_start:
            runner = self._manager._tables.get(table_id)
            if runner:
                applied_topups = runner._engine.get_and_clear_applied_topups()
                for seat_idx, topup_amount, new_stack in applied_topups:
                    rebuy_msg = {
                        "type": "REBUY",
                        "seat": seat_idx,
                        "amount": {"amount": topup_amount},
                        "new_stack": {"amount": new_stack},
                    }
                    await self._connections.broadcast_to_table(table_id, rebuy_msg)
                    print(f"[REBUY] Sent for applied top-up: seat {seat_idx}, +{topup_amount} cents")

        # Send to all users
        for user_id in user_ids:
            await self._connections.send_to_user(user_id, delta_dict)

            try:
                user_snapshot = await self._manager.get_snapshot(user_id)

                # If hand just started, send TABLE_SNAPSHOT with hole cards
                if is_hand_start:
                    snapshot_dict = user_snapshot.model_dump(mode="json")
                    await self._connections.send_to_user(user_id, snapshot_dict)

                # Check if this user is the actor and send ACTION_REQUEST
                actor_seat = user_snapshot.hand.actor_seat if user_snapshot.hand else None
                your_seat = user_snapshot.your_seat
                if (user_snapshot.hand and
                    actor_seat is not None and
                    actor_seat == your_seat):
                    # This user needs to act - send ACTION_REQUEST
                    print(f"[ACTION_REQUEST] Sending to {user_id} (seat {your_seat})", flush=True)
                    await self._send_action_request(user_id, user_snapshot)
            except Exception as e:
                print(f"[BROADCAST] Error sending to {user_id}: {e}", flush=True)

        # Safety net: ensure the current actor has an ACTION_REQUEST and timer
        # This catches cases where the broadcast loop failed for the actor
        if not is_hand_end:
            try:
                runner = self._manager._tables.get(table_id)
                if runner and runner._engine._status.value == "running":
                    actor_seat = runner._engine.get_actor_seat()
                    if actor_seat is not None:
                        for uid, seat in runner._user_seats.items():
                            if seat == actor_seat:
                                if not self._timer or not self._timer.get_pending(uid):
                                    print(f"[SAFETY_NET] Actor {uid[:20]}... missing timer, sending ACTION_REQUEST", flush=True)
                                    snapshot = await self._manager.get_snapshot(uid)
                                    await self._send_action_request(uid, snapshot)
                                break
            except Exception as e:
                print(f"[SAFETY_NET] Error ensuring actor has timer: {e}", flush=True)

        # Check for bust players and process rebuys after hand ends
        if is_hand_end:
            # Clear all pending timers for players at this table
            if self._timer:
                for uid in user_ids:
                    self._timer.clear_deadline(uid)
                print(f"[TIMER] Cleared all deadlines for table {table_id} (hand ended)")

            await self._check_and_process_rebuys(table_id)

            # Auto-start next hand only if no human players at the table
            human_users = [
                uid for uid in user_ids if not uid.startswith("user_bot_")
            ]
            if not human_users:
                asyncio.create_task(self._auto_start_next_hand(table_id))

    async def _auto_start_next_hand(self, table_id: str, delay: float = 3.0) -> None:
        """Auto-start the next hand after a delay."""
        print(f"[AUTO-START] Waiting {delay}s before starting hand on {table_id}")
        await asyncio.sleep(delay)

        # Check if table still has enough players
        runner = self._manager._tables.get(table_id)
        if not runner:
            print(f"[AUTO-START] Table {table_id} not found, aborting")
            return

        player_count = runner.player_count
        min_players = runner._config.min_players_to_start
        if player_count < min_players:
            print(f"[AUTO-START] Not enough players ({player_count} < {min_players}), aborting")
            return

        # Check if hand already in progress
        if runner._engine._status.value == "running":
            print(f"[AUTO-START] Hand already running, aborting")
            return

        # Start the next hand
        try:
            print(f"[AUTO-START] Starting hand on {table_id}")
            events = await self._manager.start_hand(table_id)
            await self._broadcast_events(table_id, None, events)
            print(f"[AUTO-START] Hand started successfully on {table_id}")
        except Exception as e:
            print(f"[AUTO-START] Error starting hand: {e}")
            pass

    async def _send_action_request(self, user_id: str, snapshot) -> None:
        """Send ACTION_REQUEST to a user who needs to act."""
        try:
            import time as _time
            _t0 = int(_time.time() * 1000)
            action_request = await self._manager.get_action_request(user_id)
            _t1 = int(_time.time() * 1000)
            print(f"[DEBUG_TIMER] now={_t1} expires_at={action_request.expires_at_ms} delta={action_request.expires_at_ms - _t1}ms (get_action took {_t1-_t0}ms)", flush=True)
            msg = action_request.model_dump(mode="json")
            await self._connections.send_to_user(user_id, msg)

            # Register deadline with timer service
            if self._timer:
                table_id = self._manager.get_table_for_user(user_id)
                # Facing bet if there's an amount to call > 0
                facing_bet = (
                    action_request.call_amount is not None
                    and action_request.call_amount.amount > 0
                )
                _t2 = int(_time.time() * 1000)
                deadline_id = self._timer.register_deadline(
                    table_id=table_id,
                    user_id=user_id,
                    hand_id=action_request.hand_id,
                    seat=action_request.seat,
                    deadline_ms=action_request.expires_at_ms,
                    facing_bet=facing_bet,
                )
                _t3 = int(_time.time() * 1000)
                delta_at_reg = action_request.expires_at_ms - _t3
                print(f"[TIMER_REG] {user_id[:25]}... deadline_id={deadline_id} expires={action_request.expires_at_ms} delta_at_reg={delta_at_reg}ms (send took {_t2-_t1}ms)", flush=True)
            else:
                print(f"[TIMER_REG] No timer service for {user_id[:20]}...", flush=True)
        except Exception as e:
            # User may no longer be the actor (race condition) - that's ok
            print(f"[TIMER_REG] Exception for {user_id[:20]}...: {e}", flush=True)
            pass

    async def _check_and_process_rebuys(self, table_id: str) -> None:
        """Check for bust human players and attempt auto-rebuy."""
        runner = self._manager._tables.get(table_id)
        if not runner:
            return

        for seat_idx, seat_state in enumerate(runner._engine._seats):
            if seat_state is None:
                continue

            user_id = seat_state.player.user_id

            # Skip bots
            if user_id.startswith(("bot_", "user_bot_")):
                continue

            # Skip if auto top-up is disabled for this player
            if not seat_state.auto_topup_enabled:
                print(f"[REBUY_CHECK] seat {seat_idx} user {user_id[:20]}... skipped (auto top-up disabled)")
                continue

            print(f"[REBUY_CHECK] seat {seat_idx} user {user_id[:20]}... chips={seat_state.chips}")

            # Check if below $200 (20000 cents) - trigger rebuy
            if seat_state.chips < 20000:
                result = await self._manager.try_rebuy(user_id, table_id, seat_idx)
                if result:
                    rebuy_amount, new_stack = result
                    rebuy_msg = {
                        "type": "REBUY",
                        "seat": seat_idx,
                        "amount": {"amount": rebuy_amount},
                        "new_stack": {"amount": new_stack},
                    }
                    await self._connections.broadcast_to_table(table_id, rebuy_msg)
                    print(f"[REBUY] Sent for seat {seat_idx}: +{rebuy_amount} cents")
                else:
                    # Can't afford rebuy - send OUT_OF_CHIPS
                    balance = 0
                    if self._manager._firestore:
                        try:
                            balance = await self._manager._firestore.get_user_balance(user_id)
                        except Exception:
                            pass
                    out_msg = {
                        "type": "OUT_OF_CHIPS",
                        "balance_cents": balance,
                        "rebuy_cost_cents": 20000,
                    }
                    await self._connections.send_to_user(user_id, out_msg)
                    print(f"[OUT_OF_CHIPS] Sent to {user_id}")

    async def handle_timeout(self, pending: "PendingAction") -> None:
        """Handle action timeout - apply auto-action."""
        try:
            # Note: The pending action has already been removed from the timer
            # by the time this callback runs, so we use the passed-in pending object
            print(f"[TIMEOUT] Processing timeout for {pending.user_id[:20]}... seat={pending.seat} facing_bet={pending.facing_bet}", flush=True)

            loop = asyncio.get_running_loop()
            future: asyncio.Future = loop.create_future()

            runner = self._manager._tables.get(pending.table_id)
            if not runner:
                return

            await runner.submit(TimeoutActionCommand(
                user_id=pending.user_id,
                hand_id=pending.hand_id,
                seat=pending.seat,
                facing_bet=pending.facing_bet,
                result_future=future,
            ))

            events = await future
            if events:
                await self._broadcast_events(pending.table_id, pending.hand_id, events)
        except Exception as e:
            # Log but don't crash on timeout handling errors
            print(f"[TIMEOUT] Error handling timeout for {pending.user_id}: {e}", flush=True)

    async def handle_quip(
        self,
        user_id: str,
        hand_id: str,
        seat: int,
        text: str,
    ) -> None:
        """Handle QUIP message from bot. Broadcasts quip to all players at table."""
        table_id = self._manager.get_table_for_user(user_id)
        if not table_id:
            print(f"[QUIP] Ignored - user {user_id} not at any table")
            return

        # Build quip message to broadcast
        quip_msg = {
            "type": "QUIP",
            "hand_id": hand_id,
            "seat": seat,
            "text": text,
        }

        # Broadcast to all players at the table
        await self._connections.broadcast_to_table(table_id, quip_msg)
        print(f"[QUIP] Broadcast to table {table_id}: seat {seat} says \"{text}\"", flush=True)

    def _map_error(self, error_msg: str) -> ErrorCode:
        """Map error message to ErrorCode."""
        msg_lower = error_msg.lower()
        if "insufficient_balance" in msg_lower:
            return ErrorCode.INSUFFICIENT_BALANCE
        if "turn" in msg_lower:
            return ErrorCode.NOT_YOUR_TURN
        if "invalid" in msg_lower:
            return ErrorCode.INVALID_ACTION
        if "already at" in msg_lower:
            return ErrorCode.ALREADY_AT_TABLE
        if "not at" in msg_lower:
            return ErrorCode.NOT_AT_TABLE
        return ErrorCode.BAD_REQUEST
