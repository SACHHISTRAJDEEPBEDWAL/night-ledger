"""FastAPI surface: one URL that works on a laptop, a tablet or a phone.

The API is deliberately small — the browser reads state over REST once on load,
then everything after that arrives over the SSE stream at /api/stream.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    StreamingResponse,
)

from .config import settings
from .events import bus
from .feeds import build_history, build_live_feed
from .models import SearchResult
from .notify import Notifier
from .scanner import Scanner
from .store import AlertStore, SetupStore, WatchlistStore
from .universe import Universe

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("nightledger")

WEB_DIR = Path(__file__).resolve().parent / "web"
HEARTBEAT_SECONDS = 20

watchlist = WatchlistStore(settings.data_dir, seed=settings.default_watchlist)
alerts = AlertStore(settings.data_dir)
setups = SetupStore()
universe = Universe(settings.data_dir)
history = build_history(settings)
live_feed = build_live_feed(settings)
notifier = Notifier(settings)
scanner = Scanner(
    settings, watchlist, alerts, setups, universe, history, live_feed, notifier, bus
)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    await scanner.start()
    try:
        yield
    finally:
        await scanner.stop()


app = FastAPI(title=settings.app_name, version="1.0.0", lifespan=lifespan)


# --------------------------------------------------------------------- auth
PUBLIC_PATHS = {"/healthz", "/manifest.webmanifest", "/favicon.ico"}

# A bare `{"detail":"unauthorised"}` in the browser reads as "the app is
# broken", not "you need a token" — so a locked instance explains itself and
# offers the way in. API calls still get plain JSON.
UNLOCK_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Night Ledger — locked</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Instrument+Serif:ital@0;1&family=IBM+Plex+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
:root{--ink:#0A0B0D;--paper:#E8E1D3;--saffron:#F2A03D;--muted:#867F73;
  --rule:rgba(232,225,211,.13);--mono:"IBM Plex Mono",ui-monospace,monospace;
  --serif:"Instrument Serif",Georgia,serif}
*{box-sizing:border-box;margin:0;padding:0}
body{min-height:100dvh;display:grid;place-items:center;padding:24px;
  background:var(--ink);color:var(--paper);font-family:var(--mono);font-size:13px;
  background-image:radial-gradient(900px 480px at 80% -10%,rgba(242,160,61,.12),transparent 60%),
    repeating-linear-gradient(to bottom,transparent 0 29px,rgba(232,225,211,.05) 29px 30px)}
.card{width:100%;max-width:430px;animation:rise .7s cubic-bezier(.16,1,.3,1) both}
@keyframes rise{from{opacity:0;transform:translateY(10px)}to{opacity:1;transform:none}}
h1{font-family:var(--serif);font-size:34px;font-weight:400;line-height:1.05}
h1 em{font-style:italic;color:var(--saffron)}
.sub{font-size:10px;letter-spacing:.18em;text-transform:uppercase;color:var(--muted);
  margin-top:6px}
p{color:var(--muted);line-height:1.7;margin-top:20px;font-size:12px}
form{display:flex;gap:8px;margin-top:20px}
input{flex:1;min-width:0;padding:11px 12px;background:#0E1013;border:1px solid var(--rule);
  color:var(--paper);font-family:var(--mono);font-size:12.5px;outline:none;transition:.2s}
input:focus{border-color:rgba(242,160,61,.5);box-shadow:0 0 0 3px rgba(242,160,61,.14)}
button{padding:11px 16px;background:rgba(242,160,61,.15);border:1px solid rgba(242,160,61,.4);
  color:var(--saffron);font-family:var(--mono);font-size:11px;letter-spacing:.12em;
  text-transform:uppercase;cursor:pointer;transition:.2s;white-space:nowrap}
button:hover{background:rgba(242,160,61,.28)}
.hint{margin-top:22px;padding-top:16px;border-top:1px solid var(--rule);font-size:11px;
  color:var(--muted);line-height:1.8}
.hint b{color:var(--paper);font-weight:500}
code{color:var(--saffron)}
</style></head><body><div class="card">
<h1>Night <em>Ledger</em></h1>
<div class="sub">Access required</div>
<p>This scanner is running, but it is protected by an access token. Nothing is
broken — you just need the key.</p>
<form method="get" action="/">
  <input name="token" type="password" placeholder="Paste your access token"
         autocomplete="off" autofocus>
  <button type="submit">Unlock</button>
</form>
<div class="hint"><b>Where to find it:</b> your host's environment settings, under
<code>ACCESS_TOKEN</code>. On Render that is your service → Environment.<br>
<b>Don't want a lock?</b> Delete the <code>ACCESS_TOKEN</code> variable and redeploy —
the dashboard then opens for anyone with the link.</div>
</div></body></html>"""


