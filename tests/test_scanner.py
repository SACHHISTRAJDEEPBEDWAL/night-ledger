"""End-to-end tests for the scanner engine.

Exercises the real Scanner, WatchlistStore, AlertStore, SetupStore, Universe
and EventBus against scripted price feeds — everything the deployed app runs
except the HTTP layer and the network.

    python -m unittest discover -s tests -t . -v
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import IST, Settings  # noqa: E402
from app.events import EventBus  # noqa: E402
from app.feeds.base import HistoryProvider, LiveFeed  # noqa: E402
from app.models import AlertKind  # noqa: E402
from app.notify import Notifier  # noqa: E402
from app.scanner import Scanner  # noqa: E402
from app.store import AlertStore, SetupStore, WatchlistStore  # noqa: E402
from app.universe import Universe  # noqa: E402
from tests import synth  # noqa: E402

GOOD = "RELIANCE.NS"     # textbook VCP
BAD = "INFY.NS"          # Stage 4 downtrend


class FakeHistory(HistoryProvider):
    def __init__(self):
        self.frames = {
            GOOD: synth.textbook_vcp(),
            BAD: synth.stage4_downtrend(),
        }
        self.calls = 0

    async def daily(self, symbols, lookback_days=400):
        self.calls += 1
        return {s: self.frames[s] for s in symbols if s in self.frames}

    async def benchmark(self, symbol, lookback_days=400):
        return None


class ScriptedFeed(LiveFeed):
    """Replays a fixed list of (symbol, price, timestamp) per poll."""

    live = True
    name = "scripted"

    def __init__(self):
        self.script: list[dict[str, tuple[float, datetime]]] = []
        self.subscribed: list[str] = []

    async def start(self): ...
    async def stop(self): ...

    async def subscribe(self, symbols):
        self.subscribed = list(symbols)

    async def latest(self, symbols):
        if not self.script:
            return {}
        frame = self.script.pop(0)
        return {s: frame[s] for s in symbols if s in frame}


def build(tmp: str):
    settings = Settings(
        data_dir=tmp,
        momentum_window_minutes=15,
        momentum_threshold_pct=2.0,
        momentum_cooldown_minutes=30,
    )
    bus = EventBus()
    scanner = Scanner(
        settings,
        WatchlistStore(tmp),
        AlertStore(tmp),
        SetupStore(),
        Universe(tmp),
        FakeHistory(),
        ScriptedFeed(),
        Notifier(settings),
        bus,
    )
    return scanner, bus


class TestScannerPipeline(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.scanner, self.bus = build(self._tmp.name)
        await self.scanner.watchlist.add(GOOD)
        await self.scanner.watchlist.add(BAD)
        self.events = await self.bus.subscribe()

    async def asyncTearDown(self):
        self._tmp.cleanup()

    def drain(self) -> list[str]:
        out = []
        while not self.events.empty():
            out.append(self.events.get_nowait())
        return out

    # ------------------------------------------------------------------ VCP
    async def test_scan_fires_for_the_valid_base_only(self):
        fired = await self.scanner.scan_setups()
        symbols = {a.symbol for a in fired}
        self.assertEqual(symbols, {GOOD})
        self.assertEqual(fired[0].kind, AlertKind.VCP_SETUP)
        self.assertIsNotNone(fired[0].pivot)

        good = self.scanner.setups.get(GOOD)
        bad = self.scanner.setups.get(BAD)
        self.assertTrue(good.valid)
        self.assertFalse(bad.valid)

    async def test_setup_alert_is_not_repeated_on_the_next_pass(self):
        first = await self.scanner.scan_setups()
        second = await self.scanner.scan_setups()
        self.assertEqual(len(first), 1)
        self.assertEqual(second, [], "same base, same date — must not re-alert")

    async def test_scan_publishes_setups_to_subscribers(self):
        await self.scanner.scan_setups()
        frames = self.drain()
        self.assertTrue(any("event: setups" in f for f in frames))
        self.assertTrue(any("event: alert" in f for f in frames))

    async def test_removing_a_symbol_drops_its_setup(self):
        await self.scanner.scan_setups()
        self.assertIsNotNone(self.scanner.setups.get(BAD))
        await self.scanner.watchlist.remove(BAD)
        await self.scanner.scan_setups()
        self.assertIsNone(self.scanner.setups.get(BAD))

    # ------------------------------------------------------------- momentum
    async def test_momentum_alert_fires_from_the_price_tape(self):
        t0 = datetime(2026, 8, 12, 10, 0, tzinfo=IST)
        feed = self.scanner.feed
        feed.script = [
            {GOOD: (100.0, t0)},
            {GOOD: (100.5, t0 + timedelta(minutes=8))},
            {GOOD: (103.0, t0 + timedelta(minutes=16))},
        ]
        for _ in range(3):
            await self.scanner.poll_prices()

        kinds = [a.kind for a in self.scanner.alerts.recent()]
        self.assertIn(AlertKind.MOMENTUM, kinds)
        alert = next(a for a in self.scanner.alerts.recent() if a.kind is AlertKind.MOMENTUM)
        self.assertAlmostEqual(alert.change_pct, 3.0, delta=0.05)

    async def test_poll_keeps_the_feed_subscription_in_step(self):
        await self.scanner.poll_prices()
        self.assertEqual(set(self.scanner.feed.subscribed), {GOOD, BAD})
        await self.scanner.watchlist.remove(BAD)
        await self.scanner.poll_prices()
        self.assertEqual(set(self.scanner.feed.subscribed), {GOOD})

    # ------------------------------------------------------------- breakout
    async def test_breakout_fires_once_when_price_clears_the_pivot(self):
        await self.scanner.scan_setups()
        pivot = self.scanner.setups.get(GOOD).pivot
        t0 = datetime(2026, 8, 12, 11, 0, tzinfo=IST)
        self.scanner.feed.script = [
            {GOOD: (pivot - 1.0, t0)},                        # still under
            {GOOD: (pivot + 0.5, t0 + timedelta(minutes=1))}, # takes it out
            {GOOD: (pivot + 2.0, t0 + timedelta(minutes=2))}, # keeps going
        ]
        for _ in range(3):
            await self.scanner.poll_prices()

        breakouts = [a for a in self.scanner.alerts.recent() if a.kind is AlertKind.BREAKOUT]
        self.assertEqual(len(breakouts), 1, "one breakout per symbol per day")
        self.assertGreater(breakouts[0].price, pivot)

    async def test_no_breakout_without_a_valid_setup(self):
        """A stock in a downtrend has no pivot, so it can never break out."""
        t0 = datetime(2026, 8, 12, 11, 0, tzinfo=IST)
        self.scanner.feed.script = [{BAD: (99999.0, t0)}]
        await self.scanner.poll_prices()
        self.assertEqual(
            [a for a in self.scanner.alerts.recent() if a.kind is AlertKind.BREAKOUT], []
        )

    # ---------------------------------------------------------------- misc
    async def test_empty_watchlist_is_a_no_op_not_a_crash(self):
        await self.scanner.watchlist.remove(GOOD)
        await self.scanner.watchlist.remove(BAD)
        self.assertEqual(await self.scanner.scan_setups(), [])
        await self.scanner.poll_prices()
        self.assertIsNotNone(self.scanner.status().last_momentum_scan)

    async def test_a_broken_symbol_does_not_abort_the_whole_scan(self):
        self.scanner.history.frames["JUNK.NS"] = pd.DataFrame(
            {"open": [], "high": [], "low": [], "close": [], "volume": []},
            index=pd.DatetimeIndex([]),
        )
        await self.scanner.watchlist.add("JUNK.NS")
        fired = await self.scanner.scan_setups()
        self.assertEqual({a.symbol for a in fired}, {GOOD})

    # ------------------------------------------------------- data health
    async def test_healthy_scan_reports_full_coverage(self):
        await self.scanner.scan_setups()
        h = self.scanner.data_health
        self.assertEqual((h.requested, h.fetched), (2, 2))
        self.assertFalse(h.blocked)
        self.assertEqual(h.valid_setups, 1)

    async def test_provider_returning_nothing_is_flagged_as_blocked(self):
        """The failure mode that matters: the scanner runs, the feed refuses,
        and without this the UI shows a normal empty dashboard."""
        self.scanner.history.frames.clear()
        await self.scanner.scan_setups()
        h = self.scanner.data_health
        self.assertTrue(h.blocked)
        self.assertEqual(h.fetched, 0)
        self.assertTrue(any("no price data" in e for e in self.scanner.errors))

    async def test_partial_coverage_lists_the_missing_symbols(self):
        del self.scanner.history.frames[BAD]
        await self.scanner.scan_setups()
        h = self.scanner.data_health
        self.assertEqual((h.requested, h.fetched), (2, 1))
        self.assertFalse(h.blocked)
        self.assertEqual(h.missing, [BAD])

    async def test_diagnose_reports_a_healthy_feed(self):
        d = await self.scanner.diagnose(GOOD)
        self.assertTrue(d.ok)
        self.assertGreater(d.bars, 200)
        self.assertIsNotNone(d.last_close)
        self.assertIn("healthy", d.summary)

    async def test_diagnose_explains_an_empty_response(self):
        d = await self.scanner.diagnose("NOTLISTED.NS")
        self.assertFalse(d.ok)
        self.assertEqual(d.bars, 0)
        self.assertIn("throttl", d.summary)

    async def test_diagnose_surfaces_a_raised_error(self):
        async def boom(*a, **kw):
            raise RuntimeError("connection reset by peer")
        self.scanner.history.daily = boom
        d = await self.scanner.diagnose(GOOD)
        self.assertFalse(d.ok)
        self.assertIn("connection reset", d.error)

    async def test_watchlist_survives_a_restart(self):
        tmp = self._tmp.name
        reopened = WatchlistStore(tmp)
        self.assertEqual(set(reopened.symbols()), {GOOD, BAD})


class TestMarketClock(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.scanner, _ = build(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_phases_across_a_weekday(self):
        day = datetime(2026, 8, 12, tzinfo=IST)  # a Wednesday
        cases = [
            (day.replace(hour=7, minute=0), "closed"),
            (day.replace(hour=9, minute=5), "pre_open"),
            (day.replace(hour=9, minute=30), "open"),
            (day.replace(hour=15, minute=29), "open"),
            (day.replace(hour=15, minute=45), "post_close"),
            (day.replace(hour=18, minute=0), "closed"),
        ]
        for when, expected in cases:
            with self.subTest(time=when.strftime("%H:%M")):
                self.assertEqual(self.scanner.market_phase(when), expected)

    def test_saturday_is_always_closed(self):
        saturday = datetime(2026, 8, 15, 11, 0, tzinfo=IST)
        self.assertEqual(self.scanner.market_phase(saturday), "closed")


class TestEventBus(unittest.IsolatedAsyncioTestCase):
    async def test_stalled_subscriber_is_dropped_not_blocking(self):
        bus = EventBus()
        q = await bus.subscribe()
        for i in range(200):  # far past the queue bound
            await bus.publish("alert", {"i": i})
        self.assertEqual(bus.listeners, 0, "a browser that stopped reading gets cut")

    async def test_healthy_subscriber_receives_events(self):
        bus = EventBus()
        q = await bus.subscribe()
        await bus.publish("alert", {"symbol": "RELIANCE.NS"})
        frame = q.get_nowait()
        self.assertIn("event: alert", frame)
        self.assertIn("RELIANCE.NS", frame)


if __name__ == "__main__":
    unittest.main(verbosity=2)
