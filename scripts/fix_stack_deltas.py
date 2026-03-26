#!/usr/bin/env python3
"""
FIX script to restore correct stack_deltas after broken migration.

This uses the EXACT same logic as hand_logger.py to ensure consistency.

Usage:
    python scripts/fix_stack_deltas.py [--dry-run] [--limit N]
"""

import argparse
import firebase_admin
from firebase_admin import firestore


def recalculate_stack_deltas(hand: dict) -> dict[str, int]:
    """
    Recalculate stack_deltas ensuring chip conservation.

    The winners data in Firestore may be incorrect (logged wrong amounts).
    We calculate contributions from actions, then distribute the pot to winners
    proportionally based on their claimed amounts.
    """
    actions = hand.get("actions", [])
    winners = hand.get("winners", [])
    seats = hand.get("seats", [])

    if not seats:
        return {}

    # Initialize all seats
    seat_indices = {s["seat_index"] for s in seats}
    street_contrib = {s["seat_index"]: 0 for s in seats}
    total_contributed = {s["seat_index"]: 0 for s in seats}

    # Track last aggressor for uncalled bet detection
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

    # Calculate actual pot from contributions
    total_pot = sum(total_contributed.values())

    # Get winner seats and their claimed amounts (for proportional distribution)
    winner_claimed = {}
    for w in winners:
        seat = w["seat"]
        winner_claimed[seat] = winner_claimed.get(seat, 0) + w["amount_won"]

    total_claimed = sum(winner_claimed.values())

    # Calculate actual winnings based on pot (not claimed amounts)
    # Distribute pot proportionally if multiple winners
    actual_won = {}
    if total_claimed > 0 and winner_claimed:
        for seat, claimed in winner_claimed.items():
            # Proportional share of actual pot
            actual_won[seat] = int(total_pot * claimed / total_claimed)

        # Handle rounding - give remainder to first winner
        remainder = total_pot - sum(actual_won.values())
        if remainder != 0 and winner_claimed:
            first_winner = next(iter(winner_claimed.keys()))
            actual_won[first_winner] += remainder
    elif winner_claimed:
        # If no claimed amounts, split equally
        per_winner = total_pot // len(winner_claimed)
        remainder = total_pot % len(winner_claimed)
        for i, seat in enumerate(winner_claimed.keys()):
            actual_won[seat] = per_winner + (1 if i < remainder else 0)

    # Calculate stack deltas
    stack_deltas = {}
    for seat_idx in seat_indices:
        contributed = total_contributed.get(seat_idx, 0)
        won = actual_won.get(seat_idx, 0)

        if won > 0:
            delta = won - contributed
        else:
            delta = -contributed

        if delta != 0:
            stack_deltas[str(seat_idx)] = delta

    return stack_deltas


def fix_hands(dry_run: bool = True, limit: int = None):
    """
    Fix all hands in Firestore with correct stack_deltas calculation.
    """
    if not firebase_admin._apps:
        firebase_admin.initialize_app()
    db = firestore.client()

    updated = 0
    unchanged = 0
    errors = 0
    processed = 0
    batch_size = 500

    last_doc = None

    while True:
        query = db.collection("hands").order_by("__name__").limit(batch_size)
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
                old_deltas = hand.get("stack_deltas", {})
                new_deltas = recalculate_stack_deltas(hand)

                # Check if deltas sum to zero
                delta_sum = sum(new_deltas.values())

                if old_deltas != new_deltas:
                    if not dry_run:
                        db.collection("hands").document(doc.id).update({
                            "stack_deltas": new_deltas
                        })
                    updated += 1
                else:
                    unchanged += 1

            except Exception as e:
                print(f"Error processing {hand_id}: {e}")
                errors += 1

            processed += 1

        if limit and processed >= limit:
            break

        last_doc = docs[-1]
        print(f"Processed {processed} hands... (updated: {updated}, unchanged: {unchanged}, errors: {errors})")

    print(f"\n=== COMPLETE ===")
    print(f"  Total processed: {processed}")
    print(f"  Updated: {updated}")
    print(f"  Unchanged: {unchanged}")
    print(f"  Errors: {errors}")
    if dry_run:
        print("(Dry run - no changes made)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Fix stack_deltas in Firestore")
    parser.add_argument("--dry-run", action="store_true", help="Don't actually update")
    parser.add_argument("--limit", type=int, help="Process only N hands")
    args = parser.parse_args()

    fix_hands(dry_run=args.dry_run, limit=args.limit)
