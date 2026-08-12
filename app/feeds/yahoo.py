"""Yahoo Finance adapter — the zero-credential default.

Daily bars are genuinely fine here: a VCP is a daily-bar pattern and a 15
minute lag on yesterday's close means nothing. The intraday poller is the
compromise — it works out of the box with no broker account, but Yahoo's
Indian intraday data lags the exchange, so the momentum trigger fires late.
Swap in the Angel One feed when that matters.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime

import pandas as pd

from ..config import IST
from .base import HistoryProvider, LiveFeed

log = logging.getLogger(__name__)

_YF_COLUMNS = {"open": "open", "high": "high", "low": "low", "close": "close", "volume": "volume"}


def _import_yf():
    try:
        import yfinance as yf  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover - environment issue
        raise RuntimeError(
            "yfinance is not installed. `pip install -r requirements.txt`"
        ) from exc
    return yf


def _slice(data: pd.DataFrame, symbol: str, many: bool) -> pd.DataFrame | None:
    """yfinance hands back flat columns for one ticker and a MultiIndex for
    several. Normalise both into a plain lower-cased OHLCV frame."""
    if data is None or data.empty:
        return None
    try:
        frame = data[symbol] if many else data
    except KeyError:
        return None
    if isinstance(frame.columns, pd.MultiIndex):
        frame = frame.droplevel(0, axis=1)
    frame = frame.rename(columns={c: str(c).lower() for c in frame.columns})
    keep = [c for c in _YF_COLUMNS if c in frame.columns]
    if len(keep) < 5:
        return None
    out = frame[keep].dropna(how="all")
    return out if not out.empty else None


class YahooHistory(HistoryProvider):
    def __init__(self, cache_minutes: int = 60) -> None:
        self.ttl = cache_minutes * 60
        self._cache: dict[str, tuple[float, pd.DataFrame]] = {}
        self._bench: dict[str, tuple[float, pd.Series]] = {}
        self._lock = asyncio.Lock()

    def _fresh(self, symbol: str) -> pd.DataFrame | None:
        hit = self._cache.get(symbol)
        if hit and time.time() - hit[0] < self.ttl:
            return hit[1]
        return None

    async def daily(
        self, symbols: list[str], lookback_days: int = 400
    ) -> dict[str, pd.DataFrame]:
        out: dict[str, pd.DataFrame] = {}
        stale = []
        for s in symbols:
            cached = self._fresh(s)
            if cached is not None:
                out[s] = cached
            else:
                stale.append(s)
        if not stale:
            return out

        async with self._lock:
            # Re-check: another waiter may have filled the cache meanwhile.
            stale = [s for s in stale if self._fresh(s) is None]
            for s in symbols:
                if s not in out and (c := self._fresh(s)) is not None:
                    out[s] = c
            if not stale:
                return out

            period = f"{max(lookback_days, 250)}d"
            fetched = await asyncio.to_thread(self._download_daily, stale, period)
            now = time.time()
            for sym, frame in fetched.items():
                self._cache[sym] = (now, frame)
                out[sym] = frame
        return out

    def _download_daily(self, symbols: list[str], period: str) -> dict[str, pd.DataFrame]:
        yf = _import_yf()
        result: dict[str, pd.DataFrame] = {}
        # Chunked so one bad ticker cannot poison a 200-name request.
        for chunk in (symbols[i : i + 40] for i in range(0, len(symbols), 40)):
            try:
                data = yf.download(
                    chunk,
                    period=period,
                    interval="1d",
                    group_by="ticker",
                    auto_adjust=True,
                    progress=False,
                    threads=True,
                )
            except Exception as exc:  # noqa: BLE001 - network layer, log and move on
                log.warning("daily download failed for %s: %s", chunk, exc)
                continue
            many = len(chunk) > 1
            for sym in chunk:
                frame = _slice(data, sym, many)
                if frame is not None and len(frame) > 60:
                    result[sym] = frame
        return result

    async def benchmark(
        self, symbol: str, lookback_days: int = 400
    ) -> pd.Series | None:
        hit = self._bench.get(symbol)
        if hit and time.time() - hit[0] < self.ttl:
            return hit[1]
        frames = await asyncio.to_thread(
            self._download_daily, [symbol], f"{max(lookback_days, 250)}d"
        )
        frame = frames.get(symbol)
        if frame is None:
            # ^CRSLDX (Nifty 500) is patchy on Yahoo; ^NSEI always resolves.
            if symbol != "^NSEI":
                return await self.benchmark("^NSEI", lookback_days)
            return None
        series = frame["close"]
        self._bench[symbol] = (time.time(), series)
        return series


class YahooPollFeed(LiveFeed):
    """Polls 1-minute bars for the whole watchlist in one request per cycle."""

    live = False
    name = "yahoo"
    delay_note = "Yahoo intraday data for Indian equities lags the exchange by ~15 min"

    def __init__(self) -> None:
        self._symbols: list[str] = []
        self._last: dict[str, tuple[float, datetime]] = {}
        self._running = False

    async def start(self) -> None:
        self._running = True

    async def stop(self) -> None:
        self._running = False

    async def subscribe(self, symbols: list[str]) -> None:
        self._symbols = list(symbols)
        for gone in set(self._last) - set(symbols):
            self._last.pop(gone, None)

    async def latest(self, symbols: list[str]) -> dict[str, tuple[float, datetime]]:
        if not symbols:
            return {}
        fresh = await asyncio.to_thread(self._poll, list(symbols))
        self._last.update(fresh)
        return {s: self._last[s] for s in symbols if s in self._last}

    def _poll(self, symbols: list[str]) -> dict[str, tuple[float, datetime]]:
        yf = _import_yf()
        out: dict[str, tuple[float, datetime]] = {}
        for chunk in (symbols[i : i + 40] for i in range(0, len(symbols), 40)):
            try:
                data = yf.download(
                    chunk,
                    period="1d",
                    interval="1m",
                    group_by="ticker",
                    auto_adjust=False,
                    progress=False,
                    threads=True,
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("intraday poll failed for %s: %s", chunk, exc)
                continue
            many = len(chunk) > 1
            for sym in chunk:
                frame = _slice(data, sym, many)
                if frame is None:
                    continue
                closes = frame["close"].dropna()
                if closes.empty:
                    continue
                ts = closes.index[-1]
                ts = ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=IST)
                else:
                    ts = ts.astimezone(IST)
                out[sym] = (float(closes.iloc[-1]), ts)
        return out
