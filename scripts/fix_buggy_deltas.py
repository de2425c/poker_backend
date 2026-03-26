#!/usr/bin/env python3
"""
Fix stack_deltas for hands logged by the buggy server (port 8001).

The buggy code calculated: delta = amount_won - contributed
But amount_won IS already the net profit, so it should be: delta = amount_won

Detection: For any winner, if stored_delta != amount_won, the hand is buggy.

Usage:
    python scripts/fix_buggy_deltas.py --dry-run        # Preview changes
    python scripts/fix_buggy_deltas.py                  # Apply fixes
    python scripts/fix_buggy_deltas.py --limit 100      # Process only 100 hands
"""

import argparse
from datetime import datetime, timezone
import firebase_admin
from firebase_admin import firestore


# Buggy code was deployed around this time
BUGGY_DEPLOY_TIME = datetime(2026, 3, 9, 2, 21, 52, tzinfo=timezone.utc)


def is_buggy_hand(hand: dict) -> bool:
    """
    Detect if a hand was logged by the buggy server.

    Buggy code: delta = amount_won - contributed (wrong)
    Correct code: delta = amount_won (right, since amount_won IS the net profit)

    Returns True if any winner's delta != their amount_won.
    """
    winners = hand.get("winners", [])
    stack_deltas = hand.get("stack_deltas", {})

    if not winners:
        return False

    for w in winners:
        seat = w["seat"]
        amount_won = w["amount_won"]

        # stack_deltas keys might be strings or ints
        stored_delta = stack_deltas.get(seat) or stack_deltas.get(str(seat))

        if stored_delta is None:
            continue

        # If delta != amount_won, this hand was logged by buggy code
        if stored_delta != amount_won:
            return True

    return False


def fix_stack_deltas(hand: dict) -> dict[str, int]:
    """
    Recalculate correct stack_deltas.

    For winners: delta = amount_won (the PokerKit payoff, which IS net profit)
    For non-winners: delta = -contributed (calculated from actions)
    """
    actions = hand.get("actions", [])
    winners = hand.get("winners", [])
    seats = hand.get("seats", [])

    if not seats:
        return {}

    # Get winner amounts (these are PokerKit payoffs = net profit)
    winner_deltas = {}
    for w in winners:
        seat = w["seat"]
        winner_deltas[seat] = winner_deltas.get(seat, 0) + w["amount_won"]

    # Calculate contributions for non-winners
    seat_indices = {s["seat_index"] for s in seats}
    street_contrib = {s["seat_index"]: 0 for s in seats}
    total_contributed = {s["seat_index"]: 0 for s in seats}

    last_aggressor_seat = None
    last_aggressor_amount = 0
    current_street = "preflop"

    for action in actions:
        seat = action["seat"]
        act = action["action"]
        amount = action.get("amount")
        street = action.get("street", "preflop")

        # New street - finalize previous street contributions
        if street != current_street:
            for s in seat_indices:
                total_contributed[s] += street_contrib.get(s, 0)
            street_contrib = {s: 0 for s in seat_indices}
            current_street = street
            last_aggressor_seat = None
            last_aggressor_amount = 0

        # Track contribution (max per seat per street - cumulative amounts)
        if amount:
            street_contrib[seat] = max(street_contrib.get(seat, 0), amount)

        # Track last aggressor for uncalled bet detection
        if act in ("bet", "raise_to") and amount:
            last_aggressor_seat = seat
            last_aggressor_amount = amount
        elif act in ("call", "check"):
            last_aggressor_seat = None
            last_aggressor_amount = 0

    # Finalize last street contributions
    for s in seat_indices:
        total_contributed[s] += street_contrib.get(s, 0)

    # Handle uncalled bet
    if last_aggressor_seat is not None and last_aggressor_amount > 0:
        called_amount = 0
        for seat_idx, amount in street_contrib.items():
            if seat_idx != last_aggressor_seat:
                called_amount = max(called_amount, amount)
        uncalled = last_aggressor_amount - called_amount
        if uncalled > 0:
            total_contributed[last_aggressor_seat] -= uncalled

    # Build final deltas
    stack_deltas = {}
    for seat_idx in seat_indices:
        if seat_idx in winner_deltas:
            # Winner: use amount_won directly (it's the PokerKit payoff = net profit)
            delta = winner_deltas[seat_idx]
        else:
            # Non-winner: lost what they contributed
            delta = -total_contributed.get(seat_idx, 0)

        if delta != 0:
            stack_deltas[str(seat_idx)] = delta

    return stack_deltas


def run_migration(dry_run: bool = True, limit: int = None):
    """
    Find and fix hands logged by the buggy server.
    """
    if not firebase_admin._apps:
        firebase_admin.initialize_app()
    db = firestore.client()

    buggy_count = 0
    fixed_count = 0
    skipped_count = 0
    errors = 0
    processed = 0
    batch_size = 500

    last_doc = None

    print(f"Scanning for buggy hands (deployed after {BUGGY_DEPLOY_TIME})...")
    print(f"Mode: {'DRY RUN' if dry_run else 'LIVE - WILL UPDATE FIRESTORE'}")
    print()

    while True:
        # Query hands after buggy deployment
        query = (db.collection("hands")
                 .where("ended_at", ">=", BUGGY_DEPLOY_TIME)
                 .order_by("ended_at")
                 .limit(batch_size))

        if last_doc:
            query = query.start_after(last_doc)

        docs = list(query.get())
        if not docs:
            break

        for doc in docs:
            if limit and processed >= limit:
                break

            hand = doc.to_dict()
            hand_id = hand.get("hand_id", doc.id)

            try:
                if is_buggy_hand(hand):
                    buggy_count += 1
                    old_deltas = hand.get("stack_deltas", {})
                    new_deltas = fix_stack_deltas(hand)

                    # Verify chip conservation
                    delta_sum = sum(new_deltas.values())
                    if delta_sum != 0:
                        print(f"  WARNING: {hand_id} deltas don't sum to 0 (sum={delta_sum})")

                    if not dry_run:
                        db.collection("hands").document(doc.id).update({
                            "stack_deltas": new_deltas
                        })

                    fixed_count += 1

                    if buggy_count <= 5:  # Show first few examples
                        print(f"  Buggy hand: {hand_id}")
                        print(f"    Old deltas: {old_deltas}")
                        print(f"    New deltas: {new_deltas}")
                else:
                    skipped_count += 1

            except Exception as e:
                print(f"Error processing {hand_id}: {e}")
                errors += 1

            processed += 1

        if limit and processed >= limit:
            break

        last_doc = docs[-1]
        if processed % 1000 == 0:
            print(f"Processed {processed}... (buggy: {buggy_count}, skipped: {skipped_count})")

    print()
    print("=" * 50)
    print("MIGRATION COMPLETE")
    print("=" * 50)
    print(f"  Total processed: {processed}")
    print(f"  Buggy hands found: {buggy_count}")
    print(f"  Fixed: {fixed_count}")
    print(f"  Skipped (correct): {skipped_count}")
    print(f"  Errors: {errors}")
    if dry_run:
        print()
        print("(Dry run - no changes made. Run without --dry-run to apply fixes)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Fix buggy stack_deltas from port 8001 server")
    parser.add_argument("--dry-run", action="store_true", help="Preview without making changes")
    parser.add_argument("--limit", type=int, help="Process only N hands")
    args = parser.parse_args()

    run_migration(dry_run=args.dry_run, limit=args.limit)
