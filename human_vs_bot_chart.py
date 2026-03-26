#!/usr/bin/env python3
"""
Human vs Bot Winrate Chart - CORRECTED
Per-hand comparison: Did human or bots win each hand?
"""

import os
from datetime import datetime
from collections import defaultdict

os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = "/Users/davideyal/Projects/stack_poker/backend/stack-24dea-firebase-adminsdk-fbsvc-928dfa73a0.json"

import firebase_admin
from firebase_admin import credentials, firestore
import matplotlib.pyplot as plt
import numpy as np

if not firebase_admin._apps:
    cred = credentials.Certificate(os.environ["GOOGLE_APPLICATION_CREDENTIALS"])
    firebase_admin.initialize_app(cred)

db = firestore.client()

EXCLUDE = {'rlOVEu0G3RX8VgDzuSlp6MVseck1', '5bC6BlYB27g0dfpmaXI8r3bO8iF2'}

def fetch_all_hands():
    """Fetch hands in batches to avoid timeout."""
    all_hands = []
    batch_size = 1000
    last_doc = None

    while True:
        query = db.collection("hands").order_by("started_at").limit(batch_size)
        if last_doc:
            query = query.start_after(last_doc)

        docs = list(query.stream())
        if not docs:
            break

        for doc in docs:
            all_hands.append(doc.to_dict())

        last_doc = docs[-1]
        print(f"  Fetched {len(all_hands)} hands so far...")

        if len(docs) < batch_size:
            break

    return all_hands

def get_user_from_seat(hand, seat_index):
    for seat in hand.get('seats', []):
        if seat.get('seat_index') == seat_index:
            return seat.get('user_id'), seat.get('display_name')
    return None, None

def is_bot(user_id):
    return not user_id or user_id.startswith('user_bot_')

def hand_has_human(hand):
    """Check if hand has a non-excluded human player."""
    for seat in hand.get('seats', []):
        user_id = seat.get('user_id')
        if user_id and user_id not in EXCLUDE and not is_bot(user_id):
            return True
    return False

def get_pnl(hand):
    """Use stack_deltas if valid, otherwise calculate."""
    deltas = hand.get('stack_deltas', {})
    if abs(sum(deltas.values())) < 1:
        return {int(k): v for k, v in deltas.items()}

    winners = hand.get('winners', [])
    is_blitz = any(w.get('hand_description') == 'Blitz fold' for w in winners)

    street_max = defaultdict(lambda: defaultdict(float))
    for a in hand.get('actions', []):
        action = a.get('action')
        seat = a.get('seat')
        amount = a.get('amount') or 0
        street = a.get('street', 'preflop')
        if action in ('post_blind', 'bet', 'call', 'raise_to'):
            street_max[street][seat] = max(street_max[street][seat], amount)

    total_contrib = defaultdict(float)
    for street, seats in street_max.items():
        for seat, amt in seats.items():
            total_contrib[seat] += amt

    won = defaultdict(float)
    winner_seats = set()
    for w in winners:
        seat = w.get('seat')
        winner_seats.add(seat)
        won[seat] += w.get('amount_won', 0)

    pnl = {}
    all_seats = set(total_contrib.keys()) | winner_seats

    if is_blitz:
        for seat in all_seats:
            pnl[seat] = won[seat] - total_contrib[seat]
    else:
        for seat in all_seats:
            if seat in winner_seats:
                pnl[seat] = won[seat]
            else:
                pnl[seat] = -total_contrib[seat]

    return pnl

