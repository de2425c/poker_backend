"""
Stats collector for benchmark statistics.

Collects and persists poker statistics per policy for comparison.
"""

import json
import os
from dataclasses import dataclass, field, asdict
from typing import Optional

from ..persistence.models import HandLog, SeatRecord, ActionRecord


@dataclass
class PlayerStats:
    """Accumulated stats for a policy."""
    policy_db: str
    hands_played: int = 0

    # Monetary
    total_won_cents: int = 0

    # VPIP/PFR (preflop)
    hands_vpip: int = 0      # Voluntarily put money in
    hands_pfr: int = 0       # Raised preflop

    # Aggression
    times_bet: int = 0
    times_raised: int = 0
    times_called: int = 0
    times_folded: int = 0

    # Showdown
    hands_saw_flop: int = 0
    hands_to_showdown: int = 0
    showdowns_won: int = 0

    def vpip_pct(self) -> float:
        """Voluntarily Put $ In Pot percentage."""
        return (self.hands_vpip / self.hands_played * 100) if self.hands_played else 0.0

    def pfr_pct(self) -> float:
        """Pre-Flop Raise percentage."""
        return (self.hands_pfr / self.hands_played * 100) if self.hands_played else 0.0

    def aggression_factor(self) -> float:
        """Aggression Factor: (bets + raises) / calls."""
        return ((self.times_bet + self.times_raised) / self.times_called) if self.times_called else 0.0

    def wtsd_pct(self) -> float:
        """Went To ShowDown percentage."""
        return (self.hands_to_showdown / self.hands_saw_flop * 100) if self.hands_saw_flop else 0.0

    def wssd_pct(self) -> float:
        """Won $ at ShowDown percentage."""
        return (self.showdowns_won / self.hands_to_showdown * 100) if self.hands_to_showdown else 0.0

    def bb_per_100(self, bb_cents: int = 200) -> float:
        """Win rate in big blinds per 100 hands."""
        if self.hands_played == 0:
            return 0.0
        return (self.total_won_cents / bb_cents) / self.hands_played * 100

    def total_actions(self) -> int:
        """Total number of actions taken."""
        return self.times_bet + self.times_raised + self.times_called + self.times_folded

    def fold_pct(self) -> float:
        """Fold frequency percentage."""
        total = self.total_actions()
        return (self.times_folded / total * 100) if total else 0.0

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        return {
            "policy_db": self.policy_db,
            "hands_played": self.hands_played,
            "total_won_cents": self.total_won_cents,
            "hands_vpip": self.hands_vpip,
            "hands_pfr": self.hands_pfr,
            "times_bet": self.times_bet,
            "times_raised": self.times_raised,
            "times_called": self.times_called,
            "times_folded": self.times_folded,
            "hands_saw_flop": self.hands_saw_flop,
            "hands_to_showdown": self.hands_to_showdown,
            "showdowns_won": self.showdowns_won,
        }

    @staticmethod
    def from_dict(data: dict) -> "PlayerStats":
        """Create from dictionary."""
        return PlayerStats(
            policy_db=data["policy_db"],
            hands_played=data.get("hands_played", 0),
            total_won_cents=data.get("total_won_cents", 0),
            hands_vpip=data.get("hands_vpip", 0),
            hands_pfr=data.get("hands_pfr", 0),
            times_bet=data.get("times_bet", 0),
            times_raised=data.get("times_raised", 0),
            times_called=data.get("times_called", 0),
            times_folded=data.get("times_folded", 0),
            hands_saw_flop=data.get("hands_saw_flop", 0),
            hands_to_showdown=data.get("hands_to_showdown", 0),
            showdowns_won=data.get("showdowns_won", 0),
        )

    def get_report_dict(self, bb_cents: int = 200) -> dict:
        """Get computed stats for reporting."""
        return {
            "policy_db": self.policy_db,
            "hands_played": self.hands_played,
            "bb_per_100": self.bb_per_100(bb_cents),
            "vpip_pct": self.vpip_pct(),
            "pfr_pct": self.pfr_pct(),
            "aggression_factor": self.aggression_factor(),
            "wtsd_pct": self.wtsd_pct(),
            "wssd_pct": self.wssd_pct(),
            "fold_pct": self.fold_pct(),
        }


