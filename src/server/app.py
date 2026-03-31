"""
FastAPI application with WebSocket endpoint.

Entry point for the poker WebSocket server.
"""

import asyncio
import os
import signal
import subprocess
import sys
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException

from ..manager import TableManager
from ..persistence import FirestoreClient, HandLogger
from .connection import ConnectionManager
from .handler import MessageHandler
from .auth import AuthService
from .timer import ActionTimerService
from .reconnect import ReconnectManager
from .logging_config import logger


# Global instances (initialized in lifespan)
manager: Optional[TableManager] = None
connections: Optional[ConnectionManager] = None
handler: Optional[MessageHandler] = None
timer: Optional[ActionTimerService] = None
reconnect_mgr: Optional[ReconnectManager] = None
firestore: Optional[FirestoreClient] = None
hand_logger: Optional[HandLogger] = None

# Bot table management: table_id -> list of (bot_user_id, subprocess.Process)
_bot_processes: dict[str, list[tuple[str, asyncio.subprocess.Process]]] = {}
# Track which human user owns which bot table: user_id -> table_id
_bot_table_owners: dict[str, str] = {}


def _kill_orphan_bot_processes() -> int:
    """Kill any orphan openbot_client processes from previous server runs.

    Returns the number of processes killed.
    """
    try:
        # Find all openbot_client processes
        result = subprocess.run(
            ["pgrep", "-f", "openbot_client"],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            # No processes found
            return 0

        pids = result.stdout.strip().split("\n")
        killed = 0
        for pid in pids:
            if pid:
                try:
                    os.kill(int(pid), signal.SIGTERM)
                    killed += 1
                except (ProcessLookupError, ValueError):
                    pass

        if killed > 0:
            print(f"[STARTUP] Killed {killed} orphan bot process(es)", flush=True)
        return killed
    except Exception as e:
        print(f"[STARTUP] Error killing orphan bots: {e}", flush=True)
        return 0


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan - initialize and cleanup resources."""
    global manager, connections, handler, timer, reconnect_mgr, firestore, hand_logger

    # Kill any orphan bot processes from previous server runs
    _kill_orphan_bot_processes()

    # Initialize persistence layer
    firestore = FirestoreClient()
    hand_logger = HandLogger(firestore)

    manager = TableManager(hand_logger, firestore)
    connections = ConnectionManager()
    auth = AuthService()
    timer = ActionTimerService()
    reconnect_mgr = ReconnectManager(grace_period_seconds=60.0)
    handler = MessageHandler(manager, connections, auth, timer)

    # Set timeout callback
    async def on_timeout(pending) -> None:
        await handler.handle_timeout(pending)

    timer.set_timeout_callback(on_timeout)
    timer.start()

    # Set reconnect grace period expiry callback
    async def on_grace_expired(user_id: str, table_id: str) -> None:
        """Called when a player's grace period expires - actually remove them."""
        try:
            # Only remove if they're still disconnected (not reconnected)
            if not connections.is_connected(user_id):
                # Check if this is a bot table owner
                if user_id in _bot_table_owners:
                    logger.info("Grace period expired for bot table owner", user_id=user_id)
                    await _cleanup_bot_table(user_id)
                else:
                    # Regular table - remove player and return chips
                    chips = await manager.remove_player(user_id)
                    if chips.amount > 0 and firestore:
                        await firestore.add_balance(user_id, chips.amount)
                        logger.info(f"Returned {chips.amount} cents after grace period", user_id=user_id)
                connections.disconnect(user_id)
                logger.info("Player removed after grace period expired", user_id=user_id, table_id=table_id)
        except Exception as e:
            logger.warning(f"Error removing player after grace period: {e}", user_id=user_id)

    reconnect_mgr.set_expiry_callback(on_grace_expired)

    yield

    await timer.stop()
    await manager.shutdown()


app = FastAPI(title="Poker Server", lifespan=lifespan)


@app.get("/health")
async def health():
    """Liveness probe - basic health check."""
    return {"status": "ok"}


@app.get("/ready")
async def ready():
    """
    Readiness probe - checks if service can accept traffic.

    Cloud Run uses this to determine if instance should receive requests.
    """
    checks = {
        "manager": manager is not None,
        "connections": connections is not None,
        "timer": timer is not None and timer._running,
    }

    all_healthy = all(checks.values())

    if all_healthy:
        return {
            "status": "ready",
            "checks": checks,
            "active_tables": len(manager._tables) if manager else 0,
            "active_connections": len(connections._connections) if connections else 0,
        }
    else:
        raise HTTPException(
            status_code=503,
            detail={"status": "not_ready", "checks": checks}
        )


@app.post("/debug/start_hand/{table_id}")
async def debug_start_hand(table_id: str):
    """
    Debug endpoint to start a hand at a table.

    Useful for testing - normally hands would start automatically
    when enough players are seated.
    """
    try:
        error = await handler.handle_start_hand(table_id)
        if error:
            raise HTTPException(status_code=400, detail=error.get("message", "Error"))
        return {"status": "hand_started"}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/debug/tables")
async def debug_list_tables():
    """Debug endpoint to list active tables."""
    tables = []
    for table_id, runner in manager._tables.items():
        tables.append({
            "table_id": table_id,
            "player_count": runner.player_count,
            "has_open_seats": runner.has_open_seats(),
        })
    return {"tables": tables}


@app.post("/debug/force_timeout/{user_id}")
async def debug_force_timeout(user_id: str):
    """
    Debug endpoint to force a timeout for a user.

    Useful for testing - directly triggers the timeout handler
    without waiting for the timer tick.
    """
    pending = timer.get_pending(user_id)
    if not pending:
        raise HTTPException(status_code=404, detail="No pending action for user")

    await handler.handle_timeout(pending)
    timer.clear_deadline(user_id)
    return {"status": "timeout_forced", "user_id": user_id}


@app.get("/debug/hand_logs")
async def debug_list_hand_logs():
    """Debug endpoint to list all hand logs."""
    return {"hand_logs": firestore.get_all_hand_logs()}


@app.get("/debug/hand_logs/{hand_id}")
async def debug_get_hand_log(hand_id: str):
    """Debug endpoint to get a specific hand log."""
    hand_log = firestore.get_hand_log(hand_id)
    if not hand_log:
        raise HTTPException(status_code=404, detail="Hand log not found")
    return hand_log


@app.get("/debug/ledger")
async def debug_list_ledger():
    """Debug endpoint to list all ledger entries."""
    return {"ledger_entries": firestore.get_all_ledger_entries()}


@app.get("/debug/ledger/{user_id}")
async def debug_get_user_ledger(user_id: str):
    """Debug endpoint to get ledger entries for a user."""
    return {"ledger_entries": firestore.get_ledger_entries(user_id)}


@app.post("/debug/add_bots/{table_id}")
async def debug_add_bots(table_id: str, count: int = 1):
    """
    Debug endpoint to add bot players to a table.

    Bots are added but don't auto-play - use /debug/start_hand to begin.
    Broadcasts seat updates to all connected clients at the table.
    """
    from ..models import PlayerIdentity, Chips, Seat, SeatStatus
    from .config import config
    import random
    import string

    if table_id not in manager._tables:
        raise HTTPException(status_code=404, detail=f"Table {table_id} not found")

    runner = manager._tables[table_id]
    stake_id = runner._config.stake_id
    bots_added = []

    for i in range(count):
        bot_id = f"bot_{''.join(random.choices(string.ascii_lowercase, k=6))}"
        bot_name = f"Bot{random.randint(1, 999)}"

        try:
            player = PlayerIdentity(
                user_id=bot_id,
                display_name=bot_name,
                avatar_url=None,
            )
            buy_in = Chips(amount=config.default_max_buy_in_cents // 2)

            # Use manager.add_player which handles table assignment
            result_table_id, seat = await manager.add_player(
                bot_id, stake_id, buy_in, player
            )

            bots_added.append({
                "bot_id": bot_id,
                "display_name": bot_name,
                "seat": seat,
                "chips": buy_in.amount,
            })

            # Broadcast SEAT_UPDATE to all connected clients at this table
            seat_update = {
                "type": "SEAT_UPDATE",
                "seat": {
                    "seat_index": seat,
                    "status": "seated",
                    "player": {
                        "user_id": bot_id,
                        "display_name": bot_name,
                        "avatar_url": None,
                    },
                    "chips": {"amount": buy_in.amount},
                    "bet": {"amount": 0},
                    "is_button": False,
                    "is_connected": True,
                },
            }
            await connections.broadcast_to_table(table_id, seat_update)

        except ValueError as e:
            # Table might be full
            break

    return {
        "table_id": table_id,
        "bots_added": bots_added,
        "player_count": runner.player_count,
    }


@app.post("/debug/kill_bots/{table_id}")
async def debug_kill_bots(table_id: str):
    """Debug endpoint to remove all bot players from a table."""
    if table_id not in manager._tables:
        raise HTTPException(status_code=404, detail=f"Table {table_id} not found")

    # Find all bot user_ids at this table
    bot_ids = [uid for uid in manager._user_tables if uid.startswith("bot_") and manager._user_tables[uid] == table_id]

    removed = []
    for bot_id in bot_ids:
        try:
            await manager.remove_player(bot_id)
            removed.append(bot_id)
        except Exception:
            pass

    runner = manager._tables.get(table_id)
    return {
        "table_id": table_id,
        "bots_removed": removed,
        "player_count": runner.player_count if runner else 0,
    }


@app.post("/debug/reset_table/{table_id}")
async def debug_reset_table(table_id: str):
    """Debug endpoint to completely reset a table (end hand, remove all players)."""
    if table_id not in manager._tables:
        raise HTTPException(status_code=404, detail=f"Table {table_id} not found")

    runner = manager._tables[table_id]

    # Get all players at this table
    player_ids = [uid for uid, tid in manager._user_tables.items() if tid == table_id]

    # Remove all players
    removed = []
    for player_id in player_ids:
        try:
            await manager.remove_player(player_id)
            removed.append(player_id)
        except Exception:
            pass

    # Delete the table
    if table_id in manager._tables:
        del manager._tables[table_id]

    return {
        "table_id": table_id,
        "players_removed": removed,
        "status": "table_deleted",
    }


_OPENBOT_CWD = os.environ.get("OPENBOT_DIR", "/home/de2425/openbot")
_OPENBOT_PYTHON = os.environ.get(
    "OPENBOT_PYTHON",
    os.path.join(_OPENBOT_CWD, "venv", "bin", "python"),
)
_OPENBOT_POLICY = os.environ.get("OPENBOT_POLICY", "/home/de2425/policy_iter200M.db")
_OPENBOT_HU_POLICY = os.environ.get("OPENBOT_HU_POLICY", "/home/de2425/policy_2m.db")
_USE_PREFLOP_DB = os.environ.get("USE_PREFLOP_DB", "false").lower() == "true"  # Toggle: use preflop_ranges.db or main policy
_SOLVER_BIN = os.environ.get("SOLVER_BIN", "/home/de2425/poker_solver/cpp/build/river_solver_optimized")


async def _spawn_bot(
    table_id: str,
    bot_index: int,
    stake_id: str = "nlh_1_2",
    buy_in_cents: int = 20000,
) -> tuple[str, asyncio.subprocess.Process]:
    """Spawn an OpenBot policy client subprocess that connects via websocket.

    DEPRECATED: Use _spawn_bot_process() for multi-bot mode instead.
    This function is kept for backwards compatibility with single-bot spawning.
    """
    bot_user_id = f"user_bot_{table_id}_{bot_index}"
    display_name = f"Bot{bot_index + 1}"

    cmd = [
        _OPENBOT_PYTHON, "-m", "src.serving.openbot_client",
        "--server", "ws://localhost:8000/ws",
        "--table-id", table_id,
        "--user-id", bot_user_id,
        "--policy", _OPENBOT_POLICY,
        "--hu-policy", _OPENBOT_HU_POLICY,
        "--display-name", display_name,
        "--stake", stake_id,
        "--buy-in", str(buy_in_cents),
        "--solver-bin", _SOLVER_BIN,
    ]
    if _USE_PREFLOP_DB:
        cmd.extend(["--preflop-db", "preflop_ranges.db"])

    log_path = os.path.join(_OPENBOT_CWD, f"bot_{bot_index}.log")
    log_file = open(log_path, "w")
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=log_file,
        stderr=asyncio.subprocess.STDOUT,
        cwd=_OPENBOT_CWD,
    )
    print(f"[BOT] Spawned OpenBot {bot_user_id} (pid={proc.pid}) for table {table_id}, log={log_path}", flush=True)
    return bot_user_id, proc


_AGGRESSION_BIAS = float(os.environ.get("BOT_AGGRESSION_BIAS", "1.5"))
_SERVER_PORT = os.environ.get("PORT", "8000")


async def _spawn_bot_process(
    table_id: str,
    bot_count: int,
    stake_id: str = "nlh_1_2",
    buy_in_cents: int = 20000,
    persona_ids: list[str] | None = None,
    bot_ids: list[str] | None = None,
) -> asyncio.subprocess.Process:
    """Spawn ONE process with multiple bots for a table.

    This consolidates N bots into a single process, reducing memory from
    N * ~150MB to ~200MB total by sharing policy stores and abstraction LUTs.

    Args:
        table_id: Table ID for bots to join
        bot_count: Number of bots to run in this process
        stake_id: Stake identifier
        buy_in_cents: Buy-in amount per bot

    Returns:
        The subprocess.Process handle
    """
    cmd = [
        _OPENBOT_PYTHON, "-m", "src.serving.openbot_client",
        "--server", f"ws://localhost:{_SERVER_PORT}/ws",
        "--table-id", table_id,
        "--num-bots", str(bot_count),
        "--policy", _OPENBOT_POLICY,
        "--hu-policy", _OPENBOT_HU_POLICY,
        "--abstraction-dir", "models/checkpoints",
        "--stake", stake_id,
        "--buy-in", str(buy_in_cents),
        "--solver-bin", _SOLVER_BIN,
        "--aggression-bias", str(_AGGRESSION_BIAS),
    ]
    if _USE_PREFLOP_DB:
        cmd.extend(["--preflop-db", "preflop_ranges.db"])
    if persona_ids:
        cmd.extend(["--persona", ",".join(persona_ids)])
    if bot_ids:
        cmd.extend(["--display-names", ",".join(bot_ids)])

    log_path = os.path.join(_OPENBOT_CWD, f"bot_table_{table_id}.log")
    log_file = open(log_path, "w")
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=log_file,
        stderr=asyncio.subprocess.STDOUT,
        cwd=_OPENBOT_CWD,
    )
    print(f"[BOT] Spawned {bot_count} bots in single process (pid={proc.pid}) for table {table_id}, personas={persona_ids}, log={log_path}", flush=True)
    return proc


