#!/usr/bin/env python3
"""
Bot client for testing the WebSocket server.

Usage:
    python scripts/bot_client.py --players 3

Connects N players, joins same stake, plays random legal actions.
"""

import asyncio
import argparse
import json
import random
import sys
from typing import Optional

try:
    import websockets
except ImportError:
    print("Please install websockets: pip install websockets")
    sys.exit(1)


class BotClient:
    """Single bot player that connects to the server."""

    def __init__(self, user_id: str, server_url: str):
        self.user_id = user_id
        self.server_url = server_url
        self.ws = None
        self.table_id: Optional[str] = None
        self.seat: Optional[int] = None
        self.last_seq = -1
        self.received_messages: list = []
        self.is_my_turn = False
        self.allowed_actions: list = []
        self.hand_id: Optional[str] = None

    async def connect(self) -> bool:
        """Connect and authenticate."""
        self.ws = await websockets.connect(self.server_url)

        # Send AUTH
        await self.ws.send(json.dumps({
            "type": "AUTH",
            "token": self.user_id,
            "protocol_version": 1,
        }))

        response = json.loads(await self.ws.recv())
        if response.get("type") != "AUTH_OK":
            print(f"{self.user_id}: Auth failed: {response}")
            return False

        print(f"{self.user_id}: Authenticated")
        return True

    async def join_pool(self, stake_id: str = "nlh_1_2", buy_in: int = 20000) -> bool:
        """Join the pool and wait for table assignment."""
        await self.ws.send(json.dumps({
            "type": "JOIN_POOL",
            "stake_id": stake_id,
            "buy_in_cents": buy_in,
        }))

        response = json.loads(await self.ws.recv())
        if response.get("type") == "TABLE_SNAPSHOT":
            self.table_id = response.get("table_id")
            self.seat = response.get("your_seat")
            self.last_seq = response.get("seq", 0)
            print(f"{self.user_id}: Joined table {self.table_id} at seat {self.seat}")
            return True
        else:
            print(f"{self.user_id}: Join failed: {response}")
            return False

    async def listen_and_respond(self):
        """Listen for messages and respond to ACTION_REQUEST."""
        try:
            async for message in self.ws:
                data = json.loads(message)
                self.received_messages.append(data)
                msg_type = data.get("type")

                if msg_type == "STATE_DELTA":
                    seq = data.get("seq", 0)
                    if seq != self.last_seq + 1 and self.last_seq >= 0:
                        print(f"{self.user_id}: WARNING: seq gap {self.last_seq} -> {seq}")
                    self.last_seq = seq
                    hand_id = data.get("hand_id")
                    if hand_id:
                        self.hand_id = hand_id

                    events = data.get("events", [])
                    for event in events:
                        if event.get("event_type") == "hand_ended":
                            print(f"{self.user_id}: Hand ended!")

                elif msg_type == "ACTION_REQUEST":
                    print(f"{self.user_id}: ACTION_REQUEST - my turn!")
                    self.is_my_turn = True
                    self.allowed_actions = data.get("allowed_actions", [])
                    self.hand_id = data.get("hand_id")

                    # Respond with a random legal action
                    await self._play_random_action()

                elif msg_type == "TABLE_SNAPSHOT":
                    self.last_seq = data.get("seq", 0)
                    hand = data.get("hand")
                    if hand:
                        self.hand_id = hand.get("hand_id")
                    print(f"{self.user_id}: TABLE_SNAPSHOT seq={self.last_seq}")

                elif msg_type == "ERROR":
                    print(f"{self.user_id}: ERROR: {data.get('message')}")

        except websockets.exceptions.ConnectionClosed:
            print(f"{self.user_id}: Connection closed")

    async def _play_random_action(self):
        """Play a random legal action."""
        if not self.allowed_actions:
            return

        # Prefer fold for quick testing
        action = random.choice(self.allowed_actions)
        action_id = f"act_{random.randint(10000, 99999)}"

        msg = {
            "type": "ACTION",
            "hand_id": self.hand_id or "",
            "action_id": action_id,
            "action": action,
        }

        await self.ws.send(json.dumps(msg))
        print(f"{self.user_id}: Sent action: {action}")
        self.is_my_turn = False

    async def send_action(self, action: str, amount: Optional[int] = None):
        """Manually send an action."""
        action_id = f"act_{random.randint(10000, 99999)}"
        msg = {
            "type": "ACTION",
            "hand_id": self.hand_id or "",
            "action_id": action_id,
            "action": action,
        }
        if amount is not None:
            msg["amount_cents"] = amount

        await self.ws.send(json.dumps(msg))
        print(f"{self.user_id}: Sent {action}")

    async def close(self):
        """Close the connection."""
        if self.ws:
            await self.ws.close()


async def run_bots(num_players: int, server_url: str, auto_play: bool = True):
    """Run multiple bot clients."""
    print(f"Connecting {num_players} bots to {server_url}...")

    bots = [BotClient(f"user_{i}", server_url) for i in range(num_players)]

    # Connect all bots
    for bot in bots:
        if not await bot.connect():
            print("Failed to connect all bots")
            return

    # Join pool
    for bot in bots:
        if not await bot.join_pool():
            print("Failed to join all bots")
            return

    # All should be at same table
    table_ids = {bot.table_id for bot in bots}
    print(f"\nAll bots at table(s): {table_ids}")
    print(f"Seats: {[(bot.user_id, bot.seat) for bot in bots]}")

    if auto_play:
        # Start listeners - they will auto-respond to ACTION_REQUEST
        print("\nListening for game events (Ctrl+C to stop)...")
        listeners = [asyncio.create_task(bot.listen_and_respond()) for bot in bots]

        # Wait indefinitely (or until interrupted)
        try:
            await asyncio.gather(*listeners)
        except asyncio.CancelledError:
            pass
    else:
        # Just wait for manual interaction
        print("\nBots connected. Use debug endpoint to start hand.")
        print(f"  curl -X POST http://localhost:8000/debug/start_hand/{list(table_ids)[0]}")

        try:
            await asyncio.sleep(300)  # 5 minutes
        except asyncio.CancelledError:
            pass

    # Cleanup
    for bot in bots:
        await bot.close()

    print("\nAll bots disconnected")


def main():
    parser = argparse.ArgumentParser(description="Poker bot client for testing")
    parser.add_argument("--players", type=int, default=2, help="Number of bot players")
    parser.add_argument("--server", default="ws://localhost:8000/ws", help="Server URL")
    parser.add_argument("--no-auto-play", action="store_true", help="Don't auto-respond to ACTION_REQUEST")
    args = parser.parse_args()

    try:
        asyncio.run(run_bots(args.players, args.server, not args.no_auto_play))
    except KeyboardInterrupt:
        print("\nInterrupted")


if __name__ == "__main__":
    main()
