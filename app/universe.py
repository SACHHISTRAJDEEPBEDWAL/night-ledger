"""The searchable instrument universe.

Three layers, each a fallback for the one above:

  1. NSE's own EQUITY_L.csv, refreshed daily and cached to disk.
  2. A seed list checked into the repo, so search works on first boot and
     keeps working when NSE blocks the scraper (which it does, often).
  3. Yahoo's search endpoint, for anything global the first two don't know.

Symbols are stored Yahoo-style (RELIANCE.NS / 500325.BO) because that is what
the history provider speaks; the broker adapters translate on their side.
"""

from __future__ import annotations

import asyncio
import csv
import io
import logging
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

from .models import SearchResult

log = logging.getLogger(__name__)

NSE_EQUITY_URL = "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv"
NSE_HOME = "https://www.nseindia.com/market-data/securities-available-for-trading"
YAHOO_SEARCH = "https://query2.finance.yahoo.com/v1/finance/search"
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
REFRESH_SECONDS = 24 * 3600


@dataclass(frozen=True)
class Instrument:
    symbol: str      # bare NSE symbol, e.g. RELIANCE
    name: str
    exchange: str = "NSE"

    @property
    def yahoo(self) -> str:
        return f"{self.symbol}.NS" if self.exchange == "NSE" else f"{self.symbol}.BO"


