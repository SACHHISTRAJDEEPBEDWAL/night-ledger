"""Angel One SmartAPI WebSocket — real tick-by-tick prices for NSE/BSE.

The SDK is synchronous and its `connect()` blocks forever, so the socket lives
in a daemon thread and drops ticks into a lock-guarded dict that the async
scanner reads. A supervisor loop rebuilds the session if the socket dies,
because a monitoring script that silently stops receiving prices is worse than
one that never started.

Credentials come from https://smartapi.angelbroking.com -> My Apps (Market
Feeds app). You need: API key, client code, PIN, and the TOTP secret shown
when you enable 2FA (the base32 string behind the QR code).
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime

import httpx

from ..config import IST, Settings
from .base import LiveFeed

log = logging.getLogger(__name__)

SCRIP_MASTER = (
    "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
)

# SmartWebSocketV2 exchange codes
EXCHANGE_TYPE = {"NSE": 1, "BSE": 3}
SUFFIX_TO_EXCHANGE = {".NS": "NSE", ".BO": "BSE"}
MODE_LTP = 1


def yahoo_to_angel(symbol: str) -> tuple[str, str] | None:
    """RELIANCE.NS -> ('RELIANCE-EQ', 'NSE')"""
    for suffix, exch in SUFFIX_TO_EXCHANGE.items():
        if symbol.upper().endswith(suffix):
            return f"{symbol[: -len(suffix)].upper()}-EQ", exch
    return None


class AngelOneFeed(LiveFeed):
    live = True
    name = "angelone"
    delay_note = "Live exchange ticks over WebSocket"

    def __init__(self, settings: Settings) -> None:
        self.s = settings
        self._ticks: dict[str, tuple[float, datetime]] = {}
        self._lock = threading.Lock()
        self._token_to_symbol: dict[str, str] = {}
        self._symbol_to_token: dict[str, tuple[str, str]] = {}  # sym -> (token, exch)
        self._scrip: dict[tuple[str, str], str] = {}            # (tradingsymbol, exch) -> token
        self._want: list[str] = []
        self._sws = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._connected = threading.Event()
        self.last_error: str = ""

    # ------------------------------------------------------------- lifecycle
    async def start(self) -> None:
        self._stop.clear()
        self._load_scrip_master()
        self._thread = threading.Thread(
            target=self._supervise, name="angelone-ws", daemon=True
        )
        self._thread.start()

    async def stop(self) -> None:
        self._stop.set()
        try:
            if self._sws is not None:
                self._sws.close_connection()
        except Exception:  # noqa: BLE001
            pass

    async def subscribe(self, symbols: list[str]) -> None:
        self._want = list(symbols)
        self._resolve_tokens(symbols)
        if self._connected.is_set():
            self._send_subscription()

    async def latest(self, symbols: list[str]) -> dict[str, tuple[float, datetime]]:
        with self._lock:
            return {s: self._ticks[s] for s in symbols if s in self._ticks}

    @property
    def status(self) -> dict:
        return {
            "name": self.name,
            "live": self.live and self._connected.is_set(),
            "delay_note": self.delay_note,
            "connected": self._connected.is_set(),
            "subscribed": len(self._symbol_to_token),
            "error": self.last_error,
        }

    # ------------------------------------------------------- instrument master
    def _load_scrip_master(self) -> None:
        try:
            with httpx.Client(timeout=60) as client:
                rows = client.get(SCRIP_MASTER).json()
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"scrip master fetch failed: {exc}"
            log.error(self.last_error)
            return
        table: dict[tuple[str, str], str] = {}
        for r in rows:
            exch = r.get("exch_seg")
            if exch not in EXCHANGE_TYPE:
                continue
            # Cash equities only — skip futures, options, indices.
            if r.get("instrumenttype"):
                continue
            table[(str(r.get("symbol", "")).upper(), exch)] = str(r.get("token"))
        self._scrip = table
        log.info("angel scrip master loaded: %d cash instruments", len(table))

    def _resolve_tokens(self, symbols: list[str]) -> None:
        mapping: dict[str, tuple[str, str]] = {}
        reverse: dict[str, str] = {}
        unresolved = []
        for sym in symbols:
            pair = yahoo_to_angel(sym)
            if pair is None:
                unresolved.append(sym)
                continue
            trading_symbol, exch = pair
            token = self._scrip.get((trading_symbol, exch))
            if token is None:
                unresolved.append(sym)
                continue
            mapping[sym] = (token, exch)
            reverse[token] = sym
        self._symbol_to_token = mapping
        self._token_to_symbol = reverse
        if unresolved:
            log.warning("no Angel One token for: %s", ", ".join(unresolved[:10]))

    # --------------------------------------------------------------- socket
    def _supervise(self) -> None:
        backoff = 5
        while not self._stop.is_set():
            try:
                self._run_socket()
                backoff = 5
            except Exception as exc:  # noqa: BLE001
                self.last_error = str(exc)
                log.error("angel websocket died: %s", exc)
            finally:
                self._connected.clear()
            if self._stop.is_set():
                return
            time.sleep(backoff)
            backoff = min(backoff * 2, 120)

    def _run_socket(self) -> None:
        from SmartApi import SmartConnect  # noqa: PLC0415
        from SmartApi.smartWebSocketV2 import SmartWebSocketV2  # noqa: PLC0415
        import pyotp  # noqa: PLC0415

        api = SmartConnect(api_key=self.s.angel_api_key)
        otp = pyotp.TOTP(self.s.angel_totp_secret).now()
        session = api.generateSession(
            self.s.angel_client_code, self.s.angel_password, otp
        )
        if not session or not session.get("status"):
            raise RuntimeError(f"login rejected: {session}")
        auth_token = session["data"]["jwtToken"]
        feed_token = api.getfeedToken()

        sws = SmartWebSocketV2(
            auth_token, self.s.angel_api_key, self.s.angel_client_code, feed_token
        )
        self._sws = sws

        def on_open(_wsapp):
            self._connected.set()
            self.last_error = ""
            log.info("angel websocket open")
            self._send_subscription()

        def on_data(_wsapp, message):
            try:
                token = str(message.get("token"))
                symbol = self._token_to_symbol.get(token)
                if not symbol:
                    return
                # SmartAPI quotes in paise for equities.
                raw = message.get("last_traded_price")
                if raw is None:
                    return
                price = float(raw) / 100.0
                epoch_ms = message.get("exchange_timestamp")
                ts = (
                    datetime.fromtimestamp(epoch_ms / 1000, IST)
                    if epoch_ms
                    else datetime.now(IST)
                )
                if price <= 0:
                    return
                with self._lock:
                    self._ticks[symbol] = (price, ts)
            except Exception as exc:  # noqa: BLE001
                log.debug("bad tick %s: %s", message, exc)

        def on_error(_wsapp, error):
            self.last_error = str(error)
            log.warning("angel websocket error: %s", error)

        def on_close(_wsapp):
            self._connected.clear()
            log.info("angel websocket closed")

        sws.on_open = on_open
        sws.on_data = on_data
        sws.on_error = on_error
        sws.on_close = on_close
        sws.connect()  # blocks until the socket drops

    def _send_subscription(self) -> None:
        if self._sws is None or not self._symbol_to_token:
            return
        by_exchange: dict[int, list[str]] = {}
        for token, exch in self._symbol_to_token.values():
            by_exchange.setdefault(EXCHANGE_TYPE[exch], []).append(token)
        payload = [
            {"exchangeType": ex, "tokens": tokens} for ex, tokens in by_exchange.items()
        ]
        try:
            self._sws.subscribe("nightledger", MODE_LTP, payload)
            log.info("subscribed to %d instruments", len(self._symbol_to_token))
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"subscribe failed: {exc}"
            log.error(self.last_error)
