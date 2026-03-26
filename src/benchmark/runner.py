"""
Benchmark runner for headless bot comparison.

Orchestrates bot-only tables and collects statistics.
"""

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional, TYPE_CHECKING

from .stats_collector import StatsCollector, PlayerStats

if TYPE_CHECKING:
    from ..manager import TableManager
    from ..persistence import HandLogger


class BenchmarkStatus(str, Enum):
    """Status of a benchmark run."""
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class BenchmarkConfig:
    """Configuration for a benchmark run."""
    policies: list[str]           # List of policy DB paths
    num_hands: int = 100          # Number of hands to play
    stake_id: str = "nlh_1_2"     # Stake level
    num_bots: int = 6             # Number of bots (max 6)


@dataclass
class BenchmarkRun:
    """State of a benchmark run."""
    benchmark_id: str
    config: BenchmarkConfig
    status: BenchmarkStatus = BenchmarkStatus.PENDING
    table_id: Optional[str] = None
    hands_played: int = 0
    started_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None
    error: Optional[str] = None
    bot_user_ids: list[str] = field(default_factory=list)
    # user_id -> policy_db mapping
    policy_map: dict[str, str] = field(default_factory=dict)


class BenchmarkRunner:
    """
    Orchestrates benchmark runs with bot-only tables.

    Creates tables with bots using different policies and collects
    statistics for comparison.
    """

    def __init__(
        self,
        manager: "TableManager",
        hand_logger: "HandLogger",
        stats_file: str = "benchmark_stats.json",
    ):
        self._manager = manager
        self._hand_logger = hand_logger
        self._stats = StatsCollector(stats_file)
        self._runs: dict[str, BenchmarkRun] = {}
        self._active_run: Optional[str] = None

    @property
    def stats_collector(self) -> StatsCollector:
        """Access to stats collector for reporting."""
        return self._stats

    async def start_benchmark(self, config: BenchmarkConfig) -> str:
        """
        Start a new benchmark run.

        Args:
            config: Benchmark configuration

        Returns:
            benchmark_id for status polling
        """
        if self._active_run:
            raise ValueError("A benchmark is already running")

        benchmark_id = f"bench_{uuid.uuid4().hex[:8]}"
        run = BenchmarkRun(
            benchmark_id=benchmark_id,
            config=config,
        )
        self._runs[benchmark_id] = run
        self._active_run = benchmark_id

        # Start benchmark in background
        asyncio.create_task(self._run_benchmark(benchmark_id))

        return benchmark_id

    def get_status(self, benchmark_id: str) -> Optional[dict]:
        """
        Get status of a benchmark run.

        Returns:
            Status dict or None if not found
        """
        run = self._runs.get(benchmark_id)
        if not run:
            return None

        result = {
            "benchmark_id": run.benchmark_id,
            "status": run.status.value,
            "hands_played": run.hands_played,
            "target_hands": run.config.num_hands,
            "policies": run.config.policies,
        }

        if run.status == BenchmarkStatus.COMPLETED:
            # Include final stats
            result["stats_by_policy"] = self._stats.get_report(
                bb_cents=self._get_bb_cents(run.config.stake_id)
            )
        elif run.status == BenchmarkStatus.FAILED:
            result["error"] = run.error

        return result

    async def _run_benchmark(self, benchmark_id: str) -> None:
        """Run the benchmark (background task)."""
        run = self._runs[benchmark_id]
        run.status = BenchmarkStatus.RUNNING
        run.started_at = datetime.utcnow()

        try:
            # Create bot-only table
            table_id, bot_user_ids, policy_map = await self._create_benchmark_table(
                run.config
            )
            run.table_id = table_id
            run.bot_user_ids = bot_user_ids
            run.policy_map = policy_map

            print(f"[BENCHMARK] Created table {table_id} with {len(bot_user_ids)} bots")
            print(f"[BENCHMARK] Policy map: {policy_map}")

            # Wait for bots to connect
            await self._wait_for_bots(table_id, bot_user_ids, timeout=30.0)

            # Track hands played
            initial_hands = self._count_hands_logged()

            # Wait for hands to complete
            while run.hands_played < run.config.num_hands:
                await asyncio.sleep(1.0)

                # Check if table still exists
                if table_id not in self._manager._tables:
                    raise RuntimeError("Table was destroyed unexpectedly")

                # Count hands from hand logger
                current_hands = self._count_hands_logged()
                new_hands = current_hands - initial_hands
                run.hands_played = new_hands

                # Process any new hand logs
                await self._process_new_hands(run)

                print(f"[BENCHMARK] Progress: {run.hands_played}/{run.config.num_hands}")

            # Cleanup
            await self._cleanup_benchmark_table(run)

            run.status = BenchmarkStatus.COMPLETED
            run.ended_at = datetime.utcnow()
            print(f"[BENCHMARK] Completed {run.hands_played} hands")

        except Exception as e:
            run.status = BenchmarkStatus.FAILED
            run.error = str(e)
            run.ended_at = datetime.utcnow()
            print(f"[BENCHMARK] Failed: {e}")

            # Try to cleanup
            try:
                await self._cleanup_benchmark_table(run)
            except Exception:
                pass

        finally:
            self._active_run = None

    async def _create_benchmark_table(
        self,
        config: BenchmarkConfig,
    ) -> tuple[str, list[str], dict[str, str]]:
        """
        Create a bot-only table for benchmarking.

        Returns:
            (table_id, bot_user_ids, policy_map)
        """
        # Create table
        table_id = self._manager.create_table(config.stake_id)

        # Determine bot count (max 6)
        num_bots = min(config.num_bots, 6)

        # Assign policies round-robin
        bot_user_ids: list[str] = []
        policy_map: dict[str, str] = {}

        for i in range(num_bots):
            policy = config.policies[i % len(config.policies)]
            bot_id = f"user_bench_{table_id}_{i}"
            bot_user_ids.append(bot_id)
            policy_map[bot_id] = policy

        # Spawn bots with their assigned policies
        for i, bot_id in enumerate(bot_user_ids):
            policy = policy_map[bot_id]
            bot_name = f"Bot{i + 1}"
            await self._spawn_benchmark_bot(
                bot_id=bot_id,
                bot_name=bot_name,
                table_id=table_id,
                stake_id=config.stake_id,
                policy_db=policy,
            )

        return table_id, bot_user_ids, policy_map

    async def _spawn_benchmark_bot(
        self,
        bot_id: str,
        bot_name: str,
        table_id: str,
        stake_id: str,
        policy_db: str,
    ) -> Optional[asyncio.subprocess.Process]:
        """Spawn a benchmark bot with a specific policy."""
        import os

        # Get config from manager
        openbot_dir = self._manager._openbot_dir
        server_url = self._manager._server_url

        # Use venv python if available
        venv_python = os.path.join(openbot_dir, "venv", "bin", "python")
        python_cmd = venv_python if os.path.exists(venv_python) else "python"

        # Get buy-in from stake config
        stake_config = self._manager._stake_configs.get(stake_id)
        buy_in = stake_config.max_buy_in_cents // 2 if stake_config else 20000

        cmd = [
            python_cmd, "-m", "src.serving.openbot_client",
            "--server", server_url,
            "--policy", policy_db,  # Use the specific policy for this bot
            "--user-id", bot_id,
            "--display-name", bot_name,
            "--table-id", table_id,
            "--stake", stake_id,
            "--buy-in", str(buy_in),
        ]

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=openbot_dir,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            print(f"[BENCHMARK] Spawned {bot_id} with policy={policy_db} (pid={proc.pid})")
            return proc
        except Exception as e:
            print(f"[BENCHMARK] Failed to spawn {bot_id}: {e}")
            return None

    async def _wait_for_bots(
        self,
        table_id: str,
        bot_ids: list[str],
        timeout: float = 30.0,
    ) -> bool:
        """Wait for bots to connect and be seated."""
        import time
        start = time.time()
        expected_count = len(bot_ids)

        while time.time() - start < timeout:
            seated_count = sum(
                1 for bot_id in bot_ids
                if self._manager.get_table_for_user(bot_id) == table_id
            )

            if seated_count >= expected_count:
                print(f"[BENCHMARK] All {expected_count} bots seated")
                return True

            await asyncio.sleep(0.5)

        print(f"[BENCHMARK] Timeout: {seated_count}/{expected_count} bots seated")
        return False

    async def _process_new_hands(self, run: BenchmarkRun) -> None:
        """Process new hand logs and update stats."""
        # Get recent hand logs for this table
        hand_logs = self._get_table_hand_logs(run.table_id)

        for hand_log in hand_logs:
            # Only process once (check if we've seen this hand)
            if not hasattr(self, '_processed_hands'):
                self._processed_hands = set()

            # Handle both object and dict access
            hand_id = hand_log.hand_id if hasattr(hand_log, 'hand_id') else hand_log.get('hand_id')

            if hand_id in self._processed_hands:
                continue

            self._processed_hands.add(hand_id)
            self._stats.record_hand(hand_log, run.policy_map)

    def _get_table_hand_logs(self, table_id: str) -> list:
        """Get hand logs for a specific table."""
        all_logs = self._hand_logger._firestore.get_all_hand_logs()
        result = []
        for log in all_logs:
            # Get table_id from dict or object
            log_table_id = log.get("table_id") if isinstance(log, dict) else getattr(log, "table_id", None)
            if log_table_id == table_id:
                # Wrap dict in a simple object for attribute access
                if isinstance(log, dict):
                    result.append(DictWrapper(log))
                else:
                    result.append(log)
        return result


    def _count_hands_logged(self) -> int:
        """Count total hands in hand logger."""
        return len(self._hand_logger._firestore.get_all_hand_logs())

    async def _cleanup_benchmark_table(self, run: BenchmarkRun) -> None:
        """Clean up benchmark table and bots."""
        if not run.table_id:
            return

        print(f"[BENCHMARK] Cleaning up table {run.table_id}")

        # Remove bots from table tracking
        for bot_id in run.bot_user_ids:
            try:
                if bot_id in self._manager._user_tables:
                    del self._manager._user_tables[bot_id]
            except Exception:
                pass

        # Stop the table runner
        runner = self._manager._tables.get(run.table_id)
        if runner:
            await runner.stop()
            del self._manager._tables[run.table_id]

    def _get_bb_cents(self, stake_id: str) -> int:
        """Get big blind in cents for a stake level."""
        config = self._manager._stake_configs.get(stake_id)
        return config.big_blind_cents if config else 200


class DictWrapper:
    """Wrapper to access dict as object attributes."""
    def __init__(self, data: dict):
        self._data = data

    def __getattr__(self, name):
        if name.startswith('_'):
            return super().__getattribute__(name)
        value = self._data.get(name)
        # Wrap nested dicts and lists of dicts
        if isinstance(value, dict):
            return DictWrapper(value)
        if isinstance(value, list):
            return [DictWrapper(v) if isinstance(v, dict) else v for v in value]
        return value

    def get(self, key, default=None):
        return self._data.get(key, default)
