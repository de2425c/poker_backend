#!/usr/bin/env python3
"""
Migration script to fix historical hand stack_deltas.

The bug: When a player bets and everyone folds, the uncalled bet was incorrectly
included in their contribution calculation, leading to wrong stack_deltas.

Usage:
    python scripts/migrate_stack_deltas.py [--dry-run] [--limit N]

Options:
    --dry-run   Show what would be changed without updating
    --limit N   Only process first N hands (for testing)
"""

import argparse
import firebase_admin
from firebase_admin import firestore


def recalculate_stack_deltas(hand: dict) -> dict[str, int]:
    """
    Recalculate stack_deltas by replaying contribution logic from actions.

    Correctly handles uncalled bets - when a player bets/raises and everyone
    folds, the uncalled portion should not count as a contribution.
    """
    actions = hand.get("actions", [])
    winners = hand.get("winners", [])
    seats = hand.get("seats", [])

    # Track contributions per street (max amount per seat per street)
    street_contrib = {}
    total_contributed = {s["seat_index"]: 0 for s in seats}

    # Track last aggressor for uncalled bet detection
    last_aggressor_seat = None
    last_aggressor_amount = 0
    current_street = "preflop"

    for action in actions:
        seat = action["seat"]
        act = action["action"]
        amount = action.get("amount")
        street = action["street"]

        # New street - finalize previous street contributions
        if street != current_street:
            for s, amt in street_contrib.items():
                total_contributed[s] += amt
            street_contrib = {}
            current_street = street
            last_aggressor_seat = None
            last_aggressor_amount = 0

        # Track contribution (max per seat per street since amounts are cumulative)
        if amount:
            street_contrib[seat] = max(street_contrib.get(seat, 0), amount)

        # Track aggressor for uncalled bet detection
        if act in ("bet", "raise_to") and amount:
            last_aggressor_seat = seat
            last_aggressor_amount = amount
        elif act in ("call", "check"):
            last_aggressor_seat = None
            last_aggressor_amount = 0
        # fold doesn't reset - bet remains uncalled

    # Finalize last street contributions
    for s, amt in street_contrib.items():
        total_contributed[s] += amt

    # Handle uncalled bet
    if last_aggressor_seat is not None and last_aggressor_amount > 0:
        called_amount = max(
            (street_contrib.get(s, 0) for s in street_contrib if s != last_aggressor_seat),
            default=0
        )
        uncalled = last_aggressor_amount - called_amount
        if uncalled > 0:
            total_contributed[last_aggressor_seat] -= uncalled

    # Build chips_won from winners
    chips_won = {}
    for w in winners:
        chips_won[w["seat"]] = chips_won.get(w["seat"], 0) + w["amount_won"]

    # Calculate stack deltas
    stack_deltas = {}
    for seat_rec in seats:
        seat_idx = seat_rec["seat_index"]
        contributed = total_contributed.get(seat_idx, 0)
        if chips_won.get(seat_idx, 0) > 0:
            delta = chips_won[seat_idx] - contributed
        else:
            delta = -contributed
        if delta != 0:
            stack_deltas[str(seat_idx)] = delta

    return stack_deltas


def migrate_hands(dry_run: bool = True, limit: int = None):
    """
    Fetch all hands from Firestore and recalculate stack_deltas.

    Args:
        dry_run: If True, show what would be changed without updating
        limit: Only process first N hands (for testing)
    """
    # Initialize Firebase
    if not firebase_admin._apps:
        firebase_admin.initialize_app()
    db = firestore.client()

    updated = 0
    unchanged = 0
    errors = 0
    processed = 0
    batch_size = 500

    # Use pagination to avoid streaming issues
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

                if old_deltas != new_deltas:
                    print(f"Hand {hand_id}:")
                    print(f"  Old: {old_deltas}")
                    print(f"  New: {new_deltas}")

                    if not dry_run:
                        db.collection("hands").document(doc.id).update({
                            "stack_deltas": new_deltas
                        })
                        print(f"  Updated!")

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
        print(f"Processed {processed} hands so far...")

    print(f"\nSummary:")
    print(f"  {updated} hands need updates")
    print(f"  {unchanged} hands unchanged")
    print(f"  {errors} errors")
    if dry_run:
        print("(Dry run - no changes made)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Fix historical hand stack_deltas in Firestore"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be changed without updating"
    )
    parser.add_argument(
        "--limit",
        type=int,
        help="Only process first N hands (for testing)"
    )
    args = parser.parse_args()

    migrate_hands(dry_run=args.dry_run, limit=args.limit)
