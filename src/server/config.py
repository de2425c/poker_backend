"""
Environment-based configuration for the poker server.

Supports Cloud Run deployment with environment variables.
"""

import os
from dataclasses import dataclass

# Load .env file if present (for local/VM deployment)
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # python-dotenv not installed, rely on system env vars


@dataclass
class ServerConfig:
    """Server configuration loaded from environment variables."""

    # Server settings
    port: int
    host: str

    # Auth settings
    auth_enabled: bool

    # Game settings
    action_timeout_seconds: int

    # Default stake configuration
    default_stake_id: str
    default_small_blind_cents: int
    default_big_blind_cents: int
    default_min_buy_in_cents: int
    default_max_buy_in_cents: int

    @classmethod
    def from_env(cls) -> "ServerConfig":
        """Load configuration from environment variables."""
        return cls(
            # Server
            port=int(os.getenv("PORT", "8080")),
            host=os.getenv("HOST", "0.0.0.0"),

            # Auth (disabled by default for local dev)
            auth_enabled=os.getenv("AUTH_ENABLED", "false").lower() == "true",

            # Game settings
            action_timeout_seconds=int(os.getenv("ACTION_TIMEOUT_SECONDS", "30")),

            # Stakes
            default_stake_id=os.getenv("DEFAULT_STAKE_ID", "nlh_1_2"),
            default_small_blind_cents=int(os.getenv("SMALL_BLIND_CENTS", "100")),
            default_big_blind_cents=int(os.getenv("BIG_BLIND_CENTS", "200")),
            default_min_buy_in_cents=int(os.getenv("MIN_BUY_IN_CENTS", "4000")),
            default_max_buy_in_cents=int(os.getenv("MAX_BUY_IN_CENTS", "40000")),
        )


# Global config instance - loaded once at module import
config = ServerConfig.from_env()
