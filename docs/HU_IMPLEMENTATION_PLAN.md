# Heads-Up (HU) Implementation Plan

**Last Verified:** 2026-03-26

## Verification Status

All line numbers and code snippets verified against actual codebase:

| File | Line(s) | Status |
|------|---------|--------|
| `src/engine/table.py` | 155-157 | ✅ Verified `>= 3` check |
| `src/engine/config.py` | 8-22 | ✅ Verified no `min_players_to_start` |
| `src/manager/runner.py` | 62-63 | ✅ Verified `< 6` check |
| `src/manager/runner.py` | 306-312 | ✅ Verified `range(6)` loop |
| `src/manager/runner.py` | 354 | ✅ Verified `range(6)` loop |
| `src/server/handler.py` | 90, 130 | ✅ Verified `>= 3` checks |
| `openbot/positions.py` | 21, 56 | ✅ Verified 6-max mapping |
| `openbot/translator.py` | 456-483 | ✅ Verified info state building |
| `openbot/openbot_client.py` | 742 | ✅ Verified `--policy` arg |

---

## Overview

This document outlines the step-by-step plan to add HU (2-player) table support to the poker backend and bot serving infrastructure. Each phase is designed to be independently testable.

**Current State:**
- PokerKit engine already supports 2-player games natively
- HU game configs already exist in openbot (`hu_100bb`, `headsup_20bb`)
- Turn/River solvers already have HU-aware code paths
- Main blockers are hardcoded assumptions in our wrapper code

**Scope:** Backend + Bot Serving only (iOS is separate)

---

## Phase 1: Backend - Remove Player Count Blockers

**Goal:** Allow 2-player games to start

**Files to modify:**
- `src/engine/table.py`
- `src/engine/config.py`

### Step 1.1: Add min_players config field

```python
# src/engine/config.py - TableConfig class
@dataclass
class TableConfig:
    stake_id: str
    small_blind_cents: int
    big_blind_cents: int
    min_buy_in_cents: int
    max_buy_in_cents: int
    max_players: int = 6
    min_players_to_start: int = 3  # ADD THIS - 2 for HU, 3 for 6-max
    action_timeout_seconds: int = 60
```

### Step 1.2: Update can_start_hand()

```python
# src/engine/table.py lines 155-157 (VERIFIED)
# BEFORE:
def can_start_hand(self) -> bool:
    """True if we have enough players to start a hand (minimum 3)."""
    return len(self._get_active_seats()) >= 3

# AFTER:
def can_start_hand(self) -> bool:
    """True if we have enough players to start a hand."""
    return len(self._get_active_seats()) >= self._config.min_players_to_start
```

### Step 1.3: Add HU stake config

```python
# src/manager/manager.py - add to _stake_configs dict
"nlh_1_2_hu": TableConfig(
    stake_id="nlh_1_2_hu",
    small_blind_cents=100,
    big_blind_cents=200,
    min_buy_in_cents=4000,
    max_buy_in_cents=40000,
    max_players=2,
    min_players_to_start=2,
),
```

### Test Phase 1:
```bash
# Unit test
pytest tests/test_engine.py -k "test_hu" -v

# Manual test
python -c "
from src.engine.config import TableConfig
from src.engine.table import PokerTableEngine

config = TableConfig(
    stake_id='test_hu',
    small_blind_cents=100,
    big_blind_cents=200,
    min_buy_in_cents=4000,
    max_buy_in_cents=40000,
    max_players=2,
    min_players_to_start=2,
)
engine = PokerTableEngine(config)
engine.seat_player(0, 'p1', 20000)
engine.seat_player(1, 'p2', 20000)
print(f'Can start: {engine.can_start_hand()}')  # Should be True
"
```

---

## Phase 2: Backend - Fix Hardcoded Loops

**Goal:** All seat iteration respects max_players

**Files to modify:**
- `src/manager/runner.py`
- `src/server/handler.py`

### Step 2.1: Fix runner.py seat loops

