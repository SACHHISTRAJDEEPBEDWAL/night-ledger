"""Deterministic synthetic OHLCV so the strategy has ground truth to be tested
against. Real market data is not reproducible; this is."""

from __future__ import annotations

import numpy as np
import pandas as pd

Leg = tuple[int, float, float]  # (bars, target_close, avg_volume)


def make_ohlcv(
    start_price: float,
    legs: list[Leg],
    seed: int = 7,
    start: str = "2023-01-02",
    noise: float = 0.003,
) -> pd.DataFrame:
    """Walk `start_price` through each leg's target over that leg's bar count.

    The exact turning points are pinned into the high/low of the pivot bar so a
    test can assert on contraction depth without fighting the noise.
    """
    rng = np.random.default_rng(seed)

    closes: list[float] = []
    turns: list[tuple[int, float, str]] = []
    price = start_price

    for bars, target, _vol in legs:
        direction = "high" if target >= price else "low"
        path = np.linspace(price, target, bars + 1)[1:]
        closes.extend(path.tolist())
        turns.append((len(closes) - 1, float(target), direction))
        price = target

    n = len(closes)
    close = np.asarray(closes, dtype="float64")

    volumes: list[float] = []
    for bars, _t, vol in legs:
        volumes.extend((rng.normal(vol, vol * 0.12, bars)).tolist())
    volume = np.clip(np.asarray(volumes, dtype="float64"), 1000, None)

    up = rng.uniform(0.0005, noise, n)
    dn = rng.uniform(0.0005, noise, n)
    high = close * (1 + up)
    low = close * (1 - dn)
    open_ = np.concatenate([[close[0] * 0.999], close[:-1]])
    open_ = np.clip(open_, low, high)

    # Pin the intended turning points so pivots land exactly where designed.
    for idx, target, kind in turns:
        if kind == "high":
            high[idx] = max(high[idx], target)
        else:
            low[idx] = min(low[idx], target)

    idx = pd.bdate_range(start=start, periods=n, name="date")
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": volume},
        index=idx,
    )


BASE_VOL = 1_000_000


def stage2_runup(bars: int = 260, start: float = 100.0, top: float = 300.0) -> list[Leg]:
    """A long, orderly advance that satisfies the trend template: staircase up
    with shallow pullbacks so every SMA stacks and the 200 keeps rising."""
    legs: list[Leg] = []
    price = start
    step = (top / start) ** (1 / 6)
    for _ in range(6):
        up = price * step
        legs.append((max(6, bars // 9), up, BASE_VOL * 1.2))
        pull = up * 0.94
        legs.append((max(4, bars // 18), pull, BASE_VOL * 0.8))
        price = pull
    legs.append((max(8, bars // 9), top, BASE_VOL * 1.4))
    return legs


def textbook_vcp() -> pd.DataFrame:
    """Stage 2 advance, then 15% -> 8% -> 3% contractions on drying volume."""
    legs = stage2_runup()
    legs += [
        (14, 255.0, BASE_VOL * 1.6),   # -15.0% from 300
        (12, 297.0, BASE_VOL * 1.0),
        (10, 273.2, BASE_VOL * 0.85),  # -8.0% from 297
        (9, 295.0, BASE_VOL * 0.7),
        (7, 286.2, BASE_VOL * 0.32),   # -3.0% from 295, volume dries up
        (3, 291.0, BASE_VOL * 0.30),
    ]
    return make_ohlcv(100.0, legs)


def widening_base() -> pd.DataFrame:
    """Contractions get *wider* left to right — the anti-pattern."""
    legs = stage2_runup()
    legs += [
        (10, 291.0, BASE_VOL * 0.9),   # -3%
        (9, 299.0, BASE_VOL * 0.9),
        (12, 275.1, BASE_VOL * 1.1),   # -8%
        (10, 297.0, BASE_VOL * 1.0),
        (14, 252.5, BASE_VOL * 1.3),   # -15%
        (4, 262.0, BASE_VOL * 1.2),
    ]
    return make_ohlcv(100.0, legs)


def no_volume_dryup() -> pd.DataFrame:
    """Correct price contractions, but volume stays heavy into the final leg."""
    legs = stage2_runup()
    legs += [
        (14, 255.0, BASE_VOL * 1.6),
        (12, 297.0, BASE_VOL * 1.5),
        (10, 273.2, BASE_VOL * 1.5),
        (9, 295.0, BASE_VOL * 1.6),
        (7, 286.2, BASE_VOL * 1.8),    # volume expanding, not exhausting
        (3, 291.0, BASE_VOL * 1.7),
    ]
    return make_ohlcv(100.0, legs)


def stage4_downtrend() -> pd.DataFrame:
    """Tight, pretty contractions inside a bear trend — must be rejected."""
    legs = [(30, 300.0, BASE_VOL)]
    price = 300.0
    for _ in range(6):
        dn = price * 0.86
        legs.append((18, dn, BASE_VOL * 1.3))
        bounce = dn * 1.05
        legs.append((10, bounce, BASE_VOL * 0.7))
        price = bounce
    legs += [
        (10, price * 0.90, BASE_VOL * 0.8),
        (8, price * 0.96, BASE_VOL * 0.5),
        (6, price * 0.935, BASE_VOL * 0.3),
        (3, price * 0.95, BASE_VOL * 0.3),
    ]
    return make_ohlcv(100.0, legs)
