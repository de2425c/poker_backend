#!/usr/bin/env python3
"""Seed HU preflop regrets from GTO ranges JSON.

Parses nested JSON with preflop ranges and initializes regrets so that:
- Hands in raise range get positive regret for raise actions
- Hands in call range get positive regret for call
- Hands not in any range get positive regret for fold

Usage:
    python scripts/seed_hu_preflop.py --input hu_ranges.json --output models/hu_seed/checkpoint_seed.pkl
"""

import argparse
import json
import os
import pickle
import sys
from collections import defaultdict
from typing import Dict, List, Tuple, Any

# FCHPA action mapping
FOLD = 0
CALL = 1      # Also check
POT = 2
ALL_IN = 3
HALF_POT = 4

RANKS = "23456789TJQKA"
SUITS = "cdhs"


def hand_str_to_ints(hand: str) -> Tuple[int, int]:
    """Convert hand string like 'Ac2d' to two card ints (0-51).

    Card encoding: rank * 4 + suit
    where rank: 2=0, 3=1, ..., A=12
    and suit: c=0, d=1, h=2, s=3
    """
    c1 = hand[:2]
    c2 = hand[2:]

    r1 = RANKS.index(c1[0])
    s1 = SUITS.index(c1[1])
    r2 = RANKS.index(c2[0])
    s2 = SUITS.index(c2[1])

    return (r1 * 4 + s1, r2 * 4 + s2)


def get_preflop_bucket(hole_cards: Tuple[int, int]) -> int:
    """Get preflop hand bucket (0-168 for 169 canonical preflop hands).

    Matches the legacy formula in abstract_mccfr.py for compatibility.
    """
    c1, c2 = hole_cards
    r1, s1 = c1 // 4, c1 % 4
    r2, s2 = c2 // 4, c2 % 4

    # Ensure higher rank is first
    if r1 < r2:
        r1, r2 = r2, r1
        s1, s2 = s2, s1

    is_suited = (s1 == s2)

    if r1 == r2:
        # Pairs: 0-12 (22=0, 33=1, ..., AA=12)
        return r1
    elif is_suited:
        # Suited: legacy loop formula
        idx = 13
        for hi in range(12, r1, -1):
            idx += hi
        idx += (r1 - 1 - r2)
        return min(idx, 168)
    else:
        # Offsuit: legacy loop formula
        idx = 13 + 78  # 13 pairs + 78 suited
        for hi in range(12, r1, -1):
            idx += hi
        idx += (r1 - 1 - r2)
        return min(idx, 168)


def parse_range_raw(range_raw: str) -> Dict[str, float]:
    """Parse RangeRaw string into hand -> frequency dict."""
    result = {}
    if not range_raw:
        return result

    for item in range_raw.split(","):
        item = item.strip()
        if ":" not in item:
            continue
        hand, freq = item.split(":")
        hand = hand.strip()
        freq = float(freq.strip())
        result[hand] = freq

    return result


def aggregate_to_buckets(hand_freqs: Dict[str, float]) -> Dict[int, float]:
    """Aggregate hand frequencies to bucket frequencies.

    For each bucket, takes the max frequency across all combos in that bucket.
    """
    bucket_freqs = defaultdict(float)

    for hand, freq in hand_freqs.items():
        try:
            ints = hand_str_to_ints(hand)
            bucket = get_preflop_bucket(ints)
            # Use max frequency for the bucket
            bucket_freqs[bucket] = max(bucket_freqs[bucket], freq)
        except (ValueError, IndexError) as e:
            print(f"  Warning: could not parse hand '{hand}': {e}")

    return dict(bucket_freqs)


def action_to_fchpa(action: str, size: float) -> int:
    """Convert action string and size to FCHPA action index."""
    action = action.lower()

    if action == "call":
        return CALL
    elif action == "all-in":
        return ALL_IN
    elif action == "raise":
        # Map raise sizes to FCHPA
        if size <= 3.5:
            return HALF_POT  # Opens, small 3bets
        else:
            return POT  # Larger 3bets, 4bets
    else:
        return CALL  # Default


def fchpa_to_str(fchpa: int) -> str:
    """Convert FCHPA to action string for history."""
    return f"a{fchpa}"


