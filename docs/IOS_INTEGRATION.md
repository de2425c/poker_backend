# iOS Integration Guide - Poker Backend WebSocket API

## Table of Contents

1. [Overview](#1-overview)
2. [WebSocket Connection](#2-websocket-connection)
3. [Message Reference](#3-message-reference)
4. [Data Models](#4-data-models)
5. [Game Flow](#5-game-flow)
6. [Reconnection Handling](#6-reconnection-handling)
7. [Error Handling](#7-error-handling)
8. [iOS Implementation](#8-ios-implementation)
9. [Debug Endpoints](#9-debug-endpoints)
10. [Quick Reference](#10-quick-reference)

---

## 1. Overview

The poker backend uses a WebSocket-based protocol for real-time game communication.

**Key Principles:**
- Server-authoritative (server is source of truth)
- State snapshots + incremental deltas
- Idempotent action handling via `action_id`
- Server-managed action timeouts with auto-fold/auto-check

**Production URL:**
```
wss://poker-backend-sjwqhyh2ca-uc.a.run.app/ws
```

---

## 2. WebSocket Connection

### 2.1 Connection Flow

```
[Client] → WebSocket connect to /ws
         ↓
[Client] → AUTH message with token
         ↓
[Server] → AUTH_OK (includes current_table_id if reconnecting)
         ↓
[Server] → TABLE_SNAPSHOT (if reconnecting to active table)
         ↓
[Ready for] → JOIN_POOL / ACTION / LEAVE_TABLE / PING
```

### 2.2 Authentication

First message after connecting MUST be AUTH:

```json
{
  "type": "AUTH",
  "token": "user_alice",
  "protocol_version": 1
}
```

**Token Format:**
- Development: Any string starting with `user_` (e.g., `user_alice`, `user_1`)
- Production: Firebase ID token from your iOS app

**Response:**
```json
{
  "type": "AUTH_OK",
  "user_id": "user_alice",
  "current_table_id": null,
  "server_time": "2024-01-10T15:30:00.000Z"
}
```

If `current_table_id` is not null, user was previously seated - expect TABLE_SNAPSHOT next.

---

## 3. Message Reference

### 3.1 Client → Server Messages

#### AUTH
Authenticate the WebSocket connection.

```json
{
  "type": "AUTH",
  "token": "string",
  "protocol_version": 1
}
```

#### JOIN_POOL
Join a table at a specific stake level.

```json
{
  "type": "JOIN_POOL",
  "stake_id": "nlh_1_2",
  "buy_in_cents": 20000
}
```

| Field | Type | Description |
|-------|------|-------------|
| stake_id | string | Stake level identifier (currently only `nlh_1_2`) |
| buy_in_cents | int | Buy-in amount in cents (4000-40000 for nlh_1_2) |

**Response:** TABLE_SNAPSHOT or ERROR

#### ACTION
Submit a game action during your turn.

```json
{
  "type": "ACTION",
  "hand_id": "hand_abc123",
  "action_id": "act_xyz789",
  "action": "raise_to",
  "amount_cents": 500
}
```

| Field | Type | Description |
|-------|------|-------------|
| hand_id | string | Current hand identifier (from ACTION_REQUEST) |
| action_id | string | Unique client-generated ID for idempotency |
| action | string | One of: `fold`, `check`, `call`, `bet`, `raise_to` |
| amount_cents | int? | Required for `bet` and `raise_to` |

**Action Semantics:**

| Action | When Valid | Amount |
|--------|------------|--------|
| `fold` | Always (your turn) | None |
| `check` | No bet to call (call_amount = 0) | None |
| `call` | Facing a bet (call_amount > 0) | None (auto) |
| `bet` | First bet of street | Required |
| `raise_to` | Facing a bet, can raise | Required (TOTAL amount, not raise BY) |

**All-in:** Send bet/raise_to with amount = your stack. Server sets `is_all_in: true`.

#### LEAVE_TABLE
Leave the current table.

```json
{
  "type": "LEAVE_TABLE"
}
```

**Response:** TABLE_LEFT with final chip count

#### PING
Heartbeat / latency check.

```json
{
  "type": "PING",
  "client_ts": 1704900000000
}
```

**Response:** PONG with server timestamp

---

### 3.2 Server → Client Messages

#### AUTH_OK
Authentication successful.

```json
{
  "type": "AUTH_OK",
  "user_id": "user_alice",
  "current_table_id": "tbl_abc123" | null,
  "server_time": "2024-01-10T15:30:00.000Z"
}
```

#### TABLE_SNAPSHOT
Complete table state. Sent after JOIN_POOL or reconnection.

```json
{
  "type": "TABLE_SNAPSHOT",
  "table_id": "tbl_abc123",
  "status": "running",
  "stake_id": "nlh_1_2",
  "small_blind": {"amount": 100},
  "big_blind": {"amount": 200},
  "seats": [
    {
      "seat_index": 0,
      "status": "active",
      "player": {
        "user_id": "user_alice",
        "display_name": "PlayerAlice",
        "avatar_url": null
      },
      "chips": {"amount": 19800},
      "bet": {"amount": 200},
      "is_button": true,
      "is_connected": true
    },
    {
      "seat_index": 1,
      "status": "active",
      "player": {
        "user_id": "user_bob",
        "display_name": "PlayerBob",
        "avatar_url": null
      },
      "chips": {"amount": 19900},
      "bet": {"amount": 100},
      "is_button": false,
      "is_connected": true
    }
  ],
  "hand": {
    "hand_id": "hand_abc123",
    "street": "preflop",
    "board": [],
    "pots": [{"amount": {"amount": 300}, "eligible_seats": [0, 1]}],
    "current_bet": {"amount": 200},
    "actor_seat": 0
  },
  "your_seat": 0,
  "your_hole_cards": [
    {"rank": "A", "suit": "h"},
    {"rank": "K", "suit": "d"}
  ],
  "seq": 1
}
```

**Key Fields:**

| Field | Description |
|-------|-------------|
| status | `waiting`, `running`, `between_hands`, `paused`, `closed` |
| seats | Array of 6 seat objects (some may be empty) |
| hand | Current hand state (null if between hands) |
| your_seat | Your seat index (0-5) |
| your_hole_cards | Your private cards (only you see these) |
| seq | Sequence number for delta ordering |

**Seat Status Values:**

| Status | Description |
|--------|-------------|
| `empty` | No player |
| `reserved` | Brief hold during matchmaking |
| `seated` | Present but sitting out |
| `active` | In current hand |
| `all_in` | All chips committed |
| `folded` | Folded this hand |

#### ACTION_REQUEST
Sent ONLY to the player whose turn it is.

```json
{
  "type": "ACTION_REQUEST",
  "hand_id": "hand_abc123",
  "request_id": "req_001",
  "seat": 0,
  "allowed_actions": ["fold", "call", "raise_to"],
  "call_amount": {"amount": 100},
  "min_raise": {"amount": 400},
  "max_raise": {"amount": 19800},
  "expires_at_ms": 1704900030000
}
```

| Field | Description |
|-------|-------------|
| allowed_actions | Valid actions for this situation |
| call_amount | Amount to call (null if can check) |
| min_raise | Minimum raise TO amount |
| max_raise | Maximum raise TO amount (your stack) |
| expires_at_ms | Unix timestamp (ms) when auto-action triggers |

**Button Logic:**

| Situation | allowed_actions | call_amount | min_raise |
|-----------|-----------------|-------------|-----------|
| Can check | [fold, check] or [fold, check, raise_to] | null or 0 | if can raise |
| Facing bet | [fold, call] or [fold, call, raise_to] | > 0 | if can raise |
| All-in for less | [fold, call] | > 0 | null |

#### STATE_DELTA
Incremental state update. Broadcast to all players at table.

```json
{
  "type": "STATE_DELTA",
  "table_id": "tbl_abc123",
  "hand_id": "hand_abc123",
  "seq": 2,
  "events": [
    {
      "event_type": "action",
      "seat": 0,
      "action": "raise_to",
      "amount": {"amount": 500},
      "is_all_in": false
    }
  ]
}
```

**Event Types:**

**ActionEvent:**
```json
{
  "event_type": "action",
  "seat": 0,
  "action": "fold|check|call|bet|raise_to|post_blind|post_ante",
  "amount": {"amount": 500} | null,
  "is_all_in": false
}
```

**StreetDealtEvent:**
```json
{
  "event_type": "street_dealt",
  "street": "flop",
  "cards": [
    {"rank": "A", "suit": "h"},
    {"rank": "K", "suit": "s"},
    {"rank": "Q", "suit": "d"}
  ]
}
```

**HandStartedEvent:**
```json
{
  "event_type": "hand_started",
  "hand_id": "hand_abc123",
  "button_seat": 0
}
```

**HandEndedEvent:**
```json
{
  "event_type": "hand_ended",
  "hand_id": "hand_abc123",
  "winners": [
    {
      "seat": 0,
      "amount": {"amount": 1000},
      "hand_description": "Two Pair, Aces and Kings",
      "shown_cards": [
        {"rank": "A", "suit": "h"},
        {"rank": "K", "suit": "d"}
      ]
    }
  ]
}
```

#### TABLE_LEFT
Confirms player has left the table.

```json
{
  "type": "TABLE_LEFT",
  "final_chips": {"amount": 22500}
}
```

#### PONG
Response to PING.

```json
{
  "type": "PONG",
  "client_ts": 1704900000000,
  "server_ts": 1704900000050
}
```

#### ERROR
Structured error response.

```json
{
  "type": "ERROR",
  "code": "not_your_turn",
  "message": "It's not seat 1's turn",
  "ref_msg_id": "act_xyz789",
  "details": null
}
```

---

## 4. Data Models

### 4.1 Core Types

```swift
struct Chips: Codable {
    let amount: Int  // In cents
}

struct Card: Codable {
    let rank: String  // "2"-"9", "T", "J", "Q", "K", "A"
    let suit: String  // "s", "h", "d", "c"
}

struct PlayerIdentity: Codable {
    let user_id: String
    let display_name: String
    let avatar_url: String?
}
```

### 4.2 Enums

```swift
enum ClientAction: String, Codable {
    case fold
    case check
    case call
    case bet
    case raise_to
}

enum Street: String, Codable {
    case preflop
    case flop
    case turn
    case river
    case showdown
}

enum TableStatus: String, Codable {
    case waiting
    case running
    case between_hands
    case paused
    case closed
}

enum SeatStatus: String, Codable {
    case empty
    case reserved
    case seated
    case active
    case all_in
    case folded
}
```

### 4.3 Game State

```swift
struct Seat: Codable {
    let seat_index: Int
    let status: SeatStatus
    let player: PlayerIdentity?
    let chips: Chips
    let bet: Chips
    let is_button: Bool
    let is_connected: Bool
}

struct Pot: Codable {
    let amount: Chips
    let eligible_seats: [Int]
}

struct HandState: Codable {
    let hand_id: String
    let street: Street
    let board: [Card]
    let pots: [Pot]
    let current_bet: Chips
    let actor_seat: Int?
}
```

---

## 5. Game Flow

### 5.1 Connection and Joining

```
1. Connect WebSocket to wss://poker-backend-xxx.run.app/ws
2. Send AUTH with token
3. Receive AUTH_OK
4. Send JOIN_POOL with stake_id and buy_in
5. Receive TABLE_SNAPSHOT
6. Render table UI
```

### 5.2 Hand Lifecycle

```
[Server] STATE_DELTA: hand_started event
         → Receive button position, start rendering hand

[Server] STATE_DELTA: post_blind events
         → Show blinds posted

[Server] ACTION_REQUEST (if your turn)
         → Show action buttons with countdown

[Client] ACTION: submit your action
         → Hide buttons, show waiting

[Server] STATE_DELTA: action event
         → Update UI with action taken

... repeat for each player ...

[Server] STATE_DELTA: street_dealt event (flop/turn/river)
         → Animate new cards on board

... repeat betting rounds ...

[Server] STATE_DELTA: hand_ended event
         → Show winner(s), animate chips
         → Update stack sizes
```

### 5.3 Action Timeout

```
[Server] ACTION_REQUEST with expires_at_ms
         ↓
[Client] Display countdown: max(0, expires_at_ms - now())
         ↓
If no action by deadline:
         ↓
[Server] Auto-fold (if facing bet) or auto-check
         ↓
[Server] STATE_DELTA with action event
```

### 5.4 Complete Turn Example

```
Scenario: Heads-up, you're facing a $2 raise to $5

[Server sends ACTION_REQUEST]
{
  "type": "ACTION_REQUEST",
  "hand_id": "hand_001",
  "seat": 0,
  "allowed_actions": ["fold", "call", "raise_to"],
  "call_amount": {"amount": 300},      // $3 to call
  "min_raise": {"amount": 800},        // Min raise to $8
  "max_raise": {"amount": 19500},      // Your stack
  "expires_at_ms": 1704900030000
}

[Client shows buttons]
  [Fold]  [Call $3]  [Raise to $8-$195]

[User taps Raise, enters $15]

[Client sends ACTION]
{
  "type": "ACTION",
  "hand_id": "hand_001",
  "action_id": "act_user0_001",
  "action": "raise_to",
  "amount_cents": 1500
}

[Server broadcasts STATE_DELTA to all]
{
  "type": "STATE_DELTA",
  "events": [
    {
      "event_type": "action",
      "seat": 0,
      "action": "raise_to",
      "amount": {"amount": 1500},
      "is_all_in": false
    }
  ]
}
```

---

## 6. Reconnection Handling

### 6.1 Reconnection Flow

```
[Network drops / App backgrounded]
         ↓
[Reconnect WebSocket]
         ↓
[Send AUTH with same token]
         ↓
[Receive AUTH_OK]
  → If current_table_id is set, you were seated
         ↓
[Receive TABLE_SNAPSHOT]
  → Full current state recovered
         ↓
[Receive ACTION_REQUEST]
  → If it's your turn, new deadline issued
```

### 6.2 State Recovery

The server maintains:
- User → Table mapping (survives reconnection)
- Table state (all seats, hand state)
- Your hole cards (private)

On reconnection, you receive everything needed to resume:
- Current hand state
- Your cards
- Pot sizes
- Whose turn it is

### 6.3 Handling Reconnection in iOS

```swift
class PokerConnectionManager {
    private var webSocket: URLSessionWebSocketTask?
    private var token: String?
    private var currentTableId: String?

    func reconnect() async throws {
        // Close old connection if exists
        webSocket?.cancel(with: .goingAway, reason: nil)

        // Create new connection
        let url = URL(string: "wss://poker-backend-xxx.run.app/ws")!
        webSocket = URLSession.shared.webSocketTask(with: url)
        webSocket?.resume()

        // Authenticate (server recognizes user)
        try await send(AuthMessage(token: token!))

        // Wait for AUTH_OK
        let authOk = try await receive()

        if let tableId = authOk.current_table_id {
            // We were at a table - wait for snapshot
            currentTableId = tableId
            let snapshot = try await receive()
            updateUI(with: snapshot)
        }
    }
}
```

---

## 7. Error Handling

### 7.1 Error Codes

| Code | Category | Description |
|------|----------|-------------|
| `unauthorized` | Auth | Invalid token |
| `bad_request` | Protocol | Invalid message format |
| `unknown_message_type` | Protocol | Unknown type field |
| `not_your_turn` | Game | Wrong player's turn |
| `invalid_action` | Game | Action not legal |
| `invalid_amount` | Game | Outside min/max raise |
| `action_timeout` | Game | Deadline expired |
| `table_not_found` | Table | Table doesn't exist |
| `table_full` | Table | All seats occupied |
| `not_at_table` | Table | User not seated |
| `already_at_table` | Table | User already seated |
| `internal_error` | Server | Unexpected error |

### 7.2 Handling Errors

```swift
func handleError(_ error: ErrorMessage) {
    switch error.code {
    case "not_your_turn":
        // Wait for ACTION_REQUEST before sending
        break

    case "invalid_action":
        // Check allowed_actions from ACTION_REQUEST
        showAlert("Invalid action")

    case "invalid_amount":
        // Use min_raise/max_raise from ACTION_REQUEST
        if let details = error.details {
            showAlert("Amount must be \(details.min_raise)-\(details.max_raise)")
        }

    case "action_timeout":
        // Server auto-acted, wait for STATE_DELTA
        break

    case "unauthorized":
        // Refresh token and reconnect
        refreshTokenAndReconnect()

    default:
        showAlert(error.message)
    }
}
```

### 7.3 Retry Strategy

```swift
func sendWithRetry<T: Encodable>(_ message: T) async throws {
    var attempts = 0
    let maxAttempts = 3

    while attempts < maxAttempts {
        do {
            try await send(message)
            return
        } catch {
            attempts += 1
            if attempts < maxAttempts {
                // Exponential backoff
                try await Task.sleep(nanoseconds: UInt64(pow(2.0, Double(attempts))) * 500_000_000)
            }
        }
    }

    // All retries failed - reconnect
    try await reconnect()
}
```

---

## 8. iOS Implementation

### 8.1 WebSocket Manager

```swift
import Foundation

class PokerWebSocket: NSObject, URLSessionWebSocketDelegate {
    private var webSocket: URLSessionWebSocketTask?
    private var session: URLSession!

    weak var delegate: PokerWebSocketDelegate?

    override init() {
        super.init()
        session = URLSession(configuration: .default, delegate: self, delegateQueue: .main)
    }

    func connect(to url: URL) {
        webSocket = session.webSocketTask(with: url)
        webSocket?.resume()
        receiveMessage()
    }

    func send<T: Encodable>(_ message: T) async throws {
        let data = try JSONEncoder().encode(message)
        try await webSocket?.send(.data(data))
    }

    private func receiveMessage() {
        webSocket?.receive { [weak self] result in
            switch result {
            case .success(let message):
                switch message {
                case .data(let data):
                    self?.handleMessage(data)
                case .string(let text):
                    if let data = text.data(using: .utf8) {
                        self?.handleMessage(data)
                    }
                @unknown default:
                    break
                }
                // Continue receiving
                self?.receiveMessage()

            case .failure(let error):
                self?.delegate?.didDisconnect(error: error)
            }
        }
    }

    private func handleMessage(_ data: Data) {
        // Decode base message to get type
        guard let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let type = json["type"] as? String else {
            return
        }

        let decoder = JSONDecoder()

        switch type {
        case "AUTH_OK":
            if let msg = try? decoder.decode(AuthOKMessage.self, from: data) {
                delegate?.didReceiveAuthOK(msg)
            }
        case "TABLE_SNAPSHOT":
            if let msg = try? decoder.decode(TableSnapshot.self, from: data) {
                delegate?.didReceiveSnapshot(msg)
            }
        case "ACTION_REQUEST":
            if let msg = try? decoder.decode(ActionRequest.self, from: data) {
                delegate?.didReceiveActionRequest(msg)
            }
        case "STATE_DELTA":
            if let msg = try? decoder.decode(StateDelta.self, from: data) {
                delegate?.didReceiveStateDelta(msg)
            }
        case "ERROR":
            if let msg = try? decoder.decode(ErrorMessage.self, from: data) {
                delegate?.didReceiveError(msg)
            }
        default:
            break
        }
    }

    // URLSessionWebSocketDelegate
    func urlSession(_ session: URLSession, webSocketTask: URLSessionWebSocketTask,
                    didOpenWithProtocol protocol: String?) {
        delegate?.didConnect()
    }

    func urlSession(_ session: URLSession, webSocketTask: URLSessionWebSocketTask,
                    didCloseWith closeCode: URLSessionWebSocketTask.CloseCode, reason: Data?) {
        delegate?.didDisconnect(error: nil)
    }
}

protocol PokerWebSocketDelegate: AnyObject {
    func didConnect()
    func didDisconnect(error: Error?)
    func didReceiveAuthOK(_ message: AuthOKMessage)
    func didReceiveSnapshot(_ snapshot: TableSnapshot)
    func didReceiveActionRequest(_ request: ActionRequest)
    func didReceiveStateDelta(_ delta: StateDelta)
    func didReceiveError(_ error: ErrorMessage)
}
```

### 8.2 Message Models

```swift
// Outgoing Messages
struct AuthMessage: Encodable {
    let type = "AUTH"
    let token: String
    let protocol_version: Int = 1
}

struct JoinPoolMessage: Encodable {
    let type = "JOIN_POOL"
    let stake_id: String
    let buy_in_cents: Int
}

struct ActionMessage: Encodable {
    let type = "ACTION"
    let hand_id: String
    let action_id: String
    let action: String
    let amount_cents: Int?
}

struct LeaveTableMessage: Encodable {
    let type = "LEAVE_TABLE"
}

struct PingMessage: Encodable {
    let type = "PING"
    let client_ts: Int64
}

// Incoming Messages
struct AuthOKMessage: Decodable {
    let type: String
    let user_id: String
    let current_table_id: String?
    let server_time: String
}

struct TableSnapshot: Decodable {
    let type: String
    let table_id: String
    let status: String
    let stake_id: String
    let small_blind: Chips
    let big_blind: Chips
    let seats: [Seat]
    let hand: HandState?
    let your_seat: Int
    let your_hole_cards: [Card]?
    let seq: Int
}

struct ActionRequest: Decodable {
    let type: String
    let hand_id: String
    let request_id: String
    let seat: Int
    let allowed_actions: [String]
    let call_amount: Chips?
    let min_raise: Chips?
    let max_raise: Chips?
    let expires_at_ms: Int64
}

struct StateDelta: Decodable {
    let type: String
    let table_id: String
    let hand_id: String?
    let seq: Int
    let events: [GameEvent]
}

struct ErrorMessage: Decodable {
    let type: String
    let code: String
    let message: String
    let ref_msg_id: String?
    let details: [String: Int]?
}
```

### 8.3 Action ID Generation

```swift
extension String {
    static func generateActionId() -> String {
        return "act_\(UUID().uuidString.prefix(8).lowercased())"
    }
}

// Usage
let actionId = String.generateActionId()  // "act_a1b2c3d4"
```

### 8.4 Countdown Timer

```swift
class ActionTimer {
    private var timer: Timer?
    private var expiresAt: Date?

    var onTick: ((Int) -> Void)?
    var onExpired: (() -> Void)?

    func start(expiresAtMs: Int64) {
        expiresAt = Date(timeIntervalSince1970: Double(expiresAtMs) / 1000.0)

        timer = Timer.scheduledTimer(withTimeInterval: 0.1, repeats: true) { [weak self] _ in
            self?.tick()
        }
    }

    func stop() {
        timer?.invalidate()
        timer = nil
    }

    private func tick() {
        guard let expiresAt = expiresAt else { return }

        let remaining = max(0, expiresAt.timeIntervalSinceNow)

        if remaining <= 0 {
            onExpired?()
            stop()
        } else {
            onTick?(Int(remaining))
        }
    }
}
```

### 8.5 Background Handling

```swift
class PokerConnectionManager {
    private var backgroundTask: UIBackgroundTaskIdentifier = .invalid

    func applicationDidEnterBackground() {
        // Start background task to keep connection briefly
        backgroundTask = UIApplication.shared.beginBackgroundTask { [weak self] in
            self?.endBackgroundTask()
        }

        // Disconnect after brief period to save battery
        DispatchQueue.main.asyncAfter(deadline: .now() + 10) { [weak self] in
            self?.disconnect()
            self?.endBackgroundTask()
        }
    }

    func applicationWillEnterForeground() {
        endBackgroundTask()
        reconnect()
    }

    private func endBackgroundTask() {
        if backgroundTask != .invalid {
            UIApplication.shared.endBackgroundTask(backgroundTask)
            backgroundTask = .invalid
        }
    }
}
```

---

## 9. Debug Endpoints

### Health Checks

```bash
# Liveness probe
GET /health
→ {"status": "ok"}

# Readiness probe (detailed)
GET /ready
→ {
    "status": "ready",
    "checks": {"manager": true, "connections": true, "timer": true},
    "active_tables": 2,
    "active_connections": 5
  }
```

### Debug Endpoints (Development)

```bash
# List all tables
GET /debug/tables
→ {"tables": [{"table_id": "tbl_abc", "player_count": 2, "has_open_seats": true}]}

# Start a hand manually
POST /debug/start_hand/{table_id}
→ {"status": "hand_started"}

# Force timeout (for testing)
POST /debug/force_timeout/{user_id}
→ {"status": "timeout_forced", "user_id": "user_alice"}

# List hand logs
GET /debug/hand_logs
→ {"hand_logs": [...]}

# Get specific hand
GET /debug/hand_logs/{hand_id}
→ {hand details}

# List all ledger entries
GET /debug/ledger
→ {"ledger_entries": [...]}

# Get user's ledger
GET /debug/ledger/{user_id}
→ {"ledger_entries": [...]}
```

---

## 10. Quick Reference

### Message Flow Cheat Sheet

```
Connect:    [C] → AUTH           → [S] AUTH_OK
Join:       [C] → JOIN_POOL      → [S] TABLE_SNAPSHOT
Play:       [S] → STATE_DELTA (hand_started + blinds)
            [S] → ACTION_REQUEST (to actor)
            [C] → ACTION
            [S] → STATE_DELTA (action event)
            ... repeat ...
            [S] → STATE_DELTA (hand_ended)
Leave:      [C] → LEAVE_TABLE    → [S] TABLE_LEFT
Ping:       [C] → PING           → [S] PONG
Error:      [S] → ERROR
```

### Stake Configuration (nlh_1_2)

| Setting | Value |
|---------|-------|
| Small Blind | 100 cents ($1) |
| Big Blind | 200 cents ($2) |
| Min Buy-in | 4000 cents ($40, 20bb) |
| Max Buy-in | 40000 cents ($400, 200bb) |
| Action Timeout | 30 seconds |
| Max Players | 6 |

### Card Notation

| Ranks | Suits |
|-------|-------|
| 2-9, T, J, Q, K, A | s (spades), h (hearts), d (diamonds), c (clubs) |

Example: `{"rank": "A", "suit": "h"}` = Ace of Hearts

### Production URL

```
wss://poker-backend-sjwqhyh2ca-uc.a.run.app/ws
```
