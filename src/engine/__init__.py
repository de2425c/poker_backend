"""
Poker game engine module.

Wraps PokerKit to provide:
- Table lifecycle management
- Action validation and execution
- Snapshot and event generation in our protocol schema
"""

from .config import TableConfig
from .adapter import PokerKitAdapter, AllowedActions
from .table import PokerTableEngine

__all__ = [
    "TableConfig",
    "PokerKitAdapter",
    "AllowedActions",
    "PokerTableEngine",
]
