#!/usr/bin/env python3
"""
Backfill participant_ids field on existing hands in Firestore.

This adds a flat array of user IDs to enable array-contains queries.

Usage:
    python scripts/backfill_participant_ids.py
"""

import firebase_admin
from firebase_admin import firestore


def backfill():
    # Initialize Firebase
    if not firebase_admin._apps:
        firebase_admin.initialize_app()

    db = firestore.client()
    hands_ref = db.collection("hands")

    # Get all hands
    print("Fetching all hands...")
    docs = hands_ref.stream()

    updated = 0
    skipped = 0
    errors = 0

    for doc in docs:
        hand_id = doc.id
        data = doc.to_dict()

        # Skip if already has participant_ids
        if "participant_ids" in data and data["participant_ids"]:
            skipped += 1
            continue

        # Extract user_ids from seats
        seats = data.get("seats", [])
        participant_ids = []

        for seat in seats:
            user_id = seat.get("user_id")
            if user_id:
                participant_ids.append(user_id)

        if not participant_ids:
            print(f"  WARNING: No participants found in hand {hand_id}")
            errors += 1
            continue

        # Update the document
        try:
            hands_ref.document(hand_id).update({
                "participant_ids": participant_ids
            })
            updated += 1
            if updated % 100 == 0:
                print(f"  Updated {updated} hands...")
        except Exception as e:
            print(f"  ERROR updating {hand_id}: {e}")
            errors += 1

    print(f"\nDone!")
    print(f"  Updated: {updated}")
    print(f"  Skipped (already had field): {skipped}")
    print(f"  Errors: {errors}")


if __name__ == "__main__":
    backfill()
