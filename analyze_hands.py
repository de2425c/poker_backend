#!/usr/bin/env python3
"""
Analyze hands collection from Firebase.
"""

import os
import json
from datetime import datetime
from collections import defaultdict
import statistics

# Set credentials path
os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = "/Users/davideyal/Projects/stack_poker/backend/stack-24dea-firebase-adminsdk-fbsvc-928dfa73a0.json"

import firebase_admin
from firebase_admin import credentials, firestore

# Initialize Firebase
if not firebase_admin._apps:
    cred = credentials.Certificate(os.environ["GOOGLE_APPLICATION_CREDENTIALS"])
    firebase_admin.initialize_app(cred)

db = firestore.client()

def fetch_all_hands():
    """Fetch all documents from hands collection."""
    hands_ref = db.collection("hands")
    docs = hands_ref.stream()

    hands = []
    for doc in docs:
        data = doc.to_dict()
        data['_id'] = doc.id
        hands.append(data)

    return hands

def get_user_from_seat(hand, seat_index):
    """Get user_id and display_name for a seat."""
    for seat in hand.get('seats', []):
        if seat.get('seat_index') == seat_index:
            return seat.get('user_id'), seat.get('display_name')
    return None, None

def is_bot(user_id):
    """Check if user_id is a bot."""
    if not user_id:
        return True
    return user_id.startswith('user_bot_')