async def _create_bot_table(
    user_id: str,
    stake_id: str,
    buy_in_cents: int,
    display_name: str,
    bot_count: int,
    auto_top_up: bool = True,
    blitz_mode: bool = False,
    persona_ids: list[str] | None = None,
    bot_ids: list[str] | None = None,
) -> dict:
    """Create a table, seat the human, then spawn bot subprocess clients."""
    from ..models import PlayerIdentity, Chips

    player = PlayerIdentity(
        user_id=user_id,
        display_name=display_name,
        avatar_url=None,
    )
    buy_in = Chips(amount=buy_in_cents)

    # Seat the human player via normal join flow
    table_id, seat = await manager.add_player(user_id, stake_id, buy_in, player)
    connections.join_table(user_id, table_id)

    # Set auto top-up preference on the player's seat and blitz mode on runner
    runner = manager._tables.get(table_id)
    if runner:
        # Set blitz mode on the runner
        runner.set_blitz_mode(blitz_mode, human_seat=seat)
        print(f"[BOT_TABLE] Set blitz_mode={blitz_mode} for table {table_id}")

        if seat < len(runner._engine._seats):
            seat_state = runner._engine._seats[seat]
            if seat_state:
                seat_state.auto_topup_enabled = auto_top_up
                print(f"[BOT_TABLE] Set auto_topup_enabled={auto_top_up} for seat {seat}")

    # Track ownership
    _bot_table_owners[user_id] = table_id

    # Get snapshot for human
    snapshot = await manager.get_snapshot(user_id)

    # Log persona and bot_id selection
    if persona_ids:
        print(f"[BOT_TABLE] Using personas: {persona_ids}")
    else:
        print(f"[BOT_TABLE] No personas - using normal GTO bots")
    if bot_ids:
        print(f"[BOT_TABLE] Using bot_ids: {bot_ids}")
    else:
        print(f"[BOT_TABLE] No bot_ids - using default Bot1, Bot2, etc.")

    # Spawn single process with all bots (memory efficient: ~200MB vs 5 * 150MB)
    proc = await _spawn_bot_process(
        table_id=table_id,
        bot_count=bot_count,
        stake_id=stake_id,
        buy_in_cents=buy_in_cents,
        persona_ids=persona_ids,
        bot_ids=bot_ids,
    )
    # Store as list with single entry for compatibility with cleanup code
    _bot_processes[table_id] = [("bot_table_process", proc)]

    # Wait for bots to connect and seat themselves (policy bots load slower)
    for attempt in range(100):  # Up to 10 seconds
        runner = manager._tables.get(table_id)
        if runner and runner.player_count >= bot_count + 1:
            break
        await asyncio.sleep(0.1)

    print(f"[BOT] All {bot_count} bots seated at {table_id}", flush=True)

    # Re-fetch snapshot after bots are seated
    snapshot = await manager.get_snapshot(user_id)
    return snapshot.model_dump(mode="json")