def process_spot_recursive(
    spot_name: str,
    spot_data: dict,
    parent_history: str,
    parent_action_fchpa: int,  # The FCHPA action that LED to this spot
    regret_sum: dict,
    strategy_sum: dict,
    initial_regret: float,
    depth: int = 0,
) -> Tuple[int, int]:
    """Process a spot and its children recursively.

    Args:
        spot_name: Name of the spot (e.g., "SB_RFI", "BB_C")
        spot_data: Dict with Action, Size, RangeRaw, and nested children
        parent_history: Action history before this spot
        parent_action_fchpa: The FCHPA action of the PREVIOUS player that leads to this node
        regret_sum: Regret table to update
        strategy_sum: Strategy table to update
        initial_regret: Magnitude of initial regrets
        depth: Recursion depth for logging

    Returns:
        (num_active_buckets, num_fold_buckets)
    """
    action = spot_data.get("Action", "")
    size = spot_data.get("Size", 0) or 0
    range_raw = spot_data.get("RangeRaw", "")

    # Determine player from spot name
    if spot_name.startswith("SB"):
        player = 0
    elif spot_name.startswith("BB"):
        player = 1
    else:
        print(f"  {'  ' * depth}Unknown spot prefix: {spot_name}, skipping")
        return 0, 0

    # Build history: parent_history + parent's action (if any)
    if parent_action_fchpa is not None and parent_history is not None:
        history = parent_history + f",a{parent_action_fchpa}"
    else:
        history = ""  # First action (SB_RFI)

    # Determine this spot's FCHPA action
    this_fchpa = action_to_fchpa(action, size)

    # Parse range
    hand_freqs = parse_range_raw(range_raw)
    bucket_freqs = aggregate_to_buckets(hand_freqs)

    indent = "  " * depth
    print(f"{indent}{spot_name}: player={player}, history='{history}', "
          f"action={action}({this_fchpa}), {len(bucket_freqs)} buckets")

    # Track active buckets
    active_buckets = set()

    # Set regrets for hands in range
    for bucket, freq in bucket_freqs.items():
        if freq < 0.01:
            continue

        active_buckets.add(bucket)
        info_state = f"p{player}:b{bucket}:h{history}"
        key = (player, info_state)

        # Positive regret for this action (scaled by frequency)
        regret_sum[key][this_fchpa] = initial_regret * freq

        # Negative regret for fold
        regret_sum[key][FOLD] = -initial_regret * freq

        # Strategy sum
        strategy_sum[key][this_fchpa] = freq

    # Set fold regrets for hands NOT in range
    fold_count = 0
    for bucket in range(169):
        if bucket not in active_buckets:
            fold_count += 1
            info_state = f"p{player}:b{bucket}:h{history}"
            key = (player, info_state)

            # Positive regret for fold
            regret_sum[key][FOLD] = initial_regret

            # Negative regret for the action
            regret_sum[key][this_fchpa] = -initial_regret

            # Strategy sum
            strategy_sum[key][FOLD] = 1.0

    # Process nested children
    total_active = len(active_buckets)
    total_fold = fold_count

    for child_name, child_data in spot_data.items():
        if isinstance(child_data, dict) and "Action" in child_data:
            # Child's history includes this spot's action
            a, f = process_spot_recursive(
                child_name,
                child_data,
                history,
                this_fchpa,  # This spot's action leads to child
                regret_sum,
                strategy_sum,
                initial_regret,
                depth + 1,
            )
            total_active += a
            total_fold += f

    return total_active, total_fold


def seed_from_json(
    json_path: str,
    output_path: str,
    initial_regret: float = 10000.0,
):
    """Load JSON ranges and create seeded checkpoint."""
    print(f"Loading ranges from {json_path}")

    with open(json_path) as f:
        ranges = json.load(f)

    # Initialize regret tables
    regret_sum = defaultdict(lambda: defaultdict(float))
    strategy_sum = defaultdict(lambda: defaultdict(float))

    # Process each top-level spot
    total_active = 0
    total_fold = 0

    for spot_name, spot_data in ranges.items():
        if isinstance(spot_data, dict) and "Action" in spot_data:
            a, f = process_spot_recursive(
                spot_name,
                spot_data,
                None,  # No parent history
                None,  # No parent action
                regret_sum,
                strategy_sum,
                initial_regret,
            )
            total_active += a
            total_fold += f

    print(f"\nTotal: {total_active} active buckets, {total_fold} fold buckets")
    print(f"Info states created: {len(regret_sum)}")

    # Convert to regular dicts for pickling
    checkpoint = {
        "iterations": 0,
        "samples_collected": 0,
        "regret_sum": {k: dict(v) for k, v in regret_sum.items()},
        "strategy_sum": {k: dict(v) for k, v in strategy_sum.items()},
        "history": [{"seeded_from": json_path}],
        "timestamp": __import__("time").time(),
    }

    # Save checkpoint
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    with open(output_path, "wb") as f:
        pickle.dump(checkpoint, f, protocol=pickle.HIGHEST_PROTOCOL)

    print(f"Saved seeded checkpoint to {output_path}")

    # Also save metadata
    meta_path = output_path.replace(".pkl", ".meta.json")
    with open(meta_path, "w") as f:
        json.dump({
            "seeded_from": json_path,
            "num_info_states": len(regret_sum),
            "initial_regret": initial_regret,
        }, f, indent=2)

    return checkpoint


def main():
    parser = argparse.ArgumentParser(description="Seed HU preflop regrets from GTO ranges")
    parser.add_argument("--input", "-i", required=True, help="Input JSON file with ranges")
    parser.add_argument("--output", "-o", default="models/hu_seed/checkpoint_seed.pkl",
                        help="Output checkpoint path")
    parser.add_argument("--regret", "-r", type=float, default=10000.0,
                        help="Initial regret magnitude")
    args = parser.parse_args()

    seed_from_json(args.input, args.output, args.regret)


if __name__ == "__main__":
    main()
