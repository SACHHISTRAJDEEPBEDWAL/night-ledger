"""Minervini's Trend Template — the Stage 2 gate.

Nothing else in the system runs until a stock clears this. A beautiful chart
pattern on a stock in a downtrend is a bull trap, so the ordering matters:
trend first, pattern second.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from ..models import TrendTemplate
from .indicators import last_valid, pct, relative_strength, sma


@dataclass(frozen=True)
class TrendParams:
    min_pct_above_52w_low: float = 25.0
    max_pct_below_52w_high: float = 25.0
    sma200_slope_lookback: int = 22
    min_rs_score: float = 0.0
    weeks52_bars: int = 252


FAILED = TrendTemplate(
    above_sma50=False,
    above_sma150=False,
    above_sma200=False,
    sma_stacked=False,
    sma200_rising=False,
    above_52w_low=False,
    near_52w_high=False,
    rs_ok=False,
    passed=False,
    price=0.0,
    sma50=None,
    sma150=None,
    sma200=None,
    pct_above_52w_low=0.0,
    pct_below_52w_high=0.0,
    rs_score=0.0,
)


def evaluate_trend(
    df: pd.DataFrame,
    benchmark: pd.Series | None = None,
    params: TrendParams | None = None,
) -> TrendTemplate:
    p = params or TrendParams()

    if df.empty:
        return FAILED.model_copy()

    close = df["close"]
    price = float(close.iloc[-1])

    s50 = sma(close, 50)
    s150 = sma(close, 150)
    s200 = sma(close, 200)

    v50, v150, v200 = last_valid(s50), last_valid(s150), last_valid(s200)

    above_50 = v50 is not None and price > v50
    above_150 = v150 is not None and price > v150
    above_200 = v200 is not None and price > v200
    stacked = None not in (v50, v150, v200) and v50 > v150 > v200

    # 200 SMA must be higher than it was ~a month ago.
    rising_200 = False
    s200_clean = s200.dropna()
    if len(s200_clean) > p.sma200_slope_lookback:
        rising_200 = bool(
            s200_clean.iloc[-1] > s200_clean.iloc[-1 - p.sma200_slope_lookback]
        )

    window = close.iloc[-p.weeks52_bars :] if len(close) > p.weeks52_bars else close
    lo52 = float(window.min())
    hi52 = float(window.max())
    pct_above_low = pct(price, lo52)
    pct_below_high = pct(hi52, price)

    above_low = pct_above_low >= p.min_pct_above_52w_low
    near_high = pct_below_high <= p.max_pct_below_52w_high

    rs = relative_strength(close, benchmark, lookback=p.weeks52_bars)
    rs_ok = rs >= p.min_rs_score

    passed = all(
        [
            above_50,
            above_150,
            above_200,
            stacked,
            rising_200,
            above_low,
            near_high,
            rs_ok,
        ]
    )

    return TrendTemplate(
        above_sma50=above_50,
        above_sma150=above_150,
        above_sma200=above_200,
        sma_stacked=bool(stacked),
        sma200_rising=rising_200,
        above_52w_low=above_low,
        near_52w_high=near_high,
        rs_ok=rs_ok,
        passed=bool(passed),
        price=round(price, 2),
        sma50=round(v50, 2) if v50 else None,
        sma150=round(v150, 2) if v150 else None,
        sma200=round(v200, 2) if v200 else None,
        pct_above_52w_low=round(pct_above_low, 2),
        pct_below_52w_high=round(pct_below_high, 2),
        rs_score=rs,
    )


def failure_reasons(t: TrendTemplate) -> list[str]:
    """Human-readable list of which template rules the stock missed."""
    checks = [
        (t.above_sma50, "price below 50 SMA"),
        (t.above_sma150, "price below 150 SMA"),
        (t.above_sma200, "price below 200 SMA"),
        (t.sma_stacked, "SMAs not stacked 50 > 150 > 200"),
        (t.sma200_rising, "200 SMA not rising"),
        (t.above_52w_low, f"only {t.pct_above_52w_low:.0f}% above 52w low"),
        (t.near_52w_high, f"{t.pct_below_52w_high:.0f}% below 52w high"),
        (t.rs_ok, f"lagging the index by {abs(t.rs_score):.0f}%"),
    ]
    return [msg for ok, msg in checks if not ok]