@app.middleware("http")
async def gate(request: Request, call_next):
    """Optional shared-secret gate. Set ACCESS_TOKEN to lock a public URL.

    A token in the query string is exchanged for a cookie once, so the link
    you bookmark on your phone keeps working without the secret hanging around
    in every subsequent request.
    """
    if not settings.access_token or request.url.path in PUBLIC_PATHS:
        return await call_next(request)

    supplied = (
        request.query_params.get("token")
        or request.headers.get("x-access-token")
        or request.cookies.get("nl_token")
    )
    if supplied != settings.access_token:
        wants_html = "text/html" in request.headers.get("accept", "")
        if wants_html and not request.url.path.startswith("/api/"):
            return HTMLResponse(UNLOCK_PAGE, status_code=401)
        return JSONResponse({"detail": "unauthorised"}, status_code=401)

    if request.query_params.get("token"):
        clean = str(request.url.remove_query_params("token"))
        resp = RedirectResponse(clean, status_code=302)
        resp.set_cookie(
            "nl_token",
            settings.access_token,
            max_age=60 * 60 * 24 * 365,
            httponly=True,
            samesite="lax",
        )
        return resp
    return await call_next(request)


# ------------------------------------------------------------------- pages
@app.get("/", include_in_schema=False)
async def dashboard() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")


@app.get("/healthz", include_in_schema=False)
async def healthz() -> dict:
    """Point an uptime pinger here — it doubles as the keep-alive that stops
    a free-tier web service from idling out."""
    return {
        "ok": True,
        "phase": scanner.market_phase(),
        "feed": live_feed.name,
        # Diagnosable from outside without exposing anything: tells you whether
        # a 401 on / is expected, or a real fault.
        "auth_required": bool(settings.access_token),
        "watchlist": len(watchlist.symbols()),
    }


@app.get("/manifest.webmanifest", include_in_schema=False)
async def manifest() -> JSONResponse:
    return JSONResponse(
        {
            "name": settings.app_name,
            "short_name": "Ledger",
            "start_url": "/",
            "display": "standalone",
            "background_color": "#0A0B0D",
            "theme_color": "#0A0B0D",
            "icons": [],
        },
        media_type="application/manifest+json",
    )


# --------------------------------------------------------------------- API
@app.get("/api/status")
async def api_status() -> dict:
    return {
        "scanner": scanner.status().model_dump(mode="json"),
        "feed": live_feed.status,
        "universe": {"source": universe.source, "size": universe.size},
        "notify": notifier.enabled,
        "settings": {
            "momentum_window_minutes": settings.momentum_window_minutes,
            "momentum_threshold_pct": settings.momentum_threshold_pct,
            "vcp_scan_minutes": settings.vcp_scan_minutes,
            "poll_seconds": settings.momentum_poll_seconds,
        },
        "listeners": bus.listeners,
    }


@app.get("/api/search", response_model=list[SearchResult])
async def api_search(
    q: str = Query(min_length=1, max_length=48),
    include_global: bool = Query(default=True),
) -> list[SearchResult]:
    results = universe.search(q)
    if include_global and len(results) < 6:
        known = {r.symbol for r in results}
        for extra in await universe.search_global(q):
            if extra.symbol not in known:
                results.append(extra)
    return results[:14]


@app.get("/api/watchlist")
async def api_watchlist() -> list[dict]:
    out = []
    for item in watchlist.items():
        setup = setups.get(item.symbol)
        quote = scanner.quotes.get(item.symbol)
        out.append(
            {
                **item.model_dump(mode="json"),
                "name": item.name or universe.name_for(item.symbol),
                "quote": quote,
                "setup": setup.model_dump(mode="json") if setup else None,
            }
        )
    return out


