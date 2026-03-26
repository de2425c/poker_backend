#!/usr/bin/env python3
"""
Verify stack_deltas by recalculating from actions array only.

This script queries hands from Firestore and verifies that stored stack_deltas
match what we calculate purely from the actions array.

Usage:
    python scripts/verify_stack_deltas.py --limit 100    # Check first 100 hands
    python scripts/verify_stack_deltas.py                # Check all hands
"""

import argparse
import firebase_admin
from firebase_admin import firestore


def calculate_contributions(hand: dict) -> dict[int, int]:
    """
    Calculate total contributions per seat from actions only.

    Returns dict of seat_index -> total_contributed
    """
    actions = hand.get("actions", [])
    seats = hand.get("seats", [])

    if not seats:
        return {}

    seat_indices = {s["seat_index"] for s in seats}
    street_contrib = {s: 0 for s in seat_indices}
    total_contributed = {s: 0 for s in seat_indices}

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

        # Track contribution (max per seat per street - amounts are cumulative)
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

    return total_contributed


def recalculate_stack_deltas(hand: dict) -> dict[str, int]:
    """
    Recalculate stack_deltas from actions array only.

    - Contributions calculated purely from actions
    - Winners get: total_pot - their_contribution
    - Non-winners get: -their_contribution
    """
    winners = hand.get("winners", [])
    seats = hand.get("seats", [])

    if not seats:
        return {}

    seat_indices = {s["seat_index"] for s in seats}
    total_contributed = calculate_contributions(hand)

    # Calculate total pot from contributions
    total_pot = sum(total_contributed.values())

    # Get winner seats (just who won, not amounts)
    winner_seats = set()
    for w in winners:
        winner_seats.add(w["seat"])

    # Calculate stack deltas
    stack_deltas = {}

    if len(winner_seats) == 1:
        # Single winner gets entire pot
        winner_seat = next(iter(winner_seats))
        for seat_idx in seat_indices:
            contributed = total_contributed.get(seat_idx, 0)
            if seat_idx == winner_seat:
                delta = total_pot - contributed
            else:
                delta = -contributed
            if delta != 0:
                stack_deltas[str(seat_idx)] = delta
    else:
        # Multiple winners - use winner amounts for proportional split
        winner_claimed = {}
        for w in winners:
            seat = w["seat"]
            winner_claimed[seat] = winner_claimed.get(seat, 0) + w["amount_won"]

        total_claimed = sum(winner_claimed.values())

        actual_won = {}
        if total_claimed > 0:
            for seat, claimed in winner_claimed.items():
                actual_won[seat] = int(total_pot * claimed / total_claimed)
            # Handle rounding
            remainder = total_pot - sum(actual_won.values())
            if remainder != 0:
                first_winner = next(iter(winner_claimed.keys()))
                actual_won[first_winner] += remainder
        else:
            # Split equally
            per_winner = total_pot // len(winner_seats)
            remainder = total_pot % len(winner_seats)
            for i, seat in enumerate(winner_seats):
                actual_won[seat] = per_winner + (1 if i < remainder else 0)

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


def verify_hands(limit: int = None):
    """
    Verify all hands in Firestore have correct stack_deltas.
    """
    if not firebase_admin._apps:
        firebase_admin.initialize_app()
    db = firestore.client()

    mismatches = []
    correct = 0
    errors = 0
    processed = 0
    batch_size = 500

    last_doc = None

    print("Verifying stack_deltas from actions array...")
    print()

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
                stored_deltas = hand.get("stack_deltas", {})
                calculated_deltas = recalculate_stack_deltas(hand)

                # Normalize keys to strings for comparison
                stored_normalized = {str(k): v for k, v in stored_deltas.items()}

                if stored_normalized != calculated_deltas:
                    # Calculate the difference
                    all_seats = set(stored_normalized.keys()) | set(calculated_deltas.keys())
                    diff = {}
                    for seat in all_seats:
                        stored_val = stored_normalized.get(seat, 0)
                        calc_val = calculated_deltas.get(seat, 0)
                        if stored_val != calc_val:
                            diff[seat] = {"stored": stored_val, "calculated": calc_val}

                    mismatches.append({
                        "hand_id": hand_id,
                        "stored": stored_normalized,
                        "calculated": calculated_deltas,
                        "diff": diff
                    })
                else:
                    correct += 1

            except Exception as e:
                print(f"Error processing {hand_id}: {e}")
                errors += 1

            processed += 1

        if limit and processed >= limit:
            break

        last_doc = docs[-1]
        if processed % 1000 == 0:
            print(f"Processed {processed}... (correct: {correct}, mismatches: {len(mismatches)})")

    # Print results
    print()
    print("=" * 60)
    print("VERIFICATION RESULTS")
    print("=" * 60)
    print(f"  Total hands checked: {processed}")
    print(f"  Correct: {correct}")
    print(f"  Mismatches: {len(mismatches)}")
    print(f"  Errors: {errors}")
    print()

    if mismatches:
        print("MISMATCHES:")
        print("-" * 60)
        for m in mismatches[:20]:  # Show first 20
            print(f"\nHand: {m['hand_id']}")
            print(f"  Stored:     {m['stored']}")
            print(f"  Calculated: {m['calculated']}")
            print(f"  Difference: {m['diff']}")

        if len(mismatches) > 20:
            print(f"\n... and {len(mismatches) - 20} more mismatches")
    else:
        print("All hands verified correctly!")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Verify stack_deltas in Firestore")
    parser.add_argument("--limit", type=int, help="Check only N hands")
    args = parser.parse_args()

    verify_hands(limit=args.limit)
