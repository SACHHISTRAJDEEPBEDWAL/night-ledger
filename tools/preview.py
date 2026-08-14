"""Offline preview server for UI work.

Runs the *real* strategy engine and the *real* stores against synthetic price
data, and serves the same JSON shapes as the FastAPI app. That means the
dashboard can be designed, screenshotted and reviewed without market hours,
broker credentials or a network connection — and without the UI drifting away
from the contract, because the payloads come from the same pydantic models.

    python tools/preview.py            # http://127.0.0.1:8777
"""

from __future__ import annotations

import asyncio
import json
import random
import sys
import uuid
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import uvicorn  # noqa: E402
from starlette.applications import Starlette  # noqa: E402
from starlette.responses import FileResponse, JSONResponse, StreamingResponse  # noqa: E402
from starlette.routing import Route  # noqa: E402

from app.config import IST  # noqa: E402
from app.models import Alert, AlertKind  # noqa: E402
from app.strategy import analyse  # noqa: E402
from app.universe import Universe  # noqa: E402
from tests import synth  # noqa: E402

WEB = ROOT / "app" / "web" / "index.html"
universe = Universe(data_dir=str(ROOT / "data"))

BOOK = [
    ("RELIANCE.NS", synth.textbook_vcp),
    ("TATAMOTORS.NS", synth.textbook_vcp),
    ("TCS.NS", synth.no_volume_dryup),
    ("PERSISTENT.NS", synth.widening_base),
    ("INFY.NS", synth.stage4_downtrend),
]

SETUPS: dict[str, dict] = {}
QUOTES: dict[str, dict] = {}
WATCH: list[dict] = []
ALERTS: list[dict] = []
SUBS: set[asyncio.Queue] = set()


def build() -> None:
    now = datetime.now(IST)
    for i, (symbol, factory) in enumerate(BOOK):
        setup = analyse(factory(), symbol, name=universe.name_for(symbol))
        SETUPS[symbol] = json.loads(setup.model_dump_json())
        px = setup.trend.price * random.uniform(0.995, 1.012)
        QUOTES[symbol] = {
            "symbol": symbol,
            "price": round(px, 2),
            "ts": (now - timedelta(seconds=random.randint(1, 40))).isoformat(),
        }
        WATCH.append(
            {
                "symbol": symbol,
                "name": universe.name_for(symbol),
                "exchange": "NSE",
                "added_at": (now - timedelta(days=len(BOOK) - i)).isoformat(),
            }
        )

    def add(kind, symbol, headline, detail, minutes, **kw):
        setup = SETUPS.get(symbol, {})
        ALERTS.append(
            json.loads(
                Alert(
                    id=uuid.uuid4().hex[:12],
                    kind=kind,
                    symbol=symbol,
                    name=universe.name_for(symbol),
                    ts=now - timedelta(minutes=minutes),
                    price=QUOTES[symbol]["price"],
                    headline=headline,
                    detail=detail,
                    pivot=setup.get("pivot"),
                    stop=setup.get("stop"),
                    **kw,
                ).model_dump_json()
            )
        )

    add(AlertKind.MOMENTUM, "TATAMOTORS.NS", "TATAMOTORS +2.41% in 15 min",
        "284.60 → 291.46", 3, change_pct=2.41)
    add(AlertKind.BREAKOUT, "RELIANCE.NS", "RELIANCE broke the pivot at 295.31",
        "+0.62% through the buy point · stop 285.97 (3.2% risk)", 18, change_pct=0.62)
    add(AlertKind.VCP_SETUP, "RELIANCE.NS", "RELIANCE — VCP base complete",
        "3 contractions 15.3% → 8.3% → 3.2% · volume at 41% of 50d avg · score 84/100", 96)
    add(AlertKind.MOMENTUM, "PERSISTENT.NS", "PERSISTENT +2.08% in 14 min",
        "1,204.10 → 1,229.15", 131, change_pct=2.08)
    add(AlertKind.VCP_SETUP, "TATAMOTORS.NS", "TATAMOTORS — VCP base complete",
        "3 contractions 15.3% → 8.3% → 3.2% · volume at 41% of 50d avg · score 81/100", 204)
    ALERTS.sort(key=lambda a: a["ts"], reverse=True)


async def status(_req):
    return JSONResponse(
        {
            "scanner": {
                "running": True,
                "feed": "yahoo",
                "live_ticks": False,
                "market_phase": "open",
                "server_time_ist": datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S"),
                "watchlist_size": len(WATCH),
                "last_momentum_scan": datetime.now(IST).isoformat(),
                "last_vcp_scan": (datetime.now(IST) - timedelta(minutes=12)).isoformat(),
                "alerts_today": len(ALERTS),
                "errors": [],
                "data_health": {
                    "requested": len(WATCH), "fetched": len(WATCH),
                    "valid_setups": sum(1 for s in SETUPS.values() if s["valid"]),
                    "missing": [], "at": datetime.now(IST).isoformat(), "blocked": False,
                },
            },
            "feed": {
                "name": "yahoo",
                "live": False,
                "delay_note": "Yahoo intraday data for Indian equities lags ~15 min",
            },
            "universe": {"source": universe.source, "size": universe.size},
            "notify": False,
            "settings": {
                "momentum_window_minutes": 15,
                "momentum_threshold_pct": 2.0,
                "vcp_scan_minutes": 30,
                "poll_seconds": 30,
            },
            "listeners": len(SUBS),
        }
    )


async def watchlist(_req):
    return JSONResponse(
        [{**w, "quote": QUOTES.get(w["symbol"]), "setup": SETUPS.get(w["symbol"])} for w in WATCH]
    )


async def alerts(_req):
    return JSONResponse(ALERTS)


async def setups(_req):
    return JSONResponse(list(SETUPS.values()))


async def search(req):
    q = req.query_params.get("q", "")
    return JSONResponse([json.loads(r.model_dump_json()) for r in universe.search(q)])


async def scan(_req):
    valid = sum(1 for s in SETUPS.values() if s["valid"])
    return JSONResponse({"scanned": len(WATCH), "new_alerts": 0,
                         "fetched": len(WATCH), "valid_setups": valid, "blocked": False})


async def diagnostics(_req):
    return JSONResponse({
        "ok": True, "probe": "RELIANCE.NS", "bars": 335, "latency_ms": 412,
        "last_close": 291.0, "last_bar_date": "2024-04-12", "error": "",
        "summary": "Price feed is healthy — 335 daily bars for RELIANCE.NS, "
                   "last close 291.00. Screening will work.",
    })


async def stream(_req):
    q: asyncio.Queue = asyncio.Queue(maxsize=32)
    SUBS.add(q)

    async def gen():
        try:
            yield "retry: 3000\n\n"
            yield 'event: hello\ndata: {"ok":true}\n\n'
            while True:
                try:
                    yield await asyncio.wait_for(q.get(), timeout=15)
                except TimeoutError:
                    yield ": keep-alive\n\n"
        finally:
            SUBS.discard(q)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache"})


async def index(_req):
    return FileResponse(WEB)


async def manifest(_req):
    return JSONResponse({"name": "Night Ledger", "short_name": "Ledger"})


build()

app = Starlette(
    routes=[
        Route("/", index),
        Route("/manifest.webmanifest", manifest),
        Route("/api/status", status),
        Route("/api/watchlist", watchlist),
        Route("/api/alerts", alerts),
        Route("/api/setups", setups),
        Route("/api/search", search),
        Route("/api/scan", scan, methods=["POST"]),
        Route("/api/diagnostics", diagnostics),
        Route("/api/stream", stream),
    ]
)

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8777, log_level="warning")