async def _cleanup_bot_table(user_id: str) -> None:
    """Clean up bot table when the human owner disconnects/leaves."""
    table_id = _bot_table_owners.pop(user_id, None)
    if not table_id:
        return

    print(f"[BOT] Cleaning up bot table {table_id}", flush=True)

    # First, get the human's chips and return them to wallet BEFORE removing
    try:
        runner = manager._tables.get(table_id)
        if runner and firestore:
            # Find human's seat and chips
            for seat_idx, seat_state in enumerate(runner._engine._seats):
                if seat_state and seat_state.player and seat_state.player.user_id == user_id:
                    chips_to_return = seat_state.chips
                    if chips_to_return > 0:
                        await firestore.add_balance(user_id, chips_to_return)
                        print(f"[BOT] Returned {chips_to_return} cents to {user_id}", flush=True)
                    break
    except Exception as e:
        print(f"[BOT] Error returning chips: {e}", flush=True)

    # Terminate bot processes using stored handles
    bot_procs = _bot_processes.pop(table_id, [])
    for bot_user_id, proc in bot_procs:
        try:
            proc.terminate()
            await asyncio.wait_for(proc.wait(), timeout=2.0)
            print(f"[BOT] Terminated bot process pid={proc.pid}", flush=True)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    # Fallback: kill any bot processes for this table by table_id in command line
    # This catches orphans if process handles are stale or tracking failed
    try:
        result = subprocess.run(
            ["pkill", "-f", f"openbot_client.*--table-id {table_id}"],
            capture_output=True,
        )
        if result.returncode == 0:
            print(f"[BOT] Fallback pkill cleaned up processes for {table_id}", flush=True)
    except Exception as e:
        print(f"[BOT] Fallback pkill failed: {e}", flush=True)

    # Remove bot players from manager (don't need to return their chips)
    for bot_user_id, _ in bot_procs:
        try:
            await manager.remove_player(bot_user_id)
        except Exception:
            pass

    # Remove human player from manager tracking
    try:
        # Use a direct removal that skips the normal leave flow since we already handled chips
        if user_id in manager._user_tables:
            del manager._user_tables[user_id]
    except Exception:
        pass

    # Delete the table
    if table_id in manager._tables:
        runner = manager._tables[table_id]
        await runner.stop()
        del manager._tables[table_id]

    print(f"[BOT] Bot table {table_id} cleaned up", flush=True)


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """
    Main WebSocket endpoint.

    Protocol:
    1. Client connects
    2. Client sends AUTH with token
    3. Server validates and responds AUTH_OK
    4. Client sends JOIN_POOL to join a table
    5. Server responds with TABLE_SNAPSHOT
    6. Game loop: client sends ACTIONs, server broadcasts STATE_DELTAs
    """
    user_id: Optional[str] = None

    try:
        # Must accept before receiving
        await websocket.accept()

        # Wait for AUTH message first
        data = await websocket.receive_json()

        if data.get("type") != "AUTH":
            await websocket.send_json({
                "type": "ERROR",
                "code": "bad_request",
                "message": "First message must be AUTH"
            })
            await websocket.close(code=4001)
            return

        token = data.get("token", "")
        auth = AuthService()
        user_id = auth.verify_token(token)

        if not user_id:
            await websocket.send_json({
                "type": "ERROR",
                "code": "unauthorized",
                "message": "Invalid token"
            })
            await websocket.close(code=4001)
            return

        # Register connection (after accept, but before sending AUTH_OK)
        # Note: connect() no longer calls accept() since we did it above
        if user_id in connections._connections:
            old_ws = connections._connections[user_id]
            try:
                await old_ws.close()
            except Exception:
                pass
        connections._connections[user_id] = websocket

        # Cancel any pending grace period - player has reconnected
        was_in_grace_period = reconnect_mgr.cancel_grace_period(user_id)
        if was_in_grace_period:
            logger.info("Player reconnected within grace period", user_id=user_id)

        # Send AUTH_OK
        response = await handler.handle_auth(
            user_id, token, data.get("protocol_version", 1)
        )
        await websocket.send_json(response)

        # If reconnecting and already at table, send snapshot
        # This can happen either from normal reconnect OR from grace period reconnect
        if response.get("current_table_id"):
            table_id = response.get("current_table_id")
            connections.join_table(user_id, table_id)
            try:
                snapshot = await manager.get_snapshot(user_id)
                await websocket.send_json(snapshot.model_dump(mode="json"))
                if was_in_grace_period:
                    logger.info("Sent snapshot to reconnected player", user_id=user_id, table_id=table_id)

                # Check if it's this player's turn - re-send ACTION_REQUEST if so
                if snapshot.hand:
                    actor_seat = snapshot.hand.actor_seat
                    your_seat = snapshot.your_seat
                    if actor_seat is not None and actor_seat == your_seat:
                        logger.info("Reconnected player needs to act, sending ACTION_REQUEST", user_id=user_id, seat=your_seat)
                        await handler._send_action_request(user_id, snapshot)
            except Exception as e:
                logger.warning(f"Failed to send snapshot on reconnect: {e}", user_id=user_id)

        # Message loop
        while True:
            data = await websocket.receive_json()
            msg_type = data.get("type")

            # Debug: log every message received
            if msg_type == "ACTION":
                print(f"[RECV] {user_id}: ACTION received hand={data.get('hand_id')} action={data.get('action')}", flush=True)

            if msg_type == "JOIN_POOL":
                response, table_id, seat, display_name, buy_in = await handler.handle_join_pool(
                    user_id,
                    data.get("stake_id"),
                    data.get("buy_in_cents"),
                    data.get("display_name", "Player"),
                )
                await websocket.send_json(response)
                # Complete join AFTER sending response to avoid race condition
                if table_id is not None:
                    await handler.complete_join(user_id, table_id, seat, display_name, buy_in)

            elif msg_type == "JOIN_TABLE":
                response, table_id, seat, display_name, buy_in = await handler.handle_join_table(
                    user_id,
                    data.get("table_id"),
                    data.get("stake_id", "nlh_1_2"),
                    data.get("buy_in_cents", 20000),
                    data.get("display_name", "Player"),
                )
                await websocket.send_json(response)
                # Complete join AFTER sending response to avoid race condition
                if table_id is not None:
                    await handler.complete_join(user_id, table_id, seat, display_name, buy_in)

            elif msg_type == "CREATE_BOT_TABLE":
                try:
                    # Auto-calculate bot_count from stake's max_players if not provided
                    stake_id = data.get("stake_id", "nlh_1_2")
                    stake_config = manager._stake_configs.get(stake_id)
                    default_bot_count = (stake_config.max_players - 1) if stake_config else 5

                    response = await _create_bot_table(
                        user_id=user_id,
                        stake_id=stake_id,
                        buy_in_cents=data.get("buy_in_cents", 20000),
                        display_name=data.get("display_name", "Player"),
                        bot_count=data.get("bot_count") or default_bot_count,
                        auto_top_up=data.get("auto_top_up", True),
                        blitz_mode=data.get("blitz_mode", False),
                        persona_ids=data.get("persona_ids"),
                        bot_ids=data.get("bot_ids"),
                    )
                    await websocket.send_json(response)
                except Exception as e:
                    await websocket.send_json({
                        "type": "ERROR",
                        "code": "bad_request",
                        "message": str(e),
                    })

            elif msg_type == "ACTION":
                response = await handler.handle_action(
                    user_id,
                    data.get("hand_id"),
                    data.get("action_id"),
                    data.get("action"),
                    data.get("amount_cents"),
                )
                if response:  # Error response
                    await websocket.send_json(response)

            elif msg_type == "LEAVE_TABLE":
                response = await handler.handle_leave_table(user_id)
                await websocket.send_json(response)

            elif msg_type == "NEXT_HAND":
                response = await handler.handle_next_hand(user_id)
                if response:  # Error response
                    await websocket.send_json(response)

            elif msg_type == "PING":
                response = await handler.handle_ping(
                    user_id, data.get("client_ts", 0)
                )
                await websocket.send_json(response)

            elif msg_type == "TOP_UP_REQUEST":
                response = await handler.handle_topup_request(
                    user_id, data.get("request_id", "")
                )
                await websocket.send_json(response)

            elif msg_type == "SET_AUTO_TOP_UP":
                response = await handler.handle_set_auto_top_up(
                    user_id, data.get("enabled", True)
                )
                await websocket.send_json(response)

            elif msg_type == "QUIP":
                # Bot quip message - broadcast to table
                await handler.handle_quip(
                    user_id,
                    data.get("hand_id", ""),
                    data.get("seat", 0),
                    data.get("text", ""),
                )
                # No response needed - just broadcast

            else:
                await websocket.send_json({
                    "type": "ERROR",
                    "code": "unknown_message_type",
                    "message": f"Unknown message type: {msg_type}"
                })

    except WebSocketDisconnect:
        logger.info("WebSocket disconnected", user_id=user_id)
    except Exception as e:
        # Log unexpected errors but don't crash
        logger.exception(f"WebSocket error: {e}", user_id=user_id)
    finally:
        if user_id:
            # Remove WebSocket from active connections
            connections._connections.pop(user_id, None)

            # Check bot table owners FIRST - clean up immediately (no grace period)
            if user_id in _bot_table_owners:
                logger.info("Bot table owner disconnected, cleaning up immediately", user_id=user_id)
                await _cleanup_bot_table(user_id)
            else:
                # Regular players: start grace period for reconnection
                table_id = manager.get_table_for_user(user_id)
                if table_id and not user_id.startswith(("bot_", "user_bot_")):
                    reconnect_mgr.start_grace_period(user_id, table_id)
                    logger.info("Grace period started for disconnected player", user_id=user_id, table_id=table_id)
                else:
                    # No table - just clean up connection tracking
                    connections.disconnect(user_id)