@app.post("/api/watchlist", status_code=201)
async def api_watchlist_add(payload: dict = Body(...)) -> dict:
    symbol = str(payload.get("symbol", "")).upper().strip()
    if not symbol:
        raise HTTPException(400, "symbol is required")
    if len(watchlist.symbols()) >= 250:
        raise HTTPException(400, "watchlist is full (250 symbols)")
    item = await watchlist.add(
        symbol,
        name=str(payload.get("name") or universe.name_for(symbol)),
        exchange=str(payload.get("exchange") or ("BSE" if symbol.endswith(".BO") else "NSE")),
    )
    # Screen it immediately so the card is not blank while the user watches.
    asyncio.create_task(scanner.scan_setups([symbol]))
    await bus.publish("watchlist", {"action": "add", "symbol": symbol})
    return item.model_dump(mode="json")


@app.delete("/api/watchlist/{symbol}")
async def api_watchlist_remove(symbol: str) -> dict:
    removed = await watchlist.remove(symbol)
    if not removed:
        raise HTTPException(404, f"{symbol} is not on the watchlist")
    scanner.momentum.reset(symbol.upper())
    scanner.quotes.pop(symbol.upper(), None)
    setups.keep_only(set(watchlist.symbols()))
    await bus.publish("watchlist", {"action": "remove", "symbol": symbol.upper()})
    return {"removed": symbol.upper()}


@app.get("/api/alerts")
async def api_alerts(limit: int = Query(default=100, ge=1, le=400)) -> list[dict]:
    return [a.model_dump(mode="json") for a in alerts.recent(limit)]


@app.delete("/api/alerts")
async def api_alerts_clear() -> dict:
    alerts.clear()
    await bus.publish("alerts_cleared", {})
    return {"cleared": True}


@app.get("/api/setups")
async def api_setups() -> list[dict]:
    return [s.model_dump(mode="json") for s in setups.all()]


@app.get("/api/setups/{symbol}")
async def api_setup(symbol: str) -> dict:
    setup = setups.get(symbol.upper())
    if not setup:
        raise HTTPException(404, f"no analysis for {symbol} yet")
    return setup.model_dump(mode="json")


@app.post("/api/scan")
async def api_scan() -> dict:
    fired = await scanner.scan_setups()
    health = scanner.data_health
    return {
        "scanned": len(watchlist.symbols()),
        "new_alerts": len(fired),
        "fetched": health.fetched if health else 0,
        "valid_setups": health.valid_setups if health else 0,
        "blocked": bool(health and health.blocked),
    }


@app.get("/api/diagnostics")
async def api_diagnostics(symbol: str = Query(default="RELIANCE.NS")) -> dict:
    """Probe the price feed on demand. Turns 'it's empty' into a real answer."""
    return (await scanner.diagnose(symbol.upper())).model_dump(mode="json")


@app.get("/api/stream")
async def api_stream(request: Request) -> StreamingResponse:
    queue = await bus.subscribe()

    async def gen():
        try:
            yield "retry: 3000\n\n"
            yield 'event: hello\ndata: {"ok":true}\n\n'
            while True:
                if await request.is_disconnected():
                    break
                try:
                    frame = await asyncio.wait_for(queue.get(), timeout=HEARTBEAT_SECONDS)
                    yield frame
                except TimeoutError:
                    yield ": keep-alive\n\n"
        finally:
            await bus.unsubscribe(queue)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@app.get("/favicon.ico", include_in_schema=False)
async def favicon() -> Response:
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
        '<rect width="32" height="32" fill="#0A0B0D"/>'
        '<path d="M4 24 L10 10 L16 20 L22 14 L28 8" stroke="#F2A03D" '
        'stroke-width="2.5" fill="none" stroke-linejoin="round"/></svg>'
    )
    return Response(svg, media_type="image/svg+xml")


if __name__ == "__main__":
    import uvicorn

    with contextlib.suppress(KeyboardInterrupt):
        uvicorn.run(
            "app.main:app", host=settings.host, port=settings.port, log_level="info"
        )
