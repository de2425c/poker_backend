"""
TableRunner - Single-threaded command processor for one table.

Consumes commands from an async queue and executes them
serially on the PokerTableEngine, ensuring thread safety.
"""

import asyncio
from typing import Optional, TYPE_CHECKING

from ..engine import PokerTableEngine, TableConfig
from ..models import ClientAction, HandStartedEvent, HandEndedEvent
from ..persistence import HandBuffer, SeatRecord
from .commands import (
    TableCommand,
    JoinTableCommand,
    LeaveTableCommand,
    PlayerActionCommand,
    StartHandCommand,
    GetSnapshotCommand,
    GetActionRequestCommand,
    TimeoutActionCommand,
)

if TYPE_CHECKING:
    from ..persistence import HandLogger


class TableRunner:
    """
    Single-threaded command processor for one table.

    Consumes commands from an async queue and executes them
    serially on the PokerTableEngine.
    """

    def __init__(
        self,
        table_id: str,
        config: TableConfig,
        hand_logger: Optional["HandLogger"] = None,
    ):
        self._engine = PokerTableEngine(table_id, config)
        self._config = config
        self._queue: asyncio.Queue[TableCommand] = asyncio.Queue()
        self._user_seats: dict[str, int] = {}  # user_id -> seat
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._hand_buffer = HandBuffer()
        self._hand_logger = hand_logger
        self._blitz_mode: bool = False
        self._human_seat: Optional[int] = None  # Track which seat is human for blitz mode

    @property
    def table_id(self) -> str:
        return self._engine.table_id

    @property
    def player_count(self) -> int:
        return len(self._user_seats)

    def has_open_seats(self) -> bool:
        return self.player_count < self._config.max_players

    def has_human_players(self) -> bool:
        """Check if any human (non-bot) players are seated."""
        for user_id in self._user_seats:
            if not user_id.startswith(("bot_", "user_bot_")):
                return True
        return False

    def has_user(self, user_id: str) -> bool:
        return user_id in self._user_seats

    def set_blitz_mode(self, enabled: bool, human_seat: Optional[int] = None) -> None:
        """Enable or disable blitz mode for this table."""
        self._blitz_mode = enabled
        self._human_seat = human_seat

    async def submit(self, command: TableCommand) -> None:
        """Submit a command to this table's queue."""
        await self._queue.put(command)

    def start(self) -> None:
        """Start the command processing loop."""
        if not self._running:
            self._running = True
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        """Stop the command processing loop."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _run(self) -> None:
        """Main processing loop - runs commands serially."""
        while self._running:
            try:
                command = await asyncio.wait_for(
                    self._queue.get(),
                    timeout=0.1
                )
                await self._process(command)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break

    async def _process(self, command: TableCommand) -> None:
        """Process a single command."""
        try:
            if isinstance(command, JoinTableCommand):
                result = self._handle_join(command)
            elif isinstance(command, LeaveTableCommand):
                result = self._handle_leave(command)
            elif isinstance(command, PlayerActionCommand):
                result = self._handle_action(command)
            elif isinstance(command, StartHandCommand):
                result = self._handle_start_hand(command)
            elif isinstance(command, GetSnapshotCommand):
                result = self._handle_snapshot(command)
            elif isinstance(command, GetActionRequestCommand):
                result = self._handle_action_request(command)
            elif isinstance(command, TimeoutActionCommand):
                result = self._handle_timeout(command)
            else:
                raise ValueError(f"Unknown command: {command}")

            command.result_future.set_result(result)
        except Exception as e:
            command.result_future.set_exception(e)

    def _handle_join(self, cmd: JoinTableCommand):
        """Handle player join request."""
        # Find open seat
        seat = self._find_open_seat()
        if seat is None:
            raise ValueError("Table full")

        self._engine.seat_player(seat, cmd.player, cmd.buy_in)
        self._user_seats[cmd.user_id] = seat

        snapshot = self._engine.get_snapshot(seat)
        return (seat, snapshot)

    def _handle_leave(self, cmd: LeaveTableCommand):
        """Handle player leave request."""
        seat = self._user_seats.get(cmd.user_id)
        if seat is None:
            raise ValueError("User not at table")

        chips = self._engine.unseat_player(seat)
        del self._user_seats[cmd.user_id]
        return chips

    def _handle_action(self, cmd: PlayerActionCommand):
        """Handle player action."""
        print(f"TableRunner._handle_action: user={cmd.user_id} hand={cmd.hand_id} action={cmd.action}")

        seat = self._user_seats.get(cmd.user_id)
        if seat is None:
            raise ValueError("User not at table")

        # Check for stale action (race condition with hand completion)
        is_stale = self._engine.is_action_stale(cmd.hand_id)
        print(f"TableRunner: is_stale={is_stale} for hand {cmd.hand_id}")
        if is_stale:
            print(f"TableRunner: Ignoring stale action for hand {cmd.hand_id}")
            return []  # Return empty events, silently ignore

        print(f"TableRunner: Calling apply_action for seat {seat}")
        events = self._engine.apply_action(seat, cmd.action, cmd.amount)

        # Buffer events for logging
        if self._hand_logger and self._hand_buffer.is_active:
            for event in events:
                self._hand_buffer.record_event(event)

        # Check if hand already ended from the action
        hand_already_ended = any(isinstance(e, HandEndedEvent) for e in events)

        # Check for blitz fold: if human folded and blitz_mode enabled, cancel hand
        if (self._blitz_mode and
            cmd.action == ClientAction.FOLD and
            seat == self._human_seat and
            not hand_already_ended):
            # Cancel the hand and award pot to remaining players
            cancel_events = self._engine.cancel_hand()
            events.extend(cancel_events)

            # Buffer cancel events and flush
            if self._hand_logger and self._hand_buffer.is_active:
                for event in cancel_events:
                    self._hand_buffer.record_event(event)
                self._flush_hand_log()
        else:
            # Normal flow: check for hand end in events
            for event in events:
                if isinstance(event, HandEndedEvent):
                    self._flush_hand_log()
                    break

        return events

    def _handle_start_hand(self, cmd: StartHandCommand):
        """Handle start hand request."""
        events = self._engine.start_hand()

        # Start buffering for hand logging
        if self._hand_logger and events:
            # Find the hand_started event
            hand_started = next(
                (e for e in events if isinstance(e, HandStartedEvent)),
                None,
            )
            if hand_started:
                # Capture seat snapshot
                seat_records = self._capture_seat_snapshot()
                self._hand_buffer.start_hand(
                    hand_id=hand_started.hand_id,
                    seats=seat_records,
                    button_seat=hand_started.button_seat,
                )
                # Buffer all events
                for event in events:
                    self._hand_buffer.record_event(event)

        return events

    def _handle_snapshot(self, cmd: GetSnapshotCommand):
        """Handle snapshot request."""
        seat = self._user_seats.get(cmd.user_id)
        if seat is None:
            raise ValueError("User not at table")
        return self._engine.get_snapshot(seat)

    def _handle_action_request(self, cmd: GetActionRequestCommand):
        """Handle action request."""
        seat = self._user_seats.get(cmd.user_id)
        if seat is None:
            raise ValueError("User not at table")
        return self._engine.get_action_request(seat)

    def _handle_timeout(self, cmd: TimeoutActionCommand):
        """Handle timeout action (server-initiated auto-fold/auto-check)."""
        seat = self._user_seats.get(cmd.user_id)
        if seat is None or seat != cmd.seat:
            return []  # Player left or seat changed

        events = []

        # Try to apply auto-action if hand still in progress
        try:
            actor_seat = self._engine.get_actor_seat()
            if actor_seat == seat:
                # Apply auto-action: fold if facing bet, check otherwise
                if cmd.facing_bet:
                    action = ClientAction.FOLD
                else:
                    action = ClientAction.CHECK
                events = self._engine.apply_action(seat, action, None)
        except ValueError:
            pass  # Hand already ended, that's fine

        # Always mark player as sitting out after timeout
        sit_out_event = self._engine.set_sitting_out(seat, True)
        if sit_out_event:
            events.append(sit_out_event)

        # Buffer events for logging
        if self._hand_logger and self._hand_buffer.is_active:
            for event in events:
                self._hand_buffer.record_event(event)

        # Check if hand already ended from the action
        hand_already_ended = any(isinstance(e, HandEndedEvent) for e in events)

        # Check for blitz fold: if human timed out with fold and blitz_mode enabled
        if (self._blitz_mode and
            action == ClientAction.FOLD and
            seat == self._human_seat and
            not hand_already_ended):
            # Cancel the hand and award pot to remaining players
            cancel_events = self._engine.cancel_hand()
            events.extend(cancel_events)

            # Buffer cancel events and flush
            if self._hand_logger and self._hand_buffer.is_active:
                for event in cancel_events:
                    self._hand_buffer.record_event(event)
                self._flush_hand_log()
        else:
            # Normal flow: check for hand end in events
            for event in events:
                if isinstance(event, HandEndedEvent):
                    self._flush_hand_log()
                    break

        return events

    def _find_open_seat(self) -> Optional[int]:
        """Find the first unoccupied seat."""
        occupied = set(self._user_seats.values())
        for i in range(self._config.max_players):
            if i not in occupied:
                return i
        return None

    def _buffer_and_maybe_flush(self, events: list) -> None:
        """Buffer events and flush to logger on hand end."""
        if not self._hand_logger or not self._hand_buffer.is_active:
            return

        for event in events:
            self._hand_buffer.record_event(event)

            # Check for hand end
            if isinstance(event, HandEndedEvent):
                self._flush_hand_log()

    def _flush_hand_log(self) -> None:
        """Flush buffered events to hand logger."""
        if not self._hand_logger:
            return

        hand_id, events, seats, started_at, button_seat = self._hand_buffer.finalize()
        if not hand_id or not events:
            return

        self._hand_logger.log_hand(
            table_id=self.table_id,
            stake_id=self._config.stake_id,
            hand_id=hand_id,
            events=events,
            seat_snapshot=seats,
            started_at=started_at,
            button_seat=button_seat,
            small_blind=self._config.small_blind_cents,
            big_blind=self._config.big_blind_cents,
            hole_cards=self._engine.dealt_hole_cards_by_seat,
        )

    def _capture_seat_snapshot(self) -> list[SeatRecord]:
        """Capture current seat state for logging."""
        seat_records = []
        # Invert user_seats to get seat -> user_id
        seat_to_user = {v: k for k, v in self._user_seats.items()}

        for i in range(self._config.max_players):
            seat_state = self._engine._seats[i]
            if seat_state is not None and seat_state.player:
                user_id = seat_to_user.get(i, "")
                seat_records.append(SeatRecord(
                    seat_index=i,
                    user_id=user_id,
                    display_name=seat_state.player.display_name,
                    starting_stack=seat_state.chips,  # Already an int
                ))

        return seat_records
