"""Pure-numeric helpers. No I/O, no globals — everything here is unit tested.

The whole module assumes a daily OHLCV frame indexed by a DatetimeIndex with
columns: open, high, low, close, volume (lower case).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

OHLCV = ["open", "high", "low", "close", "volume"]


def normalise(df: pd.DataFrame) -> pd.DataFrame:
    """Lower-case the columns, coerce to float, drop rows with no close."""
    out = df.copy()
    out.columns = [str(c).lower().strip() for c in out.columns]
    missing = [c for c in OHLCV if c not in out.columns]
    if missing:
        raise ValueError(f"missing OHLCV columns: {missing}")
    out = out[OHLCV].astype("float64")
    out = out[out["close"].notna() & (out["close"] > 0)]
    return out.sort_index()


def sma(series: pd.Series, length: int) -> pd.Series:
    return series.rolling(length, min_periods=length).mean()


def atr(df: pd.DataFrame, length: int = 14) -> pd.Series:
    prev_close = df["close"].shift(1)
    tr = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1 / length, adjust=False, min_periods=length).mean()


def last_valid(series: pd.Series) -> float | None:
    """Final non-NaN value, or None if the series never warmed up."""
    s = series.dropna()
    if s.empty:
        return None
    return float(s.iloc[-1])


# ---------------------------------------------------------------- swing pivots


@dataclass(frozen=True)
class Pivot:
    idx: int          # positional index into the frame
    price: float
    kind: str         # "high" | "low"

    @property
    def is_high(self) -> bool:
        return self.kind == "high"


def find_pivots(df: pd.DataFrame, window: int = 5) -> list[Pivot]:
    """Fractal swing points: a bar whose high is the highest in +/- `window`
    bars is a swing high (mirror for lows).

    Returned in chronological order and de-alternated, so the sequence always
    reads high, low, high, low, ... Consecutive same-kind pivots collapse to
    the most extreme one, which is what makes the contraction walk downstream
    trivial and stable.
    """
    if len(df) < window * 2 + 1:
        return []

    highs = df["high"].to_numpy(dtype="float64")
    lows = df["low"].to_numpy(dtype="float64")
    n = len(df)
    raw: list[Pivot] = []

    for i in range(window, n - window):
        lo_i, hi_i = i - window, i + window + 1
        seg_h = highs[lo_i:hi_i]
        seg_l = lows[lo_i:hi_i]
        # A bar counts only if it is the sole extreme in its neighbourhood.
        is_high = highs[i] == seg_h.max() and (seg_h == highs[i]).sum() == 1
        is_low = lows[i] == seg_l.min() and (seg_l == lows[i]).sum() == 1
        if is_high and is_low:
            continue  # an inside-out bar tells us nothing
        if is_high:
            raw.append(Pivot(i, float(highs[i]), "high"))
        elif is_low:
            raw.append(Pivot(i, float(lows[i]), "low"))

    pivots = _dealternate(raw)

    # The last `window` bars can never be confirmed by future bars — but that
    # is exactly where an in-progress final contraction lives, and that is the
    # contraction we most want to see. Extend the alternation into the tail
    # using its running extreme.
    if pivots:
        tail_from = pivots[-1].idx + 1
        if tail_from < n:
            if pivots[-1].is_high:
                j = int(np.argmin(lows[tail_from:])) + tail_from
                pivots.append(Pivot(j, float(lows[j]), "low"))
            else:
                j = int(np.argmax(highs[tail_from:])) + tail_from
                pivots.append(Pivot(j, float(highs[j]), "high"))

    return pivots


def _dealternate(pivots: list[Pivot]) -> list[Pivot]:
    """Collapse runs of same-kind pivots to their extreme."""
    out: list[Pivot] = []
    for p in pivots:
        if not out or out[-1].kind != p.kind:
            out.append(p)
            continue
        prev = out[-1]
        better = (p.price > prev.price) if p.is_high else (p.price < prev.price)
        if better:
            out[-1] = p
    return out


# ------------------------------------------------------------ relative strength


def relative_strength(
    close: pd.Series, benchmark: pd.Series | None, lookback: int = 252
) -> float:
    """Percentage out-performance of the stock vs its index over `lookback`
    bars. Positive means the stock beat the index. If no benchmark is
    available, fall back to the stock's own absolute return so the gate stays
    meaningful rather than silently passing everything.
    """
    if len(close) < 2:
        return 0.0
    span = min(lookback, len(close) - 1)
    stock_ret = float(close.iloc[-1] / close.iloc[-1 - span] - 1.0)

    if benchmark is None or len(benchmark) < 2:
        return round(stock_ret * 100, 2)

    bench = benchmark.reindex(close.index).ffill().dropna()
    if len(bench) < 2:
        return round(stock_ret * 100, 2)
    b_span = min(span, len(bench) - 1)
    bench_ret = float(bench.iloc[-1] / bench.iloc[-1 - b_span] - 1.0)
    return round((stock_ret - bench_ret) * 100, 2)


def pct(a: float, b: float) -> float:
    """(a - b) / b as a percentage, guarded against divide-by-zero."""
    if b == 0:
        return 0.0
    return (a - b) / b * 100.0
