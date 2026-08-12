"""Ground-truth tests for the strategy engine.

Run:  python -m unittest discover -s tests -v
No third-party test runner required.
"""

from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import IST  # noqa: E402
from app.strategy import (  # noqa: E402
    MomentumTracker,
    VCPParams,
    analyse,
    detect_vcp,
    evaluate_trend,
    find_pivots,
    normalise,
)
from tests import synth  # noqa: E402


class TestTrendTemplate(unittest.TestCase):
    def test_stage2_advance_passes(self):
        t = evaluate_trend(normalise(synth.textbook_vcp()))
        self.assertTrue(t.passed)
        self.assertTrue(t.sma_stacked, "50 > 150 > 200 should hold in an advance")
        self.assertTrue(t.sma200_rising)
        self.assertGreater(t.pct_above_52w_low, 25.0)

    def test_stage4_downtrend_fails_every_gate(self):
        t = evaluate_trend(normalise(synth.stage4_downtrend()))
        self.assertFalse(t.passed)
        self.assertFalse(t.above_sma200)
        self.assertFalse(t.sma_stacked)

    def test_empty_frame_does_not_crash(self):
        empty = pd.DataFrame(
            columns=["open", "high", "low", "close", "volume"],
            index=pd.DatetimeIndex([], name="date"),
        ).astype("float64")
        t = evaluate_trend(empty)
        self.assertFalse(t.passed)


class TestPivots(unittest.TestCase):
    def test_pivots_strictly_alternate(self):
        df = normalise(synth.textbook_vcp())
        pivots = find_pivots(df, window=5)
        self.assertGreater(len(pivots), 4)
        kinds = [p.kind for p in pivots]
        for a, b in zip(kinds, kinds[1:]):
            self.assertNotEqual(a, b, "pivot sequence must alternate high/low")
        idxs = [p.idx for p in pivots]
        self.assertEqual(idxs, sorted(idxs), "pivots must be chronological")

    def test_tail_pivot_is_reachable(self):
        """The final, unconfirmed contraction is the actionable one — the last
        pivot must sit inside the trailing window, not `window` bars back."""
        df = normalise(synth.textbook_vcp())
        pivots = find_pivots(df, window=5)
        self.assertGreaterEqual(pivots[-1].idx, len(df) - 6)


class TestVCPDetection(unittest.TestCase):
    def test_textbook_pattern_is_found_with_correct_depths(self):
        setup = analyse(synth.textbook_vcp(), "TEST.NS")
        self.assertTrue(setup.valid, setup.reason)
        depths = [c.depth_pct for c in setup.contractions]
        self.assertEqual(len(depths), 3)
        # Designed as 15% -> 8% -> 3%; allow a little for intrabar noise.
        for got, want in zip(depths, [15.0, 8.0, 3.0]):
            self.assertAlmostEqual(got, want, delta=0.6)
        # Monotonically tightening, which is the whole point of the pattern.
        self.assertTrue(all(a > b for a, b in zip(depths, depths[1:])))

    def test_pivot_and_stop_bracket_the_final_leg(self):
        setup = analyse(synth.textbook_vcp(), "TEST.NS")
        self.assertIsNotNone(setup.pivot)
        self.assertIsNotNone(setup.stop)
        self.assertGreater(setup.pivot, setup.stop)
        final = setup.contractions[-1]
        self.assertAlmostEqual(setup.pivot, final.high, places=2)
        self.assertAlmostEqual(setup.stop, final.low, places=2)
        self.assertLess(setup.risk_pct, 5.0, "a tight base implies tight risk")

    def test_widening_contractions_rejected(self):
        cs, meta = detect_vcp(normalise(synth.widening_base()))
        self.assertFalse(meta["valid"])
        self.assertIn("tightening", meta["reason"])

    def test_heavy_volume_into_final_leg_rejected(self):
        setup = analyse(synth.no_volume_dryup(), "TEST.NS")
        self.assertFalse(setup.valid)
        self.assertIn("dry-up", setup.reason)
        self.assertGreater(setup.volume_dryup, 0.75)

    def test_downtrend_rejected_before_pattern_is_even_considered(self):
        setup = analyse(synth.stage4_downtrend(), "TEST.NS")
        self.assertFalse(setup.valid)
        self.assertIn("Stage 2", setup.reason)
        self.assertEqual(setup.contractions, [])

    def test_insufficient_history_is_reported_not_raised(self):
        short = normalise(synth.textbook_vcp()).iloc[-20:]
        _cs, meta = detect_vcp(short)
        self.assertFalse(meta["valid"])
        self.assertIn("history", meta["reason"])

    def test_flat_series_does_not_divide_by_zero(self):
        n = 300
        flat = pd.DataFrame(
            {
                "open": np.full(n, 100.0),
                "high": np.full(n, 100.0),
                "low": np.full(n, 100.0),
                "close": np.full(n, 100.0),
                "volume": np.full(n, 1e6),
            },
            index=pd.bdate_range("2023-01-02", periods=n),
        )
        cs, meta = detect_vcp(normalise(flat))
        self.assertFalse(meta["valid"])
        self.assertEqual(cs, [])

    def test_score_rewards_the_tighter_of_two_valid_bases(self):
        tight = analyse(synth.textbook_vcp(), "TIGHT.NS")
        # Same price structure, heavy volume. Relax the volume gate so both
        # bases validate and the comparison isolates the scoring function.
        loose = analyse(
            synth.no_volume_dryup(),
            "LOOSE.NS",
            vcp_params=VCPParams(volume_dryup_ratio=1.5),
        )
        self.assertTrue(tight.valid)
        self.assertTrue(loose.valid, loose.reason)
        self.assertGreater(tight.score, loose.score)


