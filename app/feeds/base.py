"""Feed abstractions.

Two separate concerns, deliberately not merged:

  HistoryProvider — daily OHLCV bars for pattern work. A 15-minute delay is
                    irrelevant to a daily-bar pattern, so this is always the
                    free Yahoo source.

  LiveFeed        — the intraday price tape that drives the momentum trigger.
                    Here latency is the whole game, so this is the pluggable
                    part: poll Yahoo for free (delayed), or hold a broker
                    WebSocket open for real ticks.
"""

from __future__ import annotations

import abc
from datetime import datetime

import pandas as pd

from ..config import IST


class HistoryProvider(abc.ABC):
    @abc.abstractmethod
    async def daily(self, symbols: list[str], lookback_days: int = 400) -> dict[str, pd.DataFrame]:
        """Daily OHLCV per symbol. Missing/failed symbols are simply absent."""

    @abc.abstractmethod
    async def benchmark(self, symbol: str, lookback_days: int = 400) -> pd.Series | None:
        """Close series for the index used by the relative-strength gate."""


class LiveFeed(abc.ABC):
    #: True when prices arrive tick-by-tick; False when they are polled/delayed.
    live: bool = False
    #: Human-readable delay disclosure shown in the dashboard.
    delay_note: str = ""
    name: str = "base"

    @abc.abstractmethod
    async def start(self) -> None: ...

    @abc.abstractmethod
    async def stop(self) -> None: ...

    @abc.abstractmethod
    async def subscribe(self, symbols: list[str]) -> None:
        """Idempotent. Called whenever the watchlist changes."""

    @abc.abstractmethod
    async def latest(self, symbols: list[str]) -> dict[str, tuple[float, datetime]]:
        """Most recent (price, timestamp) per symbol. Symbols with no price
        yet are omitted rather than reported as zero."""

    @property
    def status(self) -> dict:
        return {"name": self.name, "live": self.live, "delay_note": self.delay_note}


def now_ist() -> datetime:
    return datetime.now(IST)
