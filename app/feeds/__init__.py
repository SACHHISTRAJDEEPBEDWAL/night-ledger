"""Feed selection. One switch in the environment picks the price source."""

from __future__ import annotations

import logging

from ..config import Settings
from .base import HistoryProvider, LiveFeed
from .yahoo import YahooHistory, YahooPollFeed

log = logging.getLogger(__name__)

__all__ = [
    "HistoryProvider",
    "LiveFeed",
    "YahooHistory",
    "YahooPollFeed",
    "build_history",
    "build_live_feed",
]


def build_history(settings: Settings) -> HistoryProvider:
    # Daily bars always come from Yahoo: they are free, deep, and a 15-minute
    # lag is meaningless on a daily-bar pattern.
    return YahooHistory(cache_minutes=settings.history_cache_minutes)


def build_live_feed(settings: Settings) -> LiveFeed:
    if settings.feed == "angelone":
        missing = [
            k
            for k, v in {
                "ANGEL_API_KEY": settings.angel_api_key,
                "ANGEL_CLIENT_CODE": settings.angel_client_code,
                "ANGEL_PASSWORD": settings.angel_password,
                "ANGEL_TOTP_SECRET": settings.angel_totp_secret,
            }.items()
            if not v
        ]
        if missing:
            log.warning(
                "FEED=angelone but %s not set — falling back to delayed Yahoo polling",
                ", ".join(missing),
            )
            return YahooPollFeed()
        from .angelone import AngelOneFeed  # noqa: PLC0415

        return AngelOneFeed(settings)
    return YahooPollFeed()
