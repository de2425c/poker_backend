#!/usr/bin/env python3
"""
Headless benchmark runner for poker bots.

Runs bot-only tables to compare different policy DBs and collect statistics.
Stats accumulate over multiple runs for statistical significance.

Usage:
    python benchmark.py --policies policy_90m.db,policy_192m.db --hands 1000
    python benchmark.py --policies policy_90m.db --hands 100
    python benchmark.py --report  # Show accumulated stats
    python benchmark.py --clear   # Clear accumulated stats

Examples:
    # Run 1000 hands comparing two policies
    python benchmark.py --policies models/policy_90m.db,models/policy_192m.db --hands 1000

    # Run more hands (stats accumulate across runs)
    python benchmark.py --policies models/policy_90m.db,models/policy_192m.db --hands 5000

    # View accumulated stats
    python benchmark.py --report

    # Compare 3 policies
    python benchmark.py --policies policy_a.db,policy_b.db,policy_c.db --hands 1000
"""

import argparse
import asyncio
import sys
import time
from typing import Optional

try:
    import httpx
except ImportError:
    print("Error: httpx is required. Install with: pip install httpx")
    sys.exit(1)


DEFAULT_SERVER = "http://localhost:8000"
POLL_INTERVAL = 2.0  # seconds


async def run_benchmark(
    policies: list[str],
    num_hands: int,
    server_url: str,
    stake_id: str = "nlh_1_2",
    num_bots: int = 6,
) -> dict:
    """
    Start benchmark and wait for completion.

    Args:
        policies: List of policy DB paths
        num_hands: Number of hands to play
        server_url: Server base URL
        stake_id: Stake level identifier
        num_bots: Number of bots (max 6)

    Returns:
        Final stats dictionary
    """
    async with httpx.AsyncClient(timeout=30.0) as client:
        # Start benchmark
        print(f"\nStarting benchmark...")
        print(f"  Policies: {', '.join(policies)}")
        print(f"  Hands: {num_hands}")
        print(f"  Stakes: {stake_id}")
        print(f"  Bots: {num_bots}")
        print()

        try:
            resp = await client.post(
                f"{server_url}/benchmark/start",
                json={
                    "policies": policies,
                    "num_hands": num_hands,
                    "stake_id": stake_id,
                    "num_bots": num_bots,
                }
            )
            resp.raise_for_status()
        except httpx.ConnectError:
            print(f"Error: Could not connect to server at {server_url}")
            print("Make sure the poker server is running:")
            print("  cd poker_backend && ./venv/bin/uvicorn src.server.app:app --host 0.0.0.0 --port 8000")
            sys.exit(1)
        except httpx.HTTPStatusError as e:
            print(f"Error starting benchmark: {e.response.text}")
            sys.exit(1)

        data = resp.json()
        benchmark_id = data["benchmark_id"]
        print(f"Benchmark started: {benchmark_id}")
        print()

        # Poll for completion
        start_time = time.time()
        last_hands = 0

        while True:
            try:
                status_resp = await client.get(f"{server_url}/benchmark/status/{benchmark_id}")
                status_resp.raise_for_status()
                status = status_resp.json()
            except Exception as e:
                print(f"\nError polling status: {e}")
                await asyncio.sleep(POLL_INTERVAL)
                continue

            hands_played = status.get("hands_played", 0)
            target = status.get("target_hands", num_hands)
            elapsed = time.time() - start_time

            # Calculate rate
            if elapsed > 0 and hands_played > 0:
                rate = hands_played / elapsed * 60  # hands per minute
                eta = (target - hands_played) / (hands_played / elapsed) if hands_played > 0 else 0
                rate_str = f" ({rate:.1f} hands/min, ETA: {int(eta)}s)"
            else:
                rate_str = ""

            # Progress update (only when hands change)
            if hands_played != last_hands:
                pct = hands_played / target * 100 if target > 0 else 0
                bar_width = 30
                filled = int(bar_width * hands_played / target) if target > 0 else 0
                bar = "=" * filled + "-" * (bar_width - filled)
                print(f"\r[{bar}] {hands_played}/{target} ({pct:.1f}%){rate_str}    ", end="", flush=True)
                last_hands = hands_played

            if status["status"] == "completed":
                print("\n")
                return status.get("stats_by_policy", {})
            elif status["status"] == "failed":
                print(f"\n\nBenchmark failed: {status.get('error', 'Unknown error')}")
                sys.exit(1)

            await asyncio.sleep(POLL_INTERVAL)