class TestMomentumTracker(unittest.TestCase):
    def setUp(self):
        self.t0 = datetime(2026, 8, 12, 10, 0, tzinfo=IST)
        self.tracker = MomentumTracker(
            window_minutes=15, threshold_pct=2.0, cooldown_minutes=30
        )

    def feed(self, tracker, prices_at):
        last = None
        for minutes, price in prices_at:
            last = tracker.update("ABC.NS", price, self.t0 + timedelta(minutes=minutes))
        return last

    def test_fires_on_two_percent_inside_the_window(self):
        sig = self.feed(
            self.tracker,
            [(0, 100.0), (5, 100.4), (10, 100.9), (15, 102.5)],
        )
        self.assertIsNotNone(sig)
        self.assertAlmostEqual(sig.change_pct, 2.5, places=2)
        self.assertAlmostEqual(sig.reference_price, 100.0, places=2)

    def test_silent_below_threshold(self):
        sig = self.feed(self.tracker, [(0, 100.0), (8, 101.0), (16, 101.5)])
        self.assertIsNone(sig)

    def test_first_tick_cannot_fire(self):
        self.assertIsNone(self.tracker.update("ABC.NS", 100.0, self.t0))

    def test_move_that_predates_the_window_does_not_fire(self):
        """A stock up 3% two hours ago but flat since is not momentum."""
        sig = self.feed(
            self.tracker,
            [(0, 100.0), (5, 103.0), (25, 103.1), (40, 103.05), (55, 103.1)],
        )
        self.assertIsNone(sig)

    def test_cooldown_suppresses_a_second_alert_then_re_arms(self):
        self.assertIsNotNone(self.feed(self.tracker, [(0, 100.0), (15, 102.5)]))
        # Still climbing, but inside the 30-minute cooldown.
        self.assertIsNone(self.feed(self.tracker, [(20, 103.0), (35, 106.0)]))
        # Past the cooldown, a fresh qualifying move fires again.
        self.assertIsNotNone(self.feed(self.tracker, [(50, 106.0), (65, 109.0)]))

    def test_overnight_gap_does_not_fake_a_spike(self):
        """Yesterday 15:29 close vs today 09:15 open must never be compared."""
        tracker = MomentumTracker(window_minutes=15, threshold_pct=2.0)
        y = datetime(2026, 8, 11, 15, 29, tzinfo=IST)
        tracker.update("ABC.NS", 100.0, y)
        tomorrow = datetime(2026, 8, 12, 9, 15, tzinfo=IST)
        self.assertIsNone(tracker.update("ABC.NS", 108.0, tomorrow))

    def test_out_of_order_ticks_are_ignored(self):
        self.tracker.update("ABC.NS", 100.0, self.t0)
        self.tracker.update("ABC.NS", 101.0, self.t0 + timedelta(minutes=10))
        self.assertIsNone(
            self.tracker.update("ABC.NS", 999.0, self.t0 + timedelta(minutes=5))
        )
        self.assertEqual(self.tracker.tape_length("ABC.NS"), 2)

    def test_non_positive_price_ignored(self):
        self.assertIsNone(self.tracker.update("ABC.NS", 0.0, self.t0))
        self.assertEqual(self.tracker.tape_length("ABC.NS"), 0)

    def test_keep_only_drops_removed_symbols(self):
        self.tracker.update("ABC.NS", 100.0, self.t0)
        self.tracker.update("XYZ.NS", 50.0, self.t0)
        self.tracker.keep_only({"ABC.NS"})
        self.assertEqual(self.tracker.tape_length("XYZ.NS"), 0)
        self.assertEqual(self.tracker.tape_length("ABC.NS"), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