class StatsCollector:
    """
    Collects and persists benchmark statistics.

    Stats are accumulated across multiple benchmark runs and persisted to JSON.
    """

    def __init__(self, stats_file: str = "benchmark_stats.json"):
        self.stats_file = stats_file
        self.stats: dict[str, PlayerStats] = {}
        self._load()

    def _load(self) -> None:
        """Load accumulated stats from file."""
        if os.path.exists(self.stats_file):
            try:
                with open(self.stats_file, "r") as f:
                    data = json.load(f)
                    for policy, stats_data in data.items():
                        self.stats[policy] = PlayerStats.from_dict(stats_data)
            except (json.JSONDecodeError, KeyError):
                # Corrupted file - start fresh
                self.stats = {}

    def _save(self) -> None:
        """Persist stats to file."""
        data = {policy: stats.to_dict() for policy, stats in self.stats.items()}
        with open(self.stats_file, "w") as f:
            json.dump(data, f, indent=2)

    def get_or_create_stats(self, policy_db: str) -> PlayerStats:
        """Get stats for a policy, creating if doesn't exist."""
        if policy_db not in self.stats:
            self.stats[policy_db] = PlayerStats(policy_db=policy_db)
        return self.stats[policy_db]

    def record_hand(
        self,
        hand_log: HandLog,
        policy_map: dict[str, str],
    ) -> None:
        """
        Process a completed hand and update stats.

        Args:
            hand_log: Completed hand data
            policy_map: user_id -> policy_db mapping
        """
        # Helper to get attribute from object or dict
        def get_attr(obj, key, default=None):
            if isinstance(obj, dict):
                return obj.get(key, default)
            return getattr(obj, key, default)

        # Build seat_index -> policy_db mapping
        seat_to_policy: dict[int, str] = {}
        seat_to_user: dict[int, str] = {}
        for seat in hand_log.seats:
            seat_idx = get_attr(seat, "seat_index")
            user_id = get_attr(seat, "user_id")
            seat_to_user[seat_idx] = user_id
            if user_id in policy_map:
                seat_to_policy[seat_idx] = policy_map[user_id]

        # Process each player in the hand
        for seat in hand_log.seats:
            seat_idx = get_attr(seat, "seat_index")
            policy = seat_to_policy.get(seat_idx)
            if not policy:
                continue

            stats = self.get_or_create_stats(policy)
            stats.hands_played += 1

            # Track winnings from stack_deltas
            # stack_deltas keys might be strings or ints depending on serialization
            stack_deltas = hand_log.stack_deltas
            delta = stack_deltas.get(seat_idx, 0) or stack_deltas.get(str(seat_idx), 0)
            stats.total_won_cents += delta

            # Analyze actions for this player
            player_actions = [
                a for a in hand_log.actions
                if get_attr(a, "seat") == seat_idx
            ]

            # Track VPIP/PFR (preflop actions)
            preflop_actions = [
                a for a in player_actions
                if get_attr(a, "street") == "preflop"
            ]
            vpip = False
            pfr = False
            for action in preflop_actions:
                action_type = get_attr(action, "action")
                if action_type in ("call", "bet", "raise_to"):
                    vpip = True
                if action_type in ("bet", "raise_to"):
                    pfr = True

            if vpip:
                stats.hands_vpip += 1
            if pfr:
                stats.hands_pfr += 1

            # Count action types across all streets
            for action in player_actions:
                action_type = get_attr(action, "action")
                if action_type == "fold":
                    stats.times_folded += 1
                elif action_type == "check":
                    pass  # Don't count checks in aggression
                elif action_type == "call":
                    stats.times_called += 1
                elif action_type == "bet":
                    stats.times_bet += 1
                elif action_type == "raise_to":
                    stats.times_raised += 1

            # Track showdown stats
            saw_flop = any(
                get_attr(a, "street") != "preflop"
                for a in player_actions
            )
            if saw_flop:
                stats.hands_saw_flop += 1

            # Check if player went to showdown
            went_to_showdown = any(
                get_attr(w, "seat") == seat_idx
                for w in hand_log.winners
                if get_attr(w, "shown_cards") is not None
            )
            if went_to_showdown:
                stats.hands_to_showdown += 1

            # Check if won at showdown
            won_at_showdown = any(
                get_attr(w, "seat") == seat_idx and get_attr(w, "amount_won", 0) > 0
                for w in hand_log.winners
                if get_attr(w, "shown_cards") is not None
            )
            if won_at_showdown:
                stats.showdowns_won += 1

        # Persist after each hand
        self._save()

    def get_report(self, bb_cents: int = 200) -> dict:
        """Generate comparison report across policies."""
        report = {}
        for policy, stats in self.stats.items():
            report[policy] = stats.get_report_dict(bb_cents)
        return report

    def clear(self) -> None:
        """Clear all accumulated stats."""
        self.stats = {}
        if os.path.exists(self.stats_file):
            os.remove(self.stats_file)

    def get_head_to_head(self, policy_a: str, policy_b: str, bb_cents: int = 200) -> Optional[dict]:
        """
        Get head-to-head comparison between two policies.

        Returns:
            Dict with comparison stats or None if either policy not found.
        """
        if policy_a not in self.stats or policy_b not in self.stats:
            return None

        stats_a = self.stats[policy_a]
        stats_b = self.stats[policy_b]

        bb_diff = stats_a.bb_per_100(bb_cents) - stats_b.bb_per_100(bb_cents)
        winner = policy_a if bb_diff > 0 else policy_b

        return {
            "policy_a": policy_a,
            "policy_b": policy_b,
            "bb_per_100_diff": bb_diff,
            "winner": winner,
            "margin_bb_per_100": abs(bb_diff),
        }
