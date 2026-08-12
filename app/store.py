"""In-memory state with JSON durability.

`ACTIVE_WATCHLIST` lives here. The scanner reads it on every pass, so adding a
stock from your phone takes effect on the next cycle without a restart — which
was the whole point of not hard-coding tickers.

JSON on disk (not a database) is deliberate: the state is a few kilobytes, and
one file makes the app trivially portable between Render, Railway and a VM.
Note that on Render's free tier the filesystem is ephemeral — attach a disk, or
accept that the watchlist resets on redeploy.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import deque
from datetime import date, datetime
from pathlib import Path

from .config import IST
from .models import Alert, VCPSetup, WatchItem

log = logging.getLogger(__name__)

MAX_ALERTS = 400


class WatchlistStore:
    def __init__(self, data_dir: str = "data") -> None:
        self.path = Path(data_dir) / "watchlist.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._items: dict[str, WatchItem] = {}
        self._lock = asyncio.Lock()
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            for row in raw:
                item = WatchItem(**row)
                self._items[item.symbol] = item
            log.info("watchlist restored: %d symbols", len(self._items))
        except Exception as exc:  # noqa: BLE001
            log.warning("could not restore watchlist: %s", exc)

    def _persist(self) -> None:
        try:
            payload = [json.loads(i.model_dump_json()) for i in self._items.values()]
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            tmp.replace(self.path)
        except Exception as exc:  # noqa: BLE001
            log.warning("could not persist watchlist: %s", exc)

    # ------------------------------------------------------------------ read
    def symbols(self) -> list[str]:
        return list(self._items.keys())

    def items(self) -> list[WatchItem]:
        return sorted(self._items.values(), key=lambda i: i.added_at)

    def contains(self, symbol: str) -> bool:
        return symbol.upper() in self._items

    # ----------------------------------------------------------------- write
    async def add(self, symbol: str, name: str = "", exchange: str = "NSE") -> WatchItem:
        symbol = symbol.upper().strip()
        async with self._lock:
            if symbol in self._items:
                return self._items[symbol]
            item = WatchItem(
                symbol=symbol,
                name=name,
                exchange=exchange,
                added_at=datetime.now(IST),
            )
            self._items[symbol] = item
            self._persist()
            return item

    async def remove(self, symbol: str) -> bool:
        symbol = symbol.upper().strip()
        async with self._lock:
            existed = self._items.pop(symbol, None) is not None
            if existed:
                self._persist()
            return existed


class AlertStore:
    """Bounded tape of fired alerts plus the dedupe bookkeeping that stops the
    same setup from screaming at you all day."""

    def __init__(self, data_dir: str = "data", maxlen: int = MAX_ALERTS) -> None:
        self.path = Path(data_dir) / "alerts.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._alerts: deque[Alert] = deque(maxlen=maxlen)
        self._seen: set[str] = set()
        self._seen_day: date = datetime.now(IST).date()
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            for row in json.loads(self.path.read_text(encoding="utf-8")):
                self._alerts.append(Alert(**row))
        except Exception as exc:  # noqa: BLE001
            log.warning("could not restore alerts: %s", exc)

    def _persist(self) -> None:
        try:
            payload = [json.loads(a.model_dump_json()) for a in self._alerts]
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload), encoding="utf-8")
            tmp.replace(self.path)
        except Exception as exc:  # noqa: BLE001
            log.debug("could not persist alerts: %s", exc)

    def _roll_day(self) -> None:
        today = datetime.now(IST).date()
        if today != self._seen_day:
            self._seen.clear()
            self._seen_day = today

    def already_fired(self, key: str) -> bool:
        self._roll_day()
        return key in self._seen

    def mark_fired(self, key: str) -> None:
        self._roll_day()
        self._seen.add(key)

    def add(self, alert: Alert) -> Alert:
        self._alerts.appendleft(alert)
        self._persist()
        return alert

    def recent(self, limit: int = 100) -> list[Alert]:
        return list(self._alerts)[:limit]

    def count_today(self) -> int:
        today = datetime.now(IST).date()
        return sum(1 for a in self._alerts if a.ts.astimezone(IST).date() == today)

    def clear(self) -> None:
        self._alerts.clear()
        self._seen.clear()
        self._persist()


class SetupStore:
    """Latest VCP verdict per symbol. Purely in-memory — it is recomputed from
    daily bars every scan cycle, so there is nothing worth persisting."""

    def __init__(self) -> None:
        self._setups: dict[str, VCPSetup] = {}

    def put(self, setup: VCPSetup) -> None:
        self._setups[setup.symbol] = setup

    def get(self, symbol: str) -> VCPSetup | None:
        return self._setups.get(symbol)

    def all(self) -> list[VCPSetup]:
        return sorted(
            self._setups.values(), key=lambda s: (not s.valid, -s.score, s.symbol)
        )

    def keep_only(self, symbols: set[str]) -> None:
        for gone in set(self._setups) - symbols:
            self._setups.pop(gone, None)
