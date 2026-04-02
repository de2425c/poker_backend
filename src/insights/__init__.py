"""AI Poker Insights - Generate GTO explanations using Claude."""

from .schema import InsightRequest, InsightResponse

__all__ = ["InsightRequest", "InsightResponse", "InsightGenerator"]


def __getattr__(name):
    """Lazy import InsightGenerator to avoid importing anthropic at package import time."""
    if name == "InsightGenerator":
        from .generator import InsightGenerator
        return InsightGenerator
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