```python
# src/manager/runner.py lines 62-63 (VERIFIED)
# BEFORE:
def has_open_seats(self) -> bool:
    return self.player_count < 6

# AFTER:
def has_open_seats(self) -> bool:
    return self.player_count < self._config.max_players
```

```python
# src/manager/runner.py lines 306-312 (VERIFIED)
# BEFORE:
def _find_open_seat(self) -> Optional[int]:
    """Find the first unoccupied seat."""
    occupied = set(self._user_seats.values())
    for i in range(6):
        if i not in occupied:
            return i
    return None

# AFTER:
def _find_open_seat(self) -> Optional[int]:
    """Find the first unoccupied seat."""
    occupied = set(self._user_seats.values())
    for i in range(self._config.max_players):
        if i not in occupied:
            return i
    return None
```

```python
# src/manager/runner.py line 354 (VERIFIED) - in _capture_seat_snapshot()
# BEFORE:
for i in range(6):

# AFTER:
for i in range(self._config.max_players):
```

### Step 2.2: Fix handler.py auto-start threshold

```python
# src/server/handler.py lines 90 and 130 (VERIFIED - both locations)
# BEFORE:
if player_count >= 3 and not has_hand:
    asyncio.create_task(self._auto_start_next_hand(table_id, delay=1.5))

# AFTER:
min_players = runner._config.min_players_to_start
if player_count >= min_players and not has_hand:
    asyncio.create_task(self._auto_start_next_hand(table_id, delay=1.5))
```

### Test Phase 2:
```bash
# Start server locally
uvicorn src.server.app:app --port 8000

# Connect 2 test clients to HU table
# Verify hand starts with 2 players
```

---

## Phase 3: Backend - Verify HU Blind Logic

**Goal:** Confirm button/blind posting works for 2 players

**Investigation needed:**
The current code reorders seats so SB is always index 0:
```python
# table.py lines 205-209
button_idx = active_seats.index(self._button_seat)
sb_idx = (button_idx + 1) % len(active_seats)
self._active_seat_indices = active_seats[sb_idx:] + active_seats[:sb_idx]
```

For HU (2 players):
- If button is seat 0: SB=seat 0, BB=seat 1 → reordered to [0, 1]
- If button is seat 1: SB=seat 1, BB=seat 0 → reordered to [1, 0]

**This should work correctly for HU.** PokerKit handles the rest.

### Test Phase 3:
```python
# Manual verification script
"""
1. Create HU table
2. Seat 2 players
3. Start 3+ hands
4. Log button position, SB poster, BB poster each hand
5. Verify correct rotation:
   - Hand 1: P0=BTN/SB, P1=BB
   - Hand 2: P1=BTN/SB, P0=BB
   - Hand 3: P0=BTN/SB, P1=BB
"""
```

---

## Phase 4: Protocol - Add Table Type

**Goal:** Client can request HU vs 6-max table

**Files to modify:**
- `src/server/handler.py`
- `src/models/messages.py`

### Step 4.1: Update JOIN_POOL handling

```python
# src/server/handler.py - handle_join_pool()
async def handle_join_pool(
    self,
    user_id: str,
    stake_id: str,
    buy_in_cents: int,
    display_name: str,
    table_type: str = "6max"  # NEW PARAM: "hu" or "6max"
) -> tuple[dict, str, int]:

    # Map to correct stake config
    if table_type == "hu":
        effective_stake_id = f"{stake_id}_hu"  # e.g., "nlh_1_2_hu"
    else:
        effective_stake_id = stake_id

    # ... rest of logic uses effective_stake_id
```

### Step 4.2: Add max_players to TABLE_SNAPSHOT

```python
# src/models/messages.py or wherever TableSnapshotMessage is defined
class TableSnapshotMessage:
    # ... existing fields
    max_players: int  # ADD THIS
```

```python
# src/engine/table.py - get_snapshot()
def get_snapshot(self, for_seat: int) -> TableSnapshotMessage:
    return TableSnapshotMessage(
        # ... existing fields
        max_players=self._config.max_players,  # ADD THIS
    )
```

