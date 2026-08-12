"""Rolling intraday momentum trigger.

Keeps a short price tape per symbol and fires when the current price is N%
above where it sat `window` minutes ago. The subtlety is the reference sample:
naively taking "the oldest thing in the buffer" makes the scanner fire a fake
+3% every morning by comparing today's open against yesterday's close, so a
reference is only trusted inside a bounded age band.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta

from ..config import IST


@dataclass(frozen=True)
class MomentumSignal:
    symbol: str
    price: float
    reference_price: float
    change_pct: float
    window_minutes: float
    ts: datetime


class MomentumTracker:
    def __init__(
        self,
        window_minutes: int = 15,
        threshold_pct: float = 2.0,
        cooldown_minutes: int = 30,
        stale_grace_minutes: int | None = None,
    ) -> None:
        self.window = timedelta(minutes=window_minutes)
        self.threshold = threshold_pct
        self.cooldown = timedelta(minutes=cooldown_minutes)
        # How much older than `window` a reference sample may be before we
        # treat the gap as a data hole (overnight, lunch break, restart).
        grace = stale_grace_minutes if stale_grace_minutes is not None else window_minutes
        self.stale_grace = timedelta(minutes=grace)
        self._tape: dict[str, deque[tuple[datetime, float]]] = {}
        self._last_fire: dict[str, datetime] = {}

    # ------------------------------------------------------------------ state
    def reset(self, symbol: str) -> None:
        self._tape.pop(symbol, None)
        self._last_fire.pop(symbol, None)

    def keep_only(self, symbols: set[str]) -> None:
        """Drop tape for symbols no longer on the watchlist."""
        for gone in set(self._tape) - symbols:
            self.reset(gone)

    def tape_length(self, symbol: str) -> int:
        return len(self._tape.get(symbol, ()))

    # ------------------------------------------------------------------- core
    def update(
        self, symbol: str, price: float, ts: datetime | None = None
    ) -> MomentumSignal | None:
        if price is None or price <= 0:
            return None
        now = ts or datetime.now(IST)
        if now.tzinfo is None:
            now = now.replace(tzinfo=IST)

        tape = self._tape.setdefault(symbol, deque())
        if tape and now <= tape[-1][0]:
            return None  # out-of-order or duplicate tick
        tape.append((now, float(price)))

        # Keep a little more than we need so the reference search never runs dry.
        horizon = now - (self.window + self.stale_grace)
        while len(tape) > 2 and tape[0][0] < horizon:
            tape.popleft()

        reference = self._reference(tape, now)
        if reference is None:
            return None

        ref_ts, ref_price = reference
        change = (price / ref_price - 1.0) * 100.0
        if change < self.threshold:
            return None

        last = self._last_fire.get(symbol)
        if last and now - last < self.cooldown:
            return None
        self._last_fire[symbol] = now

        return MomentumSignal(
            symbol=symbol,
            price=round(float(price), 2),
            reference_price=round(ref_price, 2),
            change_pct=round(change, 2),
            window_minutes=round((now - ref_ts).total_seconds() / 60, 1),
            ts=now,
        )

    def _reference(
        self, tape: deque[tuple[datetime, float]], now: datetime
    ) -> tuple[datetime, float] | None:
        """Newest sample that is at least `window` old — and not so old that
        the gap spans a session break."""
        oldest_allowed = now - (self.window + self.stale_grace)
        newest_allowed = now - self.window
        for ts, price in reversed(tape):
            if ts <= newest_allowed:
                return (ts, price) if ts >= oldest_allowed else None
        return None
