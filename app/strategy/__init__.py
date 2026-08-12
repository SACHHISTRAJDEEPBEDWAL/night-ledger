"""Strategy engine: Stage 2 trend template, VCP detection, momentum trigger.

Deliberately free of I/O and framework imports so it can be unit tested and
back-tested on its own.
"""

from __future__ import annotations

from .indicators import Pivot, atr, find_pivots, normalise, relative_strength, sma
from .momentum import MomentumSignal, MomentumTracker
from .trend import TrendParams, evaluate_trend, failure_reasons
from .vcp import VCPParams, analyse, detect_vcp, score_setup

__all__ = [
    "Pivot",
    "atr",
    "find_pivots",
    "normalise",
    "relative_strength",
    "sma",
    "MomentumSignal",
    "MomentumTracker",
    "TrendParams",
    "evaluate_trend",
    "failure_reasons",
    "VCPParams",
    "analyse",
    "detect_vcp",
    "score_setup",
    "params_from_settings",
]


def params_from_settings(settings) -> tuple[VCPParams, TrendParams]:
    """Build strategy params from the app config object."""
    vcp = VCPParams(
        min_contractions=settings.min_contractions,
        max_contractions=settings.max_contractions,
        max_first_contraction_pct=settings.max_first_contraction_pct,
        max_final_contraction_pct=settings.max_final_contraction_pct,
        shrink_ratio=settings.contraction_shrink_ratio,
        min_base_days=settings.min_base_days,
        max_base_days=settings.max_base_days,
        volume_dryup_ratio=settings.volume_dryup_ratio,
        pivot_window=settings.pivot_window,
    )
    trend = TrendParams(
        min_pct_above_52w_low=settings.min_pct_above_52w_low,
        max_pct_below_52w_high=settings.max_pct_below_52w_high,
        sma200_slope_lookback=settings.sma200_slope_lookback,
        min_rs_score=settings.min_rs_score,
    )
    return vcp, trend