### Test Phase 4:
```bash
# WebSocket test
wscat -c ws://localhost:8000/ws

# Send:
{"type": "AUTH", "token": "test"}
{"type": "JOIN_POOL", "stake_id": "nlh_1_2", "table_type": "hu", "buy_in_cents": 20000}

# Verify TABLE_SNAPSHOT includes max_players: 2
```

---

## Phase 5: Bot Serving - Policy Loading

**Goal:** Bot can load and use HU policy

**Files to modify (on .28 server):**
- `openbot/src/serving/openbot_client.py`

### Step 5.1: Add HU policy flag

```python
# openbot_client.py - argument parsing
parser.add_argument(
    "--hu-policy",
    type=str,
    default=None,
    help="Path to HU policy database (optional)"
)
```

### Step 5.2: Load both policies

```python
# openbot_client.py - initialization
self.policy_6max = PolicyStore(args.policy)
self.policy_hu = PolicyStore(args.hu_policy) if args.hu_policy else None
```

### Step 5.3: Export HU policy (when training done)

```bash
# On Azure VM after training converges
cd /mnt/data/openbot
python scripts/export_policy.py \
    --checkpoint models/hu_gto_proper/checkpoints/latest.pkl.gz \
    --output models/hu_policy.db \
    --verify

# Copy to .28 server
scp models/hu_policy.db de2425@162.222.177.28:~/openbot/models/
```

### Test Phase 5:
```bash
# On .28 server
python -c "
from src.serving.policy_store import PolicyStore
hu_policy = PolicyStore('models/hu_policy.db')
print(f'Loaded {hu_policy.count()} states')
# Query a known HU state
probs = hu_policy.get_action_probs('0|p0:b12:h')
print(f'AA open probs: {probs}')
"
```

---

## Phase 6: Bot Serving - Game Detection & Routing

**Goal:** Bot uses correct policy based on table type

**Files to modify:**
- `openbot/src/translation/translator.py`
- `openbot/src/translation/positions.py`

### Step 6.1: Detect game type

```python
# translator.py - add method
def _detect_game_type(self) -> str:
    """Detect if this is HU or 6-max based on active seats."""
    if len(self._active_seats) <= 2:
        return "hu"
    return "6max"
```

### Step 6.2: Route policy lookups

```python
# translator.py - in get_recommended_action() or similar
game_type = self._detect_game_type()
if game_type == "hu" and self.policy_hu:
    probs = self.policy_hu.get_action_probs(info_state)
else:
    probs = self.policy_6max.get_action_probs(info_state)
```

### Step 6.3: Fix position mapping for HU

```python
# positions.py - add HU-specific mapping
def seat_to_openbot_player(my_seat: int, button_seat: int, active_seats: List[int]) -> int:
    num_players = len(active_seats)

    if num_players == 2:
        # HU: Simple mapping - BTN/SB=0, BB=1
        # Position after button determines player index
        button_idx = active_seats.index(button_seat)
        my_idx = active_seats.index(my_seat)
        position = (my_idx - button_idx) % 2
        return position  # 0 for BTN/SB, 1 for BB
    else:
        # 6-max: existing logic
        # ... current implementation
```

### Test Phase 6:
```bash
# Join HU table with bot, verify:
# 1. Bot detects game_type = "hu"
# 2. Bot uses HU policy for preflop
# 3. Position mapping produces correct player indices
```

---

## Phase 7: Bot Spawning

**Goal:** Server spawns correct bot config for HU tables

**Files to modify:**
- `src/server/app.py`

### Step 7.1: Update bot spawn logic

```python
# app.py - _spawn_bot_process() or _create_bot_table()
def _create_bot_table(self, stake_id: str, bot_count: int = None):
    config = self._get_stake_config(stake_id)

    # Auto-determine bot count from table size
    if bot_count is None:
        bot_count = config.max_players - 1  # Leave 1 seat for human

    # Validate
    if bot_count >= config.max_players:
        raise ValueError(f"Too many bots ({bot_count}) for {config.max_players}-max table")

    # ... spawn with correct count
```