class Universe:
    def __init__(self, data_dir: str = "data") -> None:
        self.dir = Path(data_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.cache_file = self.dir / "nse_equity.csv"
        # Inside the package, not in data/ — a mounted disk (Render, Railway,
        # docker volume) would shadow anything sitting in the data directory
        # and leave search dead on first boot.
        self.seed_file = Path(__file__).resolve().parent / "seed" / "nse_seed.csv"
        self._instruments: list[Instrument] = []
        self._by_symbol: dict[str, Instrument] = {}
        self._loaded_at: float = 0.0
        self.source = "seed"
        self.load()

    # ------------------------------------------------------------------ load
    def load(self) -> None:
        rows = self._read_cache() or self._read_seed()
        self._install(rows, "nse" if self.cache_file.exists() else "seed")

    def _install(self, rows: list[Instrument], source: str) -> None:
        self._instruments = rows
        self._by_symbol = {i.symbol.upper(): i for i in rows}
        self.source = source
        self._loaded_at = time.time()
        log.info("universe loaded: %d instruments (%s)", len(rows), source)

    def _read_seed(self) -> list[Instrument]:
        if not self.seed_file.exists():
            return []
        with self.seed_file.open(newline="", encoding="utf-8") as fh:
            return [
                Instrument(r["symbol"].strip().upper(), r["name"].strip())
                for r in csv.DictReader(fh)
                if r.get("symbol")
            ]

    def _read_cache(self) -> list[Instrument] | None:
        if not self.cache_file.exists():
            return None
        if time.time() - self.cache_file.stat().st_mtime > REFRESH_SECONDS:
            return None
        try:
            with self.cache_file.open(newline="", encoding="utf-8") as fh:
                rows = [
                    Instrument(r["symbol"].strip().upper(), r["name"].strip())
                    for r in csv.DictReader(fh)
                    if r.get("symbol")
                ]
            return rows or None
        except Exception as exc:  # noqa: BLE001
            log.warning("universe cache unreadable: %s", exc)
            return None

    async def refresh(self) -> bool:
        """Best-effort pull of the live NSE equity master. Never fatal — the
        seed list keeps the app usable if NSE refuses us."""
        if self.source == "nse" and time.time() - self._loaded_at < REFRESH_SECONDS:
            return False
        try:
            rows = await asyncio.to_thread(self._fetch_nse)
        except Exception as exc:  # noqa: BLE001
            log.warning("NSE equity master refresh failed: %s", exc)
            return False
        if not rows:
            return False
        merged = {i.symbol: i for i in self._read_seed()}
        merged.update({i.symbol: i for i in rows})
        ordered = sorted(merged.values(), key=lambda i: i.symbol)
        self._write_cache(ordered)
        self._install(ordered, "nse")
        return True

    def _fetch_nse(self) -> list[Instrument]:
        headers = {
            "User-Agent": BROWSER_UA,
            "Accept": "text/csv,*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": NSE_HOME,
        }
        with httpx.Client(timeout=30, headers=headers, follow_redirects=True) as c:
            c.get("https://www.nseindia.com")  # pick up the consent cookies
            resp = c.get(NSE_EQUITY_URL)
            resp.raise_for_status()
            text = resp.text
        out: list[Instrument] = []
        for row in csv.DictReader(io.StringIO(text)):
            clean = {k.strip().upper(): (v or "").strip() for k, v in row.items() if k}
            sym = clean.get("SYMBOL", "")
            series = clean.get("SERIES", "EQ")
            if not sym or series not in {"EQ", "BE"}:
                continue
            out.append(Instrument(sym.upper(), clean.get("NAME OF COMPANY", sym)))
        return out

    def _write_cache(self, rows: list[Instrument]) -> None:
        try:
            with self.cache_file.open("w", newline="", encoding="utf-8") as fh:
                w = csv.writer(fh)
                w.writerow(["symbol", "name"])
                w.writerows([(i.symbol, i.name) for i in rows])
        except Exception as exc:  # noqa: BLE001
            log.warning("could not persist universe cache: %s", exc)

    # ---------------------------------------------------------------- lookup
    @property
    def size(self) -> int:
        return len(self._instruments)

    def name_for(self, yahoo_symbol: str) -> str:
        bare = yahoo_symbol.upper().removesuffix(".NS").removesuffix(".BO")
        inst = self._by_symbol.get(bare)
        return inst.name if inst else ""

    def search(self, query: str, limit: int = 12) -> list[SearchResult]:
        q = query.strip().upper()
        if not q:
            return []

        scored: list[tuple[int, Instrument]] = []
        for inst in self._instruments:
            sym, name = inst.symbol, inst.name.upper()
            if sym == q:
                score = 1000
            elif sym.startswith(q):
                score = 820 - min(len(sym), 20)
            elif name.startswith(q):
                score = 700
            elif any(w.startswith(q) for w in name.split()):
                score = 600
            elif q in sym:
                score = 480
            elif q in name:
                score = 360
            else:
                continue
            scored.append((score - len(name) // 12, inst))

        scored.sort(key=lambda t: (-t[0], t[1].symbol))
        results = [
            SearchResult(
                symbol=i.yahoo, name=i.name, exchange=i.exchange, source="local"
            )
            for _s, i in scored[:limit]
        ]

        # Let a user paste a raw Yahoo ticker we have never heard of.
        if not results and ("." in q or q.isalnum()):
            guess = q if "." in q else f"{q}.NS"
            results.append(
                SearchResult(
                    symbol=guess,
                    name="(unlisted in local master — will be verified on add)",
                    exchange="NSE" if guess.endswith(".NS") else "BSE",
                    source="local",
                )
            )
        return results

    async def search_global(self, query: str, limit: int = 6) -> list[SearchResult]:
        """Yahoo's cross-exchange search, for names outside the NSE master."""
        params = {"q": query, "quotesCount": limit, "newsCount": 0}
        try:
            async with httpx.AsyncClient(
                timeout=8, headers={"User-Agent": BROWSER_UA}
            ) as c:
                resp = await c.get(YAHOO_SEARCH, params=params)
                resp.raise_for_status()
                quotes = resp.json().get("quotes", [])
        except Exception as exc:  # noqa: BLE001
            log.debug("yahoo search failed: %s", exc)
            return []
        out: list[SearchResult] = []
        for q in quotes:
            sym = q.get("symbol")
            if not sym or q.get("quoteType") not in {"EQUITY", "ETF", "INDEX"}:
                continue
            out.append(
                SearchResult(
                    symbol=sym,
                    name=q.get("longname") or q.get("shortname") or sym,
                    exchange=q.get("exchDisp") or q.get("exchange") or "",
                    source="yahoo",
                )
            )
        return out