def create_chart(all_hands):
    # Filter to only hands with non-excluded humans
    human_hands_only = [h for h in all_hands if hand_has_human(h)]
    print(f"Hands with humans (excl degenerate/slickric): {len(human_hands_only)}")

    # Sort hands by time
    hands_with_time = []
    for hand in human_hands_only:
        started = hand.get('started_at')
        if started and isinstance(started, str):
            try:
                dt = datetime.fromisoformat(started.replace('Z', '+00:00'))
                hands_with_time.append((dt, hand))
            except:
                pass

    hands_with_time.sort(key=lambda x: x[0])
    print(f"Hands with timestamps: {len(hands_with_time)}")

    # Track PER HAND: who won?
    human_wins = 0  # Hands where human had positive PnL
    bot_wins = 0    # Hands where bots collectively had positive PnL
    ties = 0        # Hands where both or neither had positive PnL

    human_total_pnl = 0
    bot_total_pnl = 0

    # For cumulative chart
    human_cumulative = []
    bot_cumulative = []

    for dt, hand in hands_with_time:
        pnl = get_pnl(hand)

        human_pnl_this_hand = 0
        bot_pnl_this_hand = 0

        for seat, delta in pnl.items():
            user_id, _ = get_user_from_seat(hand, seat)
            if not user_id or user_id in EXCLUDE:
                continue

            if is_bot(user_id):
                bot_pnl_this_hand += delta
            else:
                human_pnl_this_hand += delta

        human_total_pnl += human_pnl_this_hand
        bot_total_pnl += bot_pnl_this_hand

        # Determine winner of this hand
        if human_pnl_this_hand > 0 and bot_pnl_this_hand <= 0:
            human_wins += 1
        elif bot_pnl_this_hand > 0 and human_pnl_this_hand <= 0:
            bot_wins += 1
        else:
            ties += 1  # Split pot or both break even

        human_cumulative.append(human_total_pnl / 100)
        bot_cumulative.append(bot_total_pnl / 100)

    total_hands = len(hands_with_time)

    # Create the chart
    fig, axes = plt.subplots(2, 2, figsize=(14, 10))
    fig.suptitle('Human vs Bot Performance (All Time, excl. degenerate & slickric)',
                 fontsize=14, fontweight='bold')

    # 1. Cumulative PnL over hands
    ax1 = axes[0, 0]
    hand_numbers = list(range(1, len(human_cumulative) + 1))
    ax1.plot(hand_numbers, human_cumulative, label='Human', color='blue', linewidth=2)
    ax1.plot(hand_numbers, bot_cumulative, label='Bots (combined)', color='red', linewidth=2)
    ax1.axhline(y=0, color='gray', linestyle='--', alpha=0.5)
    ax1.fill_between(hand_numbers, human_cumulative, alpha=0.3, color='blue')
    ax1.fill_between(hand_numbers, bot_cumulative, alpha=0.3, color='red')
    ax1.set_xlabel('Hand Number')
    ax1.set_ylabel('Cumulative PnL ($)')
    ax1.set_title('Cumulative PnL Over Time')
    ax1.legend()
    ax1.grid(True, alpha=0.3)

    # 2. Total PnL Bar Chart
    ax2 = axes[0, 1]
    human_pnl_dollars = human_total_pnl / 100
    bot_pnl_dollars = bot_total_pnl / 100
    colors = ['green' if human_pnl_dollars > 0 else 'red',
              'green' if bot_pnl_dollars > 0 else 'red']
    bars = ax2.bar(['Human', 'Bots'], [human_pnl_dollars, bot_pnl_dollars],
                   color=colors, edgecolor='black')
    ax2.axhline(y=0, color='gray', linestyle='--')
    ax2.set_ylabel('Total PnL ($)')
    ax2.set_title('Total PnL')
    for bar, val in zip(bars, [human_pnl_dollars, bot_pnl_dollars]):
        y_pos = bar.get_height() + (200 if val > 0 else -400)
        ax2.text(bar.get_x() + bar.get_width()/2, y_pos,
                f'${val:,.0f}', ha='center', va='bottom' if val > 0 else 'top',
                fontsize=14, fontweight='bold')

    # 3. Win Rate PER HAND (correct comparison!)
    ax3 = axes[1, 0]
    human_winrate = (human_wins / total_hands * 100) if total_hands > 0 else 0
    bot_winrate = (bot_wins / total_hands * 100) if total_hands > 0 else 0
    tie_rate = (ties / total_hands * 100) if total_hands > 0 else 0

    bars = ax3.bar(['Human Wins', 'Bot Wins', 'Ties'],
                   [human_winrate, bot_winrate, tie_rate],
                   color=['blue', 'red', 'gray'], edgecolor='black')
    ax3.set_ylabel('% of Hands')
    ax3.set_title('Hand Outcomes (Who Won Each Hand?)')
    ax3.set_ylim(0, 100)
    for bar, val, count in zip(bars, [human_winrate, bot_winrate, tie_rate],
                                [human_wins, bot_wins, ties]):
        ax3.text(bar.get_x() + bar.get_width()/2, bar.get_height() + 2,
                f'{val:.1f}%\n({count:,})', ha='center', va='bottom',
                fontsize=12, fontweight='bold')

    # 4. Summary Stats
    ax4 = axes[1, 1]
    ax4.axis('off')

    # Calculate bb/100
    bb = 200  # $2 big blind in cents
    human_bb100 = (human_total_pnl / bb) / (total_hands / 100) if total_hands > 0 else 0

    stats_text = f"""
    SUMMARY STATISTICS
    ══════════════════════════════════════

    TOTAL HANDS ANALYZED: {total_hands:,}

    HAND OUTCOMES
    ──────────────────────────────────────
    Human Won:        {human_wins:,} hands ({human_winrate:.1f}%)
    Bots Won:         {bot_wins:,} hands ({bot_winrate:.1f}%)
    Ties/Splits:      {ties:,} hands ({tie_rate:.1f}%)

    PnL SUMMARY
    ──────────────────────────────────────
    Human Total PnL:  ${human_total_pnl/100:,.2f}
    Bots Total PnL:   ${bot_total_pnl/100:,.2f}

    Human bb/100:     {human_bb100:+.1f}
    Human $/hand:     ${human_total_pnl/100/total_hands:.2f}

    ══════════════════════════════════════
    Zero-sum check:   ${(human_total_pnl + bot_total_pnl)/100:,.2f}
    (should be ~$0)
    """

    ax4.text(0.1, 0.95, stats_text, transform=ax4.transAxes, fontsize=11,
             verticalalignment='top', fontfamily='monospace',
             bbox=dict(boxstyle='round', facecolor='wheat', alpha=0.5))

    plt.tight_layout()
    plt.savefig('human_vs_bot_winrate.png', dpi=150, bbox_inches='tight')
    print(f"\nSaved human_vs_bot_winrate.png")

    # Print summary
    print(f"\n{'='*50}")
    print("HUMAN VS BOT SUMMARY (Correct Per-Hand Analysis)")
    print(f"{'='*50}")
    print(f"Total hands analyzed: {total_hands:,}")
    print(f"\nHand outcomes:")
    print(f"  Human won: {human_wins:,} ({human_winrate:.1f}%)")
    print(f"  Bots won:  {bot_wins:,} ({bot_winrate:.1f}%)")
    print(f"  Ties:      {ties:,} ({tie_rate:.1f}%)")
    print(f"\nPnL:")
    print(f"  Human: ${human_total_pnl/100:,.2f}")
    print(f"  Bots:  ${bot_total_pnl/100:,.2f}")
    print(f"  Human bb/100: {human_bb100:+.1f}")
    print(f"\nZero-sum check: ${(human_total_pnl + bot_total_pnl)/100:,.2f}")

if __name__ == "__main__":
    print("Fetching hands from Firebase...")
    hands = fetch_all_hands()
    print(f"Fetched {len(hands)} total hands")
    create_chart(hands)
