# Poker Backend

Real-time poker game server using WebSockets, PokerKit engine, and Firebase auth.

## Quick Start

### Local Development
```bash
cd poker_backend
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
uvicorn src.server.app:app --host 0.0.0.0 --port 8000
```

### Production Server (162.222.177.28)
```bash
ssh de2425@162.222.177.28
cd ~/poker_backend
source venv/bin/activate
uvicorn src.server.app:app --host 0.0.0.0 --port 8000 > /tmp/poker.log 2>&1 &
```

**Check logs:** `tail -f /tmp/poker.log`
**Kill server:** `pkill -f uvicorn` or `fuser -k 8000/tcp`

## Ports

| Port | Service | Description |
|------|---------|-------------|
| 8000 | Poker WebSocket | Main game server (ws://162.222.177.28:8000/ws) |
| 8001 | Analytics API | Stats/analytics (http://34.57.59.242:8001) |

## Project Structure

```
src/
├── server/          # WebSocket server, handlers, auth
│   ├── app.py       # FastAPI app, startup, timer setup
│   ├── handler.py   # Message routing, action handling
│   ├── timer.py     # Action timeout service (60s)
│   └── connections.py
├── engine/          # Game logic
│   ├── table.py     # PokerTableEngine, PokerKit adapter
│   └── config.py    # Table config (blinds, timeouts)
├── manager/         # Table management
│   ├── runner.py    # TableRunner, command processing
│   └── commands.py  # Command types (Join, Action, Timeout)
├── models/          # Pydantic schemas
│   ├── base.py      # Core types (Card, Chips, Seat, Events)
│   └── messages.py  # Protocol messages (ACTION_REQUEST, etc)
└── persistence/     # Firebase/Firestore integration
```

## Key Configs

**src/engine/config.py:**
- `action_timeout_seconds: int = 60` - Player action timeout
- `small_blind / big_blind` - Blind amounts in cents

## WebSocket Protocol

**Client -> Server:**
- `AUTH` - Authenticate with Firebase token
- `JOIN_POOL` - Join matchmaking queue
- `ACTION` - fold/check/call/bet/raise_to
- `LEAVE_TABLE` - Exit table

**Server -> Client:**
- `AUTH_OK` - Auth successful
- `TABLE_SNAPSHOT` - Full table state
- `STATE_DELTA` - Game events (actions, cards dealt)
- `ACTION_REQUEST` - Your turn to act (includes timer)

## Testing

```bash
pytest                           # All tests
pytest tests/test_engine.py      # Engine tests only
pytest -v                        # Verbose output
```

## Common Tasks

### Restart Server
```bash
ssh de2425@162.222.177.28 'pkill -f uvicorn; cd ~/poker_backend && nohup ./venv/bin/uvicorn src.server.app:app --host 0.0.0.0 --port 8000 > /tmp/poker.log 2>&1 &'
```

### Sync Code to Server
```bash
rsync -avz --exclude 'venv' --exclude '__pycache__' --exclude '.git' /Users/davideyal/Projects/stack_poker/poker_backend/ de2425@162.222.177.28:~/poker_backend/
```

**IMPORTANT: Local/Server Code Sync**
- When making changes via `sed` or direct edits on the server, ALWAYS apply the same changes locally
- Syncing local code to server will OVERWRITE server-only changes
- After server hotfixes, copy files back: `scp de2425@162.222.177.28:~/path/file ./local/path/`
- Key files that need sync:
  - `openbot/src/serving/turn_solver_cpp.py` - C++ turn solver integration
  - `openbot/src/serving/turn_solver.py` - Python turn solver
  - `openbot/src/translation/translator.py` - Bot action translation
  - `openbot/src/abstraction/paaemd/canonicalization.py` - LUT loading

### Debug Timeouts
Look for these log patterns:
- `[TIMER_REG]` - Timer registered for player
- `[TIMER] Timeout expired` - Timer fired
- `[TIMEOUT] Processing` - Handling timeout action

## iOS Client Connection

The iOS app connects to: `ws://162.222.177.28:8000/ws`

Configured in: `stackpoker/stack/Features/PokerTable/Service/PokerWebSocketManager.swift`
