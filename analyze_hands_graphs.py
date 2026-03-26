#!/usr/bin/env python3
"""
Analyze hands collection with graphs - Feb 20-23 only.
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
    return [doc.to_dict() for doc in db.collection("hands").stream()]

def get_user_from_seat(hand, seat_index):
    for seat in hand.get('seats', []):
        if seat.get('seat_index') == seat_index:
            return seat.get('user_id'), seat.get('display_name')
    return None, None

def is_bot(user_id):
    return not user_id or user_id.startswith('user_bot_')

def is_in_date_range(hand):
    """Check if hand is in Feb 20-23."""
    started = hand.get('started_at')
    if started and isinstance(started, str):
        try:
            dt = datetime.fromisoformat(started.replace('Z', '+00:00'))
            return dt.month == 2 and 20 <= dt.day <= 23
        except:
            pass
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

def create_graphs(all_hands):
    # Filter to Feb 20-23
    hands = [h for h in all_hands if is_in_date_range(h)]
    print(f"Filtered to {len(hands)} hands (Feb 20-23)")

    user_stats = defaultdict(lambda: {
        'hands': 0, 'pnl_total': 0, 'pnl_list': [], 'display_name': None,
        'is_bot': False, 'wins': 0, 'losses': 0
    })

    all_dates = []

    for hand in hands:
        started = hand.get('started_at')
        if started and isinstance(started, str):
            try:
                dt = datetime.fromisoformat(started.replace('Z', '+00:00'))
                all_dates.append(dt)
            except:
                pass

        pnl = get_pnl(hand)
        for seat, delta in pnl.items():
            user_id, display_name = get_user_from_seat(hand, seat)
            if user_id:
                stats = user_stats[user_id]
                stats['hands'] += 1
                stats['pnl_total'] += delta
                stats['pnl_list'].append(delta)
                stats['display_name'] = display_name
                stats['is_bot'] = is_bot(user_id)
                if delta > 0:
                    stats['wins'] += 1
                elif delta < 0:
                    stats['losses'] += 1

    human_users = {k: v for k, v in user_stats.items() if not v['is_bot'] and k not in EXCLUDE}

    fig = plt.figure(figsize=(16, 20))
    fig.suptitle('Stack Poker Analytics: Feb 20-23, 2026', fontsize=16, fontweight='bold')

    # 1. Human Players PnL Bar Chart
    ax1 = fig.add_subplot(3, 2, 1)
    top_players = sorted(human_users.items(), key=lambda x: -x[1]['hands'])[:15]
    names = [p[1]['display_name'] or p[0][:10] for p in top_players]
    pnls = [p[1]['pnl_total']/100 for p in top_players]
    colors = ['green' if p > 0 else 'red' for p in pnls]
    ax1.barh(names, pnls, color=colors)
    ax1.set_xlabel('PnL ($)')
    ax1.set_title('Human Players PnL (Top 15 by volume)')
    ax1.invert_yaxis()

    # 2. Win Rate vs Hands Played
    ax2 = fig.add_subplot(3, 2, 2)
    for user_id, stats in human_users.items():
        if stats['hands'] >= 5:
            win_rate = stats['wins'] / stats['hands'] * 100
            ax2.scatter(stats['hands'], win_rate, s=100, alpha=0.7)
            ax2.annotate(stats['display_name'] or user_id[:8],
                        (stats['hands'], win_rate), fontsize=8)
    ax2.axhline(y=50, color='gray', linestyle='--', alpha=0.5)
    ax2.set_xlabel('Hands Played')
    ax2.set_ylabel('Win Rate (%)')
    ax2.set_title('Win Rate vs Volume (min 5 hands)')

    # 3. Hands Per Day (human hands only)
    ax3 = fig.add_subplot(3, 2, 3)
    if all_dates:
        hands_per_day = defaultdict(int)
        for d in all_dates:
            hands_per_day[d.strftime('%m-%d')] += 1
        dates = sorted(hands_per_day.keys())
        counts = [hands_per_day[d] for d in dates]
        ax3.bar(dates, counts, color='steelblue')
        ax3.set_xlabel('Date (Feb)')
        ax3.set_ylabel('Hands')
        ax3.set_title('Hands Per Day')
        for i, (d, c) in enumerate(zip(dates, counts)):
            ax3.text(i, c + 100, str(c), ha='center', va='bottom', fontsize=10)

    # 4. Hands by Hour
    ax4 = fig.add_subplot(3, 2, 4)
    if all_dates:
        hands_per_hour = defaultdict(int)
        for d in all_dates:
            hands_per_hour[d.hour] += 1
        hours = list(range(24))
        counts = [hands_per_hour[h] for h in hours]
        ax4.bar(hours, counts, color='coral')
        ax4.set_xlabel('Hour of Day (PST)')
        ax4.set_ylabel('Hands')
        ax4.set_title('Activity by Hour')
        ax4.set_xticks(hours)

    # 5. PnL Distribution
    ax5 = fig.add_subplot(3, 2, 5)
    top_by_hands = sorted(human_users.items(), key=lambda x: -x[1]['hands'])[:5]
    for user_id, stats in top_by_hands:
        if stats['pnl_list']:
            pnls = [p/100 for p in stats['pnl_list']]
            ax5.hist(pnls, bins=30, alpha=0.5, label=stats['display_name'] or user_id[:10])
    ax5.set_xlabel('PnL per Hand ($)')
    ax5.set_ylabel('Frequency')
    ax5.set_title('PnL Distribution (Top 5 by Volume)')
    ax5.legend()

    # 6. Cumulative PnL
    ax6 = fig.add_subplot(3, 2, 6)
    user_timeline = defaultdict(list)
    for hand in hands:
        started = hand.get('started_at')
        if started and isinstance(started, str):
            try:
                dt = datetime.fromisoformat(started.replace('Z', '+00:00'))
            except:
                continue
        else:
            continue

        pnl = get_pnl(hand)
        for seat, delta in pnl.items():
            user_id, display_name = get_user_from_seat(hand, seat)
            if user_id and not is_bot(user_id) and user_id not in EXCLUDE:
                user_timeline[user_id].append((dt, delta, display_name))

    for user_id, timeline in sorted(user_timeline.items(), key=lambda x: -len(x[1]))[:6]:
        if len(timeline) >= 10:
            timeline.sort(key=lambda x: x[0])
            cumsum = np.cumsum([t[1]/100 for t in timeline])
            display_name = timeline[0][2] or user_id[:10]
            ax6.plot(range(len(cumsum)), cumsum, label=display_name, linewidth=2)

    ax6.axhline(y=0, color='gray', linestyle='--', alpha=0.5)
    ax6.set_xlabel('Hand Number')
    ax6.set_ylabel('Cumulative PnL ($)')
    ax6.set_title('Cumulative PnL Over Time')
    ax6.legend()

    plt.tight_layout()
    plt.savefig('hands_analysis.png', dpi=150, bbox_inches='tight')
    print("Saved hands_analysis.png")

    # Human vs Bot
    fig2, axes = plt.subplots(1, 2, figsize=(12, 5))
    fig2.suptitle('Humans vs Bots: Feb 20-23, 2026', fontsize=14, fontweight='bold')

    human_hands = sum(s['hands'] for s in human_users.values())
    bot_hands = sum(s['hands'] for s in user_stats.values() if s['is_bot'])
    axes[0].pie([human_hands, bot_hands], labels=[f'Humans\n({human_hands:,})', f'Bots\n({bot_hands:,})'],
                autopct='%1.1f%%', colors=['steelblue', 'lightgray'])
    axes[0].set_title('Player-Hands Distribution')

    human_pnl = sum(s['pnl_total'] for s in human_users.values()) / 100
    bot_pnl = sum(s['pnl_total'] for s in user_stats.values() if s['is_bot']) / 100
    colors = ['green' if human_pnl > 0 else 'red', 'green' if bot_pnl > 0 else 'red']
    bars = axes[1].bar(['Humans', 'Bots'], [human_pnl, bot_pnl], color=colors)
    axes[1].axhline(y=0, color='gray', linestyle='--')
    axes[1].set_ylabel('Total PnL ($)')
    axes[1].set_title('Total PnL')

    # Add labels on bars
    for bar, val in zip(bars, [human_pnl, bot_pnl]):
        axes[1].text(bar.get_x() + bar.get_width()/2, bar.get_height() + 100,
                    f'${val:,.0f}', ha='center', va='bottom', fontsize=12, fontweight='bold')

    plt.tight_layout()
    plt.savefig('human_vs_bot.png', dpi=150, bbox_inches='tight')
    print("Saved human_vs_bot.png")

    # Summary
    total_hands = len(hands)
    human_hand_count = sum(s['hands'] for s in human_users.values())
    total_pnl = sum(s['pnl_total'] for s in human_users.values()) / 100
    bb_per_100 = (total_pnl / 2) / (human_hand_count / 100) if human_hand_count > 0 else 0

    print(f"\n=== SUMMARY: Feb 20-23 (excl. degenerate & slickric) ===")
    print(f"Unique Players: {len(human_users)}")
    print(f"Total Hands: {total_hands}")
    print(f"Human Player-Hands: {human_hand_count}")
    print(f"Total PnL: ${total_pnl:,.2f}")
    print(f"bb/100: {bb_per_100:.1f}")

if __name__ == "__main__":
    print("Fetching hands from Firebase...")
    hands = fetch_all_hands()
    print(f"Fetched {len(hands)} total hands")
    create_graphs(hands)
