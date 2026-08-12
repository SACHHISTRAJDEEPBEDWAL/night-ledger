"""Volatility Contraction Pattern detection.

The shape we are looking for, left to right:

    /\\                                  each pullback shallower than the last,
   /  \\      /\\                        volume drying up into the final one,
  /    \\    /  \\    /\\   <- pivot     price coiling under a clean buy point
 /      \\__/    \\__/  \\__

A base is only interesting if the stock already passed the Stage 2 trend
template, so `analyse` runs that gate first and short-circuits on failure.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from ..models import Contraction, VCPSetup
from .indicators import find_pivots, normalise, sma
from .trend import TrendParams, evaluate_trend, failure_reasons


@dataclass(frozen=True)
class VCPParams:
    min_contractions: int = 2
    max_contractions: int = 6
    max_first_contraction_pct: float = 35.0
    max_final_contraction_pct: float = 12.0
    min_contraction_pct: float = 1.0     # anything shallower is just noise
    shrink_ratio: float = 0.80           # each leg <= 80% of the previous
    min_base_days: int = 15
    max_base_days: int = 130
    volume_dryup_ratio: float = 0.75
    pivot_window: int = 5


@dataclass
class _Leg:
    high: float
    low: float
    depth: float
    start_idx: int
    end_idx: int
    start: str
    end: str
    avg_volume: float


def _legs(df: pd.DataFrame, pivot_window: int) -> list[_Leg]:
    """Every high -> low pullback in the frame, chronologically."""
    pivots = find_pivots(df, pivot_window)
    out: list[_Leg] = []
    for a, b in zip(pivots, pivots[1:]):
        if not (a.is_high and not b.is_high):
            continue
        if b.price >= a.price or a.price <= 0:
            continue
        seg = df.iloc[a.idx : b.idx + 1]
        out.append(
            _Leg(
                high=a.price,
                low=b.price,
                depth=(a.price - b.price) / a.price * 100.0,
                start_idx=a.idx,
                end_idx=b.idx,
                start=str(df.index[a.idx].date()),
                end=str(df.index[b.idx].date()),
                avg_volume=float(seg["volume"].mean()),
            )
        )
    return out


def _tightening_run(depths: list[float], ratio: float, max_n: int) -> tuple[int, int]:
    """Longest suffix of `depths` where every leg is meaningfully tighter than
    the one before it. Returns (start, end) inclusive positional bounds."""
    k = len(depths) - 1
    j = k
    while j > 0 and (k - j + 1) < max_n and depths[j] <= ratio * depths[j - 1]:
        j -= 1
    return j, k


def detect_vcp(
    df: pd.DataFrame, params: VCPParams | None = None
) -> tuple[list[Contraction], dict]:
    """Returns (contractions, meta). `meta['valid']` is the verdict and
    `meta['reason']` explains a rejection in plain English."""
    p = params or VCPParams()
    meta: dict = {"valid": False, "reason": "", "pivot": None, "stop": None}

    if len(df) < p.min_base_days + p.pivot_window * 2:
        meta["reason"] = "not enough price history"
        return [], meta

    window = df.iloc[-(p.max_base_days + p.pivot_window * 2) :]
    legs = [l for l in _legs(window, p.pivot_window) if l.depth >= p.min_contraction_pct]

    if len(legs) < p.min_contractions:
        meta["reason"] = f"only {len(legs)} usable pullback(s) in the base"
        return [], meta

    depths = [l.depth for l in legs]
    j, k = _tightening_run(depths, p.shrink_ratio, p.max_contractions)
    run = legs[j : k + 1]

    contractions = [
        Contraction(
            index=i + 1,
            high=round(l.high, 2),
            low=round(l.low, 2),
            depth_pct=round(l.depth, 2),
            start=l.start,
            end=l.end,
            bars=l.end_idx - l.start_idx + 1,
            avg_volume=round(l.avg_volume, 0),
        )
        for i, l in enumerate(run)
    ]

    if len(run) < p.min_contractions:
        meta["reason"] = "pullbacks are not progressively tightening"
        return contractions, meta

    first, final = run[0], run[-1]

    if first.depth > p.max_first_contraction_pct:
        meta["reason"] = f"base too deep — first leg fell {first.depth:.0f}%"
        return contractions, meta

    if final.depth > p.max_final_contraction_pct:
        meta["reason"] = f"final leg still loose at {final.depth:.0f}%"
        return contractions, meta

    base_days = len(window) - first.start_idx
    if base_days < p.min_base_days:
        meta["reason"] = f"base only {base_days} bars old"
        return contractions, meta

    # Volume exhaustion: sellers ran out of stock during the last coil.
    vol50 = sma(window["volume"], 50)
    avg50 = float(vol50.dropna().iloc[-1]) if vol50.notna().any() else float(
        window["volume"].mean()
    )
    dryup = final.avg_volume / avg50 if avg50 > 0 else 1.0
    meta["volume_dryup"] = round(dryup, 3)

    if dryup > p.volume_dryup_ratio:
        meta["reason"] = (
            f"no volume dry-up — final leg traded at {dryup:.0%} of the 50d average"
        )
        return contractions, meta

    meta.update(
        valid=True,
        reason="",
        pivot=round(final.high, 2),
        stop=round(final.low, 2),
        base_days=base_days,
        tightness_pct=round(final.depth, 2),
    )
    return contractions, meta


def score_setup(
    contractions: list[Contraction],
    tightness: float,
    dryup: float,
    rs_score: float,
    price: float,
    pivot: float,
) -> float:
    """0-100 ranking so the dashboard can sort the good ones to the top."""
    # Tighter final contraction is better (0% -> 30 pts, 12% -> 0 pts).
    tight_pts = max(0.0, 30.0 * (1 - min(tightness, 12.0) / 12.0))
    # Deeper volume dry-up is better (0.3x -> 25 pts, 0.75x -> 0 pts).
    dry_pts = max(0.0, 25.0 * (1 - (min(max(dryup, 0.3), 0.75) - 0.3) / 0.45))
    # Three or four contractions is the classic textbook count.
    n = len(contractions)
    count_pts = {2: 8.0, 3: 15.0, 4: 15.0, 5: 10.0, 6: 6.0}.get(n, 4.0)
    # Relative strength, capped so one monster runner cannot dominate.
    rs_pts = max(0.0, min(15.0, rs_score / 4.0))
    # Actionability: sitting right under the pivot beats being 10% away.
    dist = abs(pivot - price) / pivot * 100 if pivot else 100.0
    near_pts = max(0.0, 15.0 * (1 - min(dist, 10.0) / 10.0))
    return round(tight_pts + dry_pts + count_pts + rs_pts + near_pts, 1)


def analyse(
    raw: pd.DataFrame,
    symbol: str,
    name: str = "",
    benchmark: pd.Series | None = None,
    vcp_params: VCPParams | None = None,
    trend_params: TrendParams | None = None,
) -> VCPSetup:
    """Full pipeline for one symbol: Stage 2 gate, then pattern."""
    df = normalise(raw)
    as_of = str(df.index[-1].date()) if len(df) else ""

    trend = evaluate_trend(df, benchmark, trend_params)

    if not trend.passed:
        return VCPSetup(
            symbol=symbol,
            name=name,
            as_of=as_of,
            valid=False,
            reason="not in a Stage 2 uptrend: " + "; ".join(failure_reasons(trend)),
            trend=trend,
        )

    contractions, meta = detect_vcp(df, vcp_params)
    pivot = meta.get("pivot")
    stop = meta.get("stop")
    risk = None
    if pivot and stop and pivot > 0:
        risk = round((pivot - stop) / pivot * 100, 2)

    score = 0.0
    if meta["valid"] and pivot:
        score = score_setup(
            contractions,
            meta.get("tightness_pct", 0.0),
            meta.get("volume_dryup", 1.0),
            trend.rs_score,
            trend.price,
            pivot,
        )

    return VCPSetup(
        symbol=symbol,
        name=name,
        as_of=as_of,
        valid=bool(meta["valid"]),
        reason=meta.get("reason", ""),
        trend=trend,
        contractions=contractions,
        pivot=pivot,
        stop=stop,
        risk_pct=risk,
        base_days=meta.get("base_days", 0),
        tightness_pct=meta.get("tightness_pct"),
        volume_dryup=meta.get("volume_dryup"),
        score=score,
    )