async def get_report(server_url: str) -> dict:
    """Fetch accumulated stats from server."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            resp = await client.get(f"{server_url}/benchmark/report")
            resp.raise_for_status()
            return resp.json()
        except httpx.ConnectError:
            print(f"Error: Could not connect to server at {server_url}")
            sys.exit(1)
        except httpx.HTTPStatusError as e:
            print(f"Error fetching report: {e.response.text}")
            sys.exit(1)


async def clear_stats(server_url: str) -> None:
    """Clear accumulated stats on server."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            resp = await client.delete(f"{server_url}/benchmark/stats")
            resp.raise_for_status()
            print("Benchmark statistics cleared.")
        except httpx.ConnectError:
            print(f"Error: Could not connect to server at {server_url}")
            sys.exit(1)
        except httpx.HTTPStatusError as e:
            print(f"Error clearing stats: {e.response.text}")
            sys.exit(1)


def show_report(stats: dict, policies: Optional[list[str]] = None) -> None:
    """Pretty print benchmark results."""
    if not stats:
        print("No benchmark data available.")
        print("Run a benchmark first:")
        print("  python benchmark.py --policies policy_a.db,policy_b.db --hands 1000")
        return

    print()
    print("=" * 60)
    print("BENCHMARK RESULTS")
    print("=" * 60)

    # Sort by bb/100 descending
    sorted_stats = sorted(
        stats.items(),
        key=lambda x: x[1].get("bb_per_100", 0),
        reverse=True
    )

    for policy, s in sorted_stats:
        print(f"\n{policy}")
        print("-" * len(policy))
        print(f"  Hands:    {s.get('hands_played', 0):,}")
        print(f"  bb/100:   {s.get('bb_per_100', 0):+.2f}")
        print(f"  VPIP:     {s.get('vpip_pct', 0):.1f}%")
        print(f"  PFR:      {s.get('pfr_pct', 0):.1f}%")
        print(f"  AF:       {s.get('aggression_factor', 0):.2f}")
        print(f"  WTSD:     {s.get('wtsd_pct', 0):.1f}%")
        print(f"  W$SD:     {s.get('wssd_pct', 0):.1f}%")
        print(f"  Fold:     {s.get('fold_pct', 0):.1f}%")

    # Head-to-head comparison if exactly 2 policies
    if len(sorted_stats) == 2:
        p1_name, p1_stats = sorted_stats[0]
        p2_name, p2_stats = sorted_stats[1]
        diff = p1_stats.get("bb_per_100", 0) - p2_stats.get("bb_per_100", 0)

        print()
        print("-" * 60)
        print(f"Head-to-Head: {p1_name}")
        print(f"  beats {p2_name}")
        print(f"  by {diff:+.2f} bb/100")

    print()
    print("=" * 60)
    print()

    # Stat explanations
    print("Stat Definitions:")
    print("  bb/100  - Win rate in big blinds per 100 hands")
    print("  VPIP    - Voluntarily Put $ In Pot (preflop)")
    print("  PFR     - Pre-Flop Raise percentage")
    print("  AF      - Aggression Factor: (bets + raises) / calls")
    print("  WTSD    - Went To ShowDown (of hands that saw flop)")
    print("  W$SD    - Won $ at ShowDown")
    print("  Fold    - Fold frequency")
    print()


def main():
    parser = argparse.ArgumentParser(
        description="Headless benchmark runner for poker bots",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s --policies policy_90m.db,policy_192m.db --hands 1000
  %(prog)s --report
  %(prog)s --clear
        """
    )
    parser.add_argument(
        "--policies",
        type=str,
        help="Comma-separated policy DB paths (e.g., policy_a.db,policy_b.db)"
    )
    parser.add_argument(
        "--hands",
        type=int,
        default=100,
        help="Number of hands to play (default: 100)"
    )
    parser.add_argument(
        "--server",
        type=str,
        default=DEFAULT_SERVER,
        help=f"Server URL (default: {DEFAULT_SERVER})"
    )
    parser.add_argument(
        "--stake",
        type=str,
        default="nlh_1_2",
        help="Stake level (default: nlh_1_2)"
    )
    parser.add_argument(
        "--bots",
        type=int,
        default=6,
        help="Number of bots (default: 6, max: 6)"
    )
    parser.add_argument(
        "--report",
        action="store_true",
        help="Show accumulated stats from previous runs"
    )
    parser.add_argument(
        "--clear",
        action="store_true",
        help="Clear accumulated statistics"
    )

    args = parser.parse_args()

    if args.clear:
        asyncio.run(clear_stats(args.server))
        return

    if args.report:
        report = asyncio.run(get_report(args.server))
        show_report(report.get("stats_by_policy", {}))
        return

    if not args.policies:
        parser.error("--policies is required (or use --report/--clear)")

    policies = [p.strip() for p in args.policies.split(",")]
    if len(policies) < 1:
        parser.error("At least one policy is required")

    stats = asyncio.run(run_benchmark(
        policies=policies,
        num_hands=args.hands,
        server_url=args.server,
        stake_id=args.stake,
        num_bots=min(args.bots, 6),
    ))

    show_report(stats, policies)


if __name__ == "__main__":
    main()