def analyze_hands(hands):
    """Run analytics on hands data."""
    if not hands:
        print("No hands to analyze!")
        return

    print("\n" + "="*60)
    print("HANDS COLLECTION ANALYTICS")
    print("="*60)
    print(f"\nTotal hands: {len(hands)}")

    # Build user stats from seats and stack_deltas
    user_stats = defaultdict(lambda: {
        'hands': 0,
        'pnl_total': 0,
        'pnl_list': [],
        'display_name': None,
        'is_bot': False,
        'wins': 0,
        'losses': 0,
        'breakeven': 0,
        'tables': set(),
        'stakes': defaultdict(int)
    })

    # Stakes analysis
    stakes_count = defaultdict(int)
    stakes_hands = defaultdict(list)

    # Time tracking
    all_dates = []

    # Actions analysis
    action_counts = defaultdict(int)
    actions_per_hand = []

    for hand in hands:
        # Stakes
        sb = hand.get('small_blind', 0)
        bb = hand.get('big_blind', 0)
        stake_str = f"${sb/100:.0f}/${bb/100:.0f}" if sb >= 100 else f"${sb}/{bb}"
        stakes_count[stake_str] += 1
        stakes_hands[stake_str].append(hand)

        # Time
        started = hand.get('started_at')
        if started:
            if isinstance(started, str):
                try:
                    dt = datetime.fromisoformat(started.replace('Z', '+00:00'))
                    all_dates.append(dt)
                except:
                    pass

        # Actions
        actions = hand.get('actions', [])
        actions_per_hand.append(len(actions))
        for action in actions:
            action_counts[action.get('action', 'unknown')] += 1

        # User stats from stack_deltas
        stack_deltas = hand.get('stack_deltas', {})
        table_id = hand.get('table_id', 'unknown')

        for seat_str, delta in stack_deltas.items():
            seat_idx = int(seat_str)
            user_id, display_name = get_user_from_seat(hand, seat_idx)

            if user_id:
                stats = user_stats[user_id]
                stats['hands'] += 1
                stats['pnl_total'] += delta
                stats['pnl_list'].append(delta)
                stats['display_name'] = display_name
                stats['is_bot'] = is_bot(user_id)
                stats['tables'].add(table_id)
                stats['stakes'][stake_str] += 1

                if delta > 0:
                    stats['wins'] += 1
                elif delta < 0:
                    stats['losses'] += 1
                else:
                    stats['breakeven'] += 1

    # Separate human and bot users
    human_users = {k: v for k, v in user_stats.items() if not v['is_bot']}
    bot_users = {k: v for k, v in user_stats.items() if v['is_bot']}

    print(f"\n{'='*60}")
    print("USER BREAKDOWN")
    print(f"{'='*60}")
    print(f"Total unique users: {len(user_stats)}")
    print(f"  Human players: {len(human_users)}")
    print(f"  Bots: {len(bot_users)}")

    # Human player detailed stats
    print(f"\n{'='*60}")
    print("HUMAN PLAYER STATS")
    print(f"{'='*60}")

    for user_id, stats in sorted(human_users.items(), key=lambda x: -x[1]['hands']):
        print(f"\n  {stats['display_name'] or user_id[:20]}")
        print(f"    User ID: {user_id}")
        print(f"    Total Hands: {stats['hands']}")
        print(f"    Total PnL: {stats['pnl_total']/100:.2f} (in dollars)")
        print(f"    Wins: {stats['wins']}, Losses: {stats['losses']}, Breakeven: {stats['breakeven']}")

        if stats['hands'] > 0:
            win_rate = stats['wins'] / stats['hands'] * 100
            print(f"    Win Rate: {win_rate:.1f}%")
            avg_pnl = stats['pnl_total'] / stats['hands']
            print(f"    Avg PnL/Hand: {avg_pnl/100:.2f}")

        if stats['pnl_list']:
            pnls = [p/100 for p in stats['pnl_list']]  # Convert to dollars
            print(f"    Median PnL: {statistics.median(pnls):.2f}")
            print(f"    Max Win: {max(pnls):.2f}")
            print(f"    Max Loss: {min(pnls):.2f}")
            if len(pnls) > 1:
                print(f"    Std Dev: {statistics.stdev(pnls):.2f}")

        print(f"    Unique Tables: {len(stats['tables'])}")
        print(f"    Stakes played: {dict(stats['stakes'])}")

    # Bot aggregate stats
    print(f"\n{'='*60}")
    print("BOT AGGREGATE STATS")
    print(f"{'='*60}")

    total_bot_hands = sum(s['hands'] for s in bot_users.values())
    total_bot_pnl = sum(s['pnl_total'] for s in bot_users.values())
    total_bot_wins = sum(s['wins'] for s in bot_users.values())
    total_bot_losses = sum(s['losses'] for s in bot_users.values())

    print(f"  Total bot hands: {total_bot_hands}")
    print(f"  Total bot PnL: {total_bot_pnl/100:.2f}")
    if total_bot_hands > 0:
        print(f"  Bot win rate: {total_bot_wins/total_bot_hands*100:.1f}%")
        print(f"  Bot avg PnL/hand: {total_bot_pnl/total_bot_hands/100:.2f}")

    # Stakes analysis
    print(f"\n{'='*60}")
    print("STAKES ANALYSIS")
    print(f"{'='*60}")

    for stake, count in sorted(stakes_count.items(), key=lambda x: -x[1]):
        print(f"  {stake}: {count} hands")

    # Actions analysis
    print(f"\n{'='*60}")
    print("ACTIONS ANALYSIS")
    print(f"{'='*60}")

    total_actions = sum(action_counts.values())
    print(f"Total actions: {total_actions}")
    print(f"Avg actions/hand: {total_actions/len(hands):.1f}")
    print(f"Median actions/hand: {statistics.median(actions_per_hand):.0f}")

    print("\nAction breakdown:")
    for action, count in sorted(action_counts.items(), key=lambda x: -x[1]):
        pct = count / total_actions * 100
        print(f"  {action}: {count} ({pct:.1f}%)")

    # Time analysis
    print(f"\n{'='*60}")
    print("TIME ANALYSIS")
    print(f"{'='*60}")

    if all_dates:
        all_dates.sort()
        print(f"  First hand: {all_dates[0]}")
        print(f"  Last hand: {all_dates[-1]}")
        duration = all_dates[-1] - all_dates[0]
        print(f"  Duration: {duration}")

        # Hands per day
        hands_per_day = defaultdict(int)
        for d in all_dates:
            hands_per_day[d.strftime('%Y-%m-%d')] += 1

        print(f"\n  Hands per day:")
        for day, count in sorted(hands_per_day.items(), reverse=True):
            print(f"    {day}: {count} hands")

        # Hands per hour
        hands_per_hour = defaultdict(int)
        for d in all_dates:
            hands_per_hour[d.hour] += 1

        print(f"\n  Hands by hour of day:")
        for hour in sorted(hands_per_hour.keys()):
            count = hands_per_hour[hour]
            bar = '█' * (count // 50)
            print(f"    {hour:02d}:00 - {count:4d} {bar}")

    # Board analysis (showdowns)
    print(f"\n{'='*60}")
    print("BOARD/SHOWDOWN ANALYSIS")
    print(f"{'='*60}")

    boards_with_cards = sum(1 for h in hands if h.get('board'))
    preflop_finishes = len(hands) - boards_with_cards

    print(f"  Hands ending preflop (no board): {preflop_finishes} ({preflop_finishes/len(hands)*100:.1f}%)")
    print(f"  Hands with board cards: {boards_with_cards} ({boards_with_cards/len(hands)*100:.1f}%)")

    board_sizes = defaultdict(int)
    for hand in hands:
        board = hand.get('board', [])
        if board:
            board_sizes[len(board)] += 1

    if board_sizes:
        print("\n  Board sizes (when cards dealt):")
        for size, count in sorted(board_sizes.items()):
            street = {3: 'flop', 4: 'turn', 5: 'river'}.get(size, f'{size} cards')
            print(f"    {street}: {count} hands")

    # Winners analysis
    print(f"\n{'='*60}")
    print("WINNERS ANALYSIS")
    print(f"{'='*60}")

    pots_won = defaultdict(list)
    for hand in hands:
        for winner in hand.get('winners', []):
            user_id = winner.get('user_id')
            amount = winner.get('amount_won', 0)
            if user_id and not is_bot(user_id):
                pots_won[user_id].append(amount)

    print("Human player pot wins:")
    for user_id, amounts in sorted(pots_won.items(), key=lambda x: -sum(x[1])):
        total = sum(amounts)
        display = human_users.get(user_id, {}).get('display_name', user_id[:20])
        print(f"  {display}: {len(amounts)} pots, total {total/100:.2f}")

if __name__ == "__main__":
    print("Fetching hands from Firebase...")
    hands = fetch_all_hands()
    print(f"Fetched {len(hands)} hands")

    if hands:
        analyze_hands(hands)
