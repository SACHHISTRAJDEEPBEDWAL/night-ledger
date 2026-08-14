"""Wire formats shared by the scanner, the REST API and the dashboard."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field


class AlertKind(str, Enum):
    VCP_SETUP = "vcp_setup"        # a valid VCP base has formed; watch the pivot
    BREAKOUT = "breakout"          # price took out the pivot on volume
    MOMENTUM = "momentum"          # +N% inside the rolling intraday window
    SYSTEM = "system"              # scanner lifecycle / errors


class Contraction(BaseModel):
    """One leg of the volatility contraction pattern."""

    index: int
    high: float
    low: float
    depth_pct: float
    start: str                     # ISO date of the swing high
    end: str                       # ISO date of the swing low
    bars: int
    avg_volume: float


class TrendTemplate(BaseModel):
    """Minervini's Stage 2 gate. `passed` is the AND of every check."""

    above_sma50: bool
    above_sma150: bool
    above_sma200: bool
    sma_stacked: bool              # 50 > 150 > 200
    sma200_rising: bool
    above_52w_low: bool
    near_52w_high: bool
    rs_ok: bool
    passed: bool

    price: float
    sma50: float | None
    sma150: float | None
    sma200: float | None
    pct_above_52w_low: float
    pct_below_52w_high: float
    rs_score: float


class VCPSetup(BaseModel):
    symbol: str
    name: str = ""
    as_of: str
    valid: bool
    reason: str = ""
    trend: TrendTemplate
    contractions: list[Contraction] = Field(default_factory=list)
    pivot: float | None = None            # buy point: high of the final leg
    stop: float | None = None             # low of the final leg
    risk_pct: float | None = None         # pivot -> stop, as % of pivot
    base_days: int = 0
    tightness_pct: float | None = None    # final-leg depth
    volume_dryup: float | None = None     # final-leg vol / 50d avg vol
    score: float = 0.0                    # 0-100 quality ranking


class Alert(BaseModel):
    id: str
    kind: AlertKind
    symbol: str
    name: str = ""
    ts: datetime
    price: float
    headline: str
    detail: str = ""
    change_pct: float | None = None
    pivot: float | None = None
    stop: float | None = None
    payload: dict = Field(default_factory=dict)


class WatchItem(BaseModel):
    symbol: str                    # Yahoo-style, e.g. RELIANCE.NS
    name: str = ""
    exchange: str = "NSE"
    added_at: datetime


class SearchResult(BaseModel):
    symbol: str
    name: str
    exchange: str
    source: Literal["local", "yahoo"] = "local"


class Quote(BaseModel):
    symbol: str
    price: float
    change_pct: float | None = None
    ts: datetime
    stale: bool = False


class DataHealth(BaseModel):
    """Did the last screening pass actually get any price data?

    Without this the dashboard cannot tell "no setups found" apart from "the
    data provider refused us" — and those need very different reactions.
    Yahoo throttles datacenter IPs, so on a cloud host the second case is
    common and must not look like the first.
    """

    requested: int = 0
    fetched: int = 0
    valid_setups: int = 0
    missing: list[str] = Field(default_factory=list)
    at: datetime | None = None
    blocked: bool = False


class Diagnostic(BaseModel):
    """Result of manually probing the price feed from the dashboard."""

    ok: bool
    summary: str
    probe: str = ""
    bars: int = 0
    latency_ms: int = 0
    last_close: float | None = None
    last_bar_date: str = ""
    error: str = ""


class ScannerStatus(BaseModel):
    running: bool
    feed: str
    live_ticks: bool
    market_phase: str              # closed | pre_open | open | post_close
    server_time_ist: str
    watchlist_size: int
    last_momentum_scan: datetime | None = None
    last_vcp_scan: datetime | None = None
    alerts_today: int = 0
    errors: list[str] = Field(default_factory=list)
    data_health: DataHealth | None = None
