# Stack Poker Protocol Specification

## Overview

This document defines the WebSocket protocol for the Stack Poker real-time game server. The server is **authoritative**—clients request actions, but the server validates and executes all game logic using PokerKit as the single source of truth.

## Design Principles

1. **Server Authority**: Clients never apply game rules locally. They send action requests; the server validates and broadcasts results.

2. **Deterministic Replay**: Every hand can be replayed from its event log. RNG seeds or dealt cards are recorded.

3. **Idempotent Actions**: Every action has a unique `action_id`. Replaying the same action returns the same result (no double-betting on network retry).

4. **Ordered Updates**: State deltas have monotonic `seq` numbers per table. Clients can detect missed messages and request snapshots.

5. **Minimal Wire Format**: Only send what's needed. Full state on join/reconnect, deltas during play.

---

## Base Types

### ActionType

Player actions map 1:1 to PokerKit's action system:

| Action | Description | Has Amount? |
|--------|-------------|-------------|
| `fold` | Surrender hand | No |
| `check` | Pass (no bet to call) | No |
| `call` | Match current bet | No (implied) |
| `bet` | Open betting | Yes (required) |
| `raise` | Increase current bet | Yes (required) |
| `all_in` | Commit remaining stack | No (implied) |
| `post_blind` | Forced blind (server-initiated) | Yes |
| `post_ante` | Forced ante (server-initiated) | Yes |

**Design note**: `all_in` is separate from `bet`/`raise` because:
- Clearer intent in logs
- Simplifies client UI (show distinct button)
- PokerKit tracks all-in state separately

### Street

Betting rounds: `preflop` → `flop` → `turn` → `river` → `showdown`

Used for:
- Hand history segmentation
- Progressive board reveal in UI
- Per-street analytics

### TableStatus

```
WAITING ──(2+ players)──► RUNNING ──(hand ends)──► BETWEEN_HANDS
    ▲                         │                         │
    │                         ▼                         │
    └──────(1 player)─────────┴────(new hand starts)────┘
```

- `PAUSED`: Admin intervention (break time, dispute)
- `CLOSED`: Table shutting down, no new hands

### SeatStatus

Lifecycle within a hand:

```
EMPTY → RESERVED → SEATED → ACTIVE → (FOLDED | ALL_IN) → SEATED
```

- `RESERVED`: Brief hold during join (prevents race conditions)
- `SEATED`: At table but not in current hand (sitting out or joined mid-hand)
- `ACTIVE`: In hand, can act
- `ALL_IN`: In hand, committed all chips
- `FOLDED`: Out of current hand

### ErrorCode

Structured codes allow clients to handle errors programmatically:

| Category | Codes | Client Response |
|----------|-------|-----------------|
| Auth (1xx) | `unauthorized`, `session_expired` | Re-authenticate |
| Protocol (2xx) | `bad_request`, `invalid_message` | Fix client bug |
| Game Logic (3xx) | `not_your_turn`, `invalid_action` | Show user feedback |
| Table (4xx) | `table_full`, `not_at_table` | UI state sync |
| Server (5xx) | `internal_error` | Retry with backoff |

---

## Primitive Models

### Card

```json
{"rank": "A", "suit": "h"}
```

**Why not just `"Ah"`?**
- Type safety: can't pass `"XX"`
- Easier manipulation in logic
- Matches PokerKit internal format
- Validation at parse time

Ranks: `2-9`, `T`, `J`, `Q`, `K`, `A`
Suits: `s`(pades), `h`(earts), `d`(iamonds), `c`(lubs)

### Chips

```json
{"amount": 10000}
```

All amounts in **cents** (smallest unit). A $100 buy-in = `{"amount": 10000}`.

**Why a model instead of int?**
- Prevents unit confusion (cents vs dollars vs BB)
- Validation: always non-negative
- Future: multi-currency games

### PlayerIdentity

Minimal info for wire protocol:

```json
{
  "user_id": "usr_abc123",
  "display_name": "Phil",
  "avatar_url": "https://..."
}
```

Deliberately separate from your User model—only expose what the table needs.

---

## ID Conventions

All IDs are prefixed for easy log grepping:

| Entity | Format | Example |
|--------|--------|---------|
| Table | `tbl_{hex10}` | `tbl_a1b2c3d4e5` |
| Hand | `hand_{hex12}` | `hand_a1b2c3d4e5f6` |
| Action | `act_{hex8}` | `act_a1b2c3d4` |

---

## Next: Message Types

See individual sections:
- [Client Messages](./client_messages.md) - AUTH, JOIN_POOL, ACTION, etc.
- [Server Messages](./server_messages.md) - TABLE_SNAPSHOT, STATE_DELTA, etc.
