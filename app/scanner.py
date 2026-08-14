"""The always-on engine.

Two independent loops, because the two signals live on different clocks:

  VCP loop       — daily bars, every `vcp_scan_minutes`. Slow, cache-backed,
                   answers "which of my stocks are set up?"
  momentum loop  — intraday prices, every `momentum_poll_seconds`. Fast,
                   answers "is one of them moving right now?"

Both read ACTIVE_WATCHLIST fresh on every pass, so edits from the dashboard
take effect on the next cycle with no restart.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import datetime, time as dtime, timedelta

from .config import IST, Settings
from .events import EventBus
from .feeds.base import HistoryProvider, LiveFeed
from .models import Alert, AlertKind, DataHealth, Diagnostic, ScannerStatus
from .notify import Notifier
from .store import AlertStore, SetupStore, WatchlistStore
from .strategy import MomentumTracker, analyse, params_from_settings
from .universe import Universe

log = logging.getLogger(__name__)

PRE_OPEN_MINUTES = 15
POST_CLOSE_MINUTES = 30
IDLE_VCP_MINUTES = 240


def _parse_hhmm(value: str) -> dtime:
    hh, mm = value.split(":")
    return dtime(int(hh), int(mm))


class Scanner:
    def __init__(
        self,
        settings: Settings,
        watchlist: WatchlistStore,
        alerts: AlertStore,
        setups: SetupStore,
        universe: Universe,
        history: HistoryProvider,
        feed: LiveFeed,
        notifier: Notifier,
        bus: EventBus,
    ) -> None:
        self.s = settings
        self.watchlist = watchlist
        self.alerts = alerts
        self.setups = setups
        self.universe = universe
        self.history = history
        self.feed = feed
        self.notifier = notifier
        self.bus = bus

        self.vcp_params, self.trend_params = params_from_settings(settings)
        self.momentum = MomentumTracker(
            window_minutes=settings.momentum_window_minutes,
            threshold_pct=settings.momentum_threshold_pct,
            cooldown_minutes=settings.momentum_cooldown_minutes,
        )

        self._tasks: list[asyncio.Task] = []
        self._running = False
        self._subscribed: set[str] = set()
        self.quotes: dict[str, dict] = {}
        self.errors: list[str] = []
        self.data_health: DataHealth | None = None
        self.last_momentum_scan: datetime | None = None
        self.last_vcp_scan: datetime | None = None
        self._open = _parse_hhmm(settings.market_open)
        self._close = _parse_hhmm(settings.market_close)

    # ------------------------------------------------------------- lifecycle
    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        await self.feed.start()
        self._tasks = [
            asyncio.create_task(self._vcp_loop(), name="vcp-loop"),
            asyncio.create_task(self._momentum_loop(), name="momentum-loop"),
            asyncio.create_task(self._universe_loop(), name="universe-loop"),
        ]
        log.info("scanner started on feed=%s", self.feed.name)

    async def stop(self) -> None:
        self._running = False
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        await self.feed.stop()
        log.info("scanner stopped")

    # ---------------------------------------------------------- market clock
    def market_phase(self, now: datetime | None = None) -> str:
        now = now or datetime.now(IST)
        if now.weekday() >= 5:
            return "closed"
        t = now.time()
        pre_open_start = (
            datetime.combine(now.date(), self._open)
            - timedelta(minutes=PRE_OPEN_MINUTES)
        ).time()
        post_close_end = (
            datetime.combine(now.date(), self._close)
            + timedelta(minutes=POST_CLOSE_MINUTES)
        ).time()
        if t < pre_open_start:
            return "closed"
        if t < self._open:
            return "pre_open"
        if t <= self._close:
            return "open"
        if t <= post_close_end:
            return "post_close"
        return "closed"

    @property
    def market_active(self) -> bool:
        return self.market_phase() in {"pre_open", "open", "post_close"}

    # ------------------------------------------------------------- VCP cycle
    async def _vcp_loop(self) -> None:
        await asyncio.sleep(2)  # let the web server bind first
        while self._running:
            try:
                await self.scan_setups()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self._record_error(f"VCP scan: {exc}")
                log.exception("VCP scan failed")
            minutes = self.s.vcp_scan_minutes if self.market_active else IDLE_VCP_MINUTES
            await asyncio.sleep(minutes * 60)

    async def scan_setups(self, symbols: list[str] | None = None) -> list:
        targets = symbols if symbols is not None else self.watchlist.symbols()
        if not targets:
            self.last_vcp_scan = datetime.now(IST)
            return []

        frames = await self.history.daily(targets)
        benchmark = await self.history.benchmark(self.s.rs_benchmark)

        missing = [s for s in targets if s not in frames]
        if missing and not frames:
            # Every single fetch failed. That is a provider problem, not a
            # market observation, and the dashboard has to say so.
            self._record_error(
                f"no price data for any of {len(targets)} symbols — "
                "the data provider may be throttling this host"
            )

        fired: list[Alert] = []
        for symbol in targets:
            frame = frames.get(symbol)
            if frame is None or len(frame) < 60:
                continue
            try:
                setup = analyse(
                    frame,
                    symbol,
                    name=self.universe.name_for(symbol),
                    benchmark=benchmark,
                    vcp_params=self.vcp_params,
                    trend_params=self.trend_params,
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("analysis failed for %s: %s", symbol, exc)
                continue

            self.setups.put(setup)

            if setup.valid:
                key = f"vcp:{symbol}:{setup.as_of}"
                if not self.alerts.already_fired(key):
                    self.alerts.mark_fired(key)
                    depths = " → ".join(
                        f"{c.depth_pct:.1f}%" for c in setup.contractions
                    )
                    fired.append(
                        await self.emit(
                            AlertKind.VCP_SETUP,
                            symbol,
                            setup.trend.price,
                            headline=f"{_short(symbol)} — VCP base complete",
                            detail=(
                                f"{len(setup.contractions)} contractions {depths} · "
                                f"volume at {setup.volume_dryup:.0%} of 50d avg · "
                                f"score {setup.score:.0f}/100"
                            ),
                            pivot=setup.pivot,
                            stop=setup.stop,
                            payload={"score": setup.score, "risk_pct": setup.risk_pct},
                        )
                    )

        self.setups.keep_only(set(self.watchlist.symbols()))
        self.last_vcp_scan = datetime.now(IST)
        self.data_health = DataHealth(
            requested=len(targets),
            fetched=len(frames),
            valid_setups=sum(1 for s in self.setups.all() if s.valid),
            missing=missing[:12],
            at=self.last_vcp_scan,
            blocked=bool(targets) and not frames,
        )
        await self.bus.publish("setups", [s.model_dump(mode="json") for s in self.setups.all()])
        return fired

    # -------------------------------------------------------- momentum cycle
    async def _momentum_loop(self) -> None:
        await asyncio.sleep(4)
        while self._running:
            try:
                if self.market_active:
                    await self.poll_prices()
                    delay = self.s.momentum_poll_seconds
                else:
                    delay = 120
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self._record_error(f"price poll: {exc}")
                log.exception("price poll failed")
                delay = max(self.s.momentum_poll_seconds, 30)
            await asyncio.sleep(delay)

    async def poll_prices(self) -> None:
        symbols = self.watchlist.symbols()
        await self._sync_subscription(symbols)
        if not symbols:
            self.last_momentum_scan = datetime.now(IST)
            return

        prices = await self.feed.latest(symbols)
        self.momentum.keep_only(set(symbols))
        self.last_momentum_scan = datetime.now(IST)

        snapshot: dict[str, dict] = {}
        for symbol, (price, ts) in prices.items():
            snapshot[symbol] = {
                "symbol": symbol,
                "price": round(price, 2),
                "ts": ts.isoformat(),
            }

            signal = self.momentum.update(symbol, price, ts)
            if signal is not None:
                await self.emit(
                    AlertKind.MOMENTUM,
                    symbol,
                    signal.price,
                    headline=(
                        f"{_short(symbol)} +{signal.change_pct:.2f}% "
                        f"in {signal.window_minutes:.0f} min"
                    ),
                    detail=f"{signal.reference_price:,.2f} → {signal.price:,.2f}",
                    change_pct=signal.change_pct,
                    ts=ts,
                )

            await self._check_breakout(symbol, price, ts)

        # Carry forward the previous price so the tape does not blank out when
        # a single symbol misses a poll.
        self.quotes.update(snapshot)
        if snapshot:
            await self.bus.publish("quotes", list(snapshot.values()))

    async def _check_breakout(self, symbol: str, price: float, ts: datetime) -> None:
        setup = self.setups.get(symbol)
        if not setup or not setup.valid or not setup.pivot:
            return
        if price <= setup.pivot:
            return
        key = f"breakout:{symbol}:{ts.astimezone(IST).date()}"
        if self.alerts.already_fired(key):
            return
        self.alerts.mark_fired(key)
        through = (price / setup.pivot - 1) * 100
        await self.emit(
            AlertKind.BREAKOUT,
            symbol,
            price,
            headline=f"{_short(symbol)} broke the pivot at {setup.pivot:,.2f}",
            detail=(
                f"{through:+.2f}% through the buy point · "
                f"stop {setup.stop:,.2f} ({setup.risk_pct:.1f}% risk)"
            ),
            change_pct=round(through, 2),
            pivot=setup.pivot,
            stop=setup.stop,
            ts=ts,
            payload={"score": setup.score},
        )

    # ---------------------------------------------------------------- shared
    async def _sync_subscription(self, symbols: list[str]) -> None:
        current = set(symbols)
        if current != self._subscribed:
            await self.feed.subscribe(symbols)
            self._subscribed = current

    async def _universe_loop(self) -> None:
        while self._running:
            try:
                if await self.universe.refresh():
                    await self.bus.publish("universe", {"source": self.universe.source})
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.debug("universe refresh: %s", exc)
            await asyncio.sleep(6 * 3600)

    async def emit(
        self,
        kind: AlertKind,
        symbol: str,
        price: float,
        headline: str,
        detail: str = "",
        change_pct: float | None = None,
        pivot: float | None = None,
        stop: float | None = None,
        ts: datetime | None = None,
        payload: dict | None = None,
    ) -> Alert:
        alert = Alert(
            id=uuid.uuid4().hex[:12],
            kind=kind,
            symbol=symbol,
            name=self.universe.name_for(symbol),
            ts=ts or datetime.now(IST),
            price=round(price, 2),
            headline=headline,
            detail=detail,
            change_pct=change_pct,
            pivot=pivot,
            stop=stop,
            payload=payload or {},
        )
        self.alerts.add(alert)
        await self.bus.publish("alert", alert.model_dump(mode="json"))
        if self.notifier.enabled:
            asyncio.create_task(self.notifier.send(alert))
        log.info("[%s] %s", kind.value, headline)
        return alert

    def _record_error(self, message: str) -> None:
        stamped = f"{datetime.now(IST):%H:%M:%S} {message}"
        self.errors.append(stamped)
        del self.errors[:-8]

    def status(self) -> ScannerStatus:
        feed_status = self.feed.status
        return ScannerStatus(
            running=self._running,
            feed=self.feed.name,
            live_ticks=bool(feed_status.get("live")),
            market_phase=self.market_phase(),
            server_time_ist=datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S"),
            watchlist_size=len(self.watchlist.symbols()),
            last_momentum_scan=self.last_momentum_scan,
            last_vcp_scan=self.last_vcp_scan,
            alerts_today=self.alerts.count_today(),
            errors=list(self.errors),
            data_health=self.data_health,
        )

    async def diagnose(self, probe: str = "RELIANCE.NS") -> Diagnostic:
        """Fetch one symbol end to end and report plainly what happened.

        This exists because "the dashboard is empty" has several very
        different causes — no watchlist, market closed, provider blocked,
        nothing set up — and guessing between them from the outside is
        miserable.
        """
        started = time.perf_counter()
        try:
            frames = await self.history.daily([probe], lookback_days=260)
        except Exception as exc:  # noqa: BLE001
            return Diagnostic(
                ok=False, probe=probe, error=str(exc)[:300],
                latency_ms=int((time.perf_counter() - started) * 1000),
                summary="The data provider raised an error. See `error` for detail.",
            )

        elapsed = int((time.perf_counter() - started) * 1000)
        frame = frames.get(probe)
        if frame is None or frame.empty:
            return Diagnostic(
                ok=False, probe=probe, latency_ms=elapsed,
                summary=(
                    "Reached the provider but got zero bars back. Yahoo throttles "
                    "cloud/datacenter IPs — this usually means this host is rate "
                    "limited, not that the symbol is wrong. Retry in a few minutes, "
                    "or switch FEED to a broker API."
                ),
            )

        close = float(frame["close"].iloc[-1])
        return Diagnostic(
            ok=True, probe=probe, bars=len(frame), latency_ms=elapsed,
            last_close=round(close, 2),
            last_bar_date=str(frame.index[-1].date()),
            summary=(
                f"Price feed is healthy — {len(frame)} daily bars for {probe}, "
                f"last close {close:,.2f}. Screening will work."
            ),
        )


def _short(symbol: str) -> str:
    return symbol.removesuffix(".NS").removesuffix(".BO")
