"""
Benchmark module for headless bot comparison.

Runs bot-only tables to compare different policy DBs and collect statistics.
"""

from .stats_collector import PlayerStats, StatsCollector
from .runner import BenchmarkRunner, BenchmarkConfig, BenchmarkStatus

__all__ = [
    "PlayerStats",
    "StatsCollector",
    "BenchmarkRunner",
    "BenchmarkConfig",
    "BenchmarkStatus",
]