### Step 7.2: Pass game type to bot

```python
# app.py - bot spawn command
cmd = [
    _OPENBOT_PYTHON, "-m", "src.serving.openbot_client",
    "--server", f"ws://localhost:{_SERVER_PORT}/ws",
    "--table-id", table_id,
    "--num-bots", str(bot_count),
    "--policy", _OPENBOT_POLICY,
    "--hu-policy", _OPENBOT_HU_POLICY,  # ADD THIS
    # ... other args
]
```

### Test Phase 7:
```bash
# API call to create HU bot table
curl -X POST http://localhost:8000/api/create_bot_table \
    -H "Content-Type: application/json" \
    -d '{"stake_id": "nlh_1_2_hu", "bot_count": 1}'

# Verify:
# 1. Only 1 bot joins
# 2. Bot has --hu-policy flag set
```

---

## Phase 8: Integration Testing

**Goal:** Full HU game works end-to-end

### Test Script:
```python
"""
integration_test_hu.py

1. Start local server with HU support
2. Create HU bot table via API
3. Connect test client
4. Play through complete hand:
   - Verify blind posting (BTN posts SB)
   - Make preflop action
   - See flop/turn/river
   - Verify pot awarding
5. Play 2nd hand, verify button rotation
6. Check no errors in logs
"""
```

### Manual Test Checklist:
- [ ] HU table created successfully
- [ ] 1 bot joins (not 5)
- [ ] Hand starts with 2 players
- [ ] Button posts SB, other posts BB
- [ ] Preflop action order correct (BTN first)
- [ ] Postflop action order correct (BB first)
- [ ] Pot awarded correctly
- [ ] Button rotates after hand
- [ ] Multiple hands work consecutively

---

## Appendix A: File Change Summary

| File | Changes |
|------|---------|
| `src/engine/config.py` | Add `min_players_to_start` field |
| `src/engine/table.py` | Use config in `can_start_hand()` |
| `src/manager/manager.py` | Add HU stake config |
| `src/manager/runner.py` | Fix `range(6)` loops |
| `src/server/handler.py` | Add `table_type` param, fix auto-start |
| `src/models/messages.py` | Add `max_players` to snapshot |
| `openbot/src/serving/openbot_client.py` | Add `--hu-policy` flag |
| `openbot/src/translation/translator.py` | Game type detection, policy routing |
| `openbot/src/translation/positions.py` | HU position mapping |
| `src/server/app.py` | Bot spawn logic for HU |

---

## Appendix B: Risks & Mitigations

| Risk | Mitigation |
|------|------------|
| PokerKit HU blind logic differs | Test extensively in Phase 3 before proceeding |
| Position mapping bugs | Unit test with known HU hands |
| Policy format mismatch | Verify info state format matches between training and serving |
| Breaking 6-max | Run full 6-max regression tests after each phase |

---

## Appendix C: Dependencies

```
Phase 1 ─┬─► Phase 2 ─► Phase 3 ─► Phase 4
         │                            │
         │                            ▼
         └──────────────────────► Phase 7 ─► Phase 8
                                      ▲
Phase 5 ─► Phase 6 ───────────────────┘
```

- Phases 1-4: Backend (can do independently)
- Phases 5-6: Bot serving (can do in parallel with backend)
- Phase 7: Requires both backend and serving ready
- Phase 8: Final integration

---

## Appendix D: Commands Reference

```bash
# Start local server
cd poker_backend
uvicorn src.server.app:app --host 0.0.0.0 --port 8000

# Run tests
pytest tests/ -v

# Check training status
ssh -i ~/Downloads/hutraining_key.pem azureuser@20.11.49.109 'tail -20 /tmp/train_gto.log'

# Export policy
python scripts/export_policy.py --checkpoint <path> --output hu_policy.db

# Sync to production
rsync -avz poker_backend/ de2425@162.222.177.28:~/poker_backend/
```
