"""
Structured logging configuration for Cloud Run.

Outputs JSON logs compatible with Google Cloud Logging.
"""

import json
import logging
import sys
from datetime import datetime, timezone
from typing import Optional


class CloudRunFormatter(logging.Formatter):
    """
    JSON formatter compatible with Google Cloud Logging.

    Follows the structured logging format expected by Cloud Logging:
    https://cloud.google.com/logging/docs/structured-logging
    """

    def format(self, record: logging.LogRecord) -> str:
        """Format log record as JSON for Cloud Logging."""
        log_obj = {
            "severity": record.levelname,
            "message": record.getMessage(),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "logging.googleapis.com/sourceLocation": {
                "file": record.pathname,
                "line": record.lineno,
                "function": record.funcName,
            },
        }

        # Add optional context fields if present
        if hasattr(record, "user_id") and record.user_id:
            log_obj["user_id"] = record.user_id
        if hasattr(record, "table_id") and record.table_id:
            log_obj["table_id"] = record.table_id
        if hasattr(record, "hand_id") and record.hand_id:
            log_obj["hand_id"] = record.hand_id

        # Add exception info if present
        if record.exc_info:
            log_obj["exception"] = self.formatException(record.exc_info)

        return json.dumps(log_obj)


class PokerLogger:
    """
    Logger wrapper with context support.

    Usage:
        logger = get_logger()
        logger.info("Player joined", user_id="user_123", table_id="tbl_abc")
    """

    def __init__(self, logger: logging.Logger):
        self._logger = logger

    def _log(
        self,
        level: int,
        msg: str,
        user_id: Optional[str] = None,
        table_id: Optional[str] = None,
        hand_id: Optional[str] = None,
        **kwargs,
    ) -> None:
        """Log with optional context fields."""
        extra = {
            "user_id": user_id,
            "table_id": table_id,
            "hand_id": hand_id,
        }
        self._logger.log(level, msg, extra=extra, **kwargs)

    def debug(self, msg: str, **kwargs) -> None:
        self._log(logging.DEBUG, msg, **kwargs)

    def info(self, msg: str, **kwargs) -> None:
        self._log(logging.INFO, msg, **kwargs)

    def warning(self, msg: str, **kwargs) -> None:
        self._log(logging.WARNING, msg, **kwargs)

    def error(self, msg: str, **kwargs) -> None:
        self._log(logging.ERROR, msg, **kwargs)

    def exception(self, msg: str, **kwargs) -> None:
        self._log(logging.ERROR, msg, exc_info=True, **kwargs)


def setup_logging() -> PokerLogger:
    """
    Configure structured logging for Cloud Run.

    Returns a PokerLogger instance for use throughout the application.
    """
    # Create handler writing to stdout (Cloud Run captures stdout)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(CloudRunFormatter())

    # Configure root poker logger
    logger = logging.getLogger("poker")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)

    # Prevent duplicate logs
    logger.propagate = False

    return PokerLogger(logger)


# Global logger instance
logger = setup_logging()
