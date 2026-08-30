"""
Risk fusion test suite.

Cover is concentrated on the three defects from Part B of the locking register that this
module exists to fix: normalizer saturation (N1), the critical-threshold scale (N2), and
the fact that weights alone cannot make a detector act (P4).
"""

import unittest

from data_plane.fusion import fuse, normalize, score_t0
from data_plane.input_gate import get_bundle
from data_plane.models import DetectorSignals

BUNDLES = {
    "customer_support": "bundles/customer_support_bundle.json",
    "decision_support": "bundles/decision_support_bundle.json",
    "internal_copilot": "bundles/internal_copilot_bundle.json",
}


class TestPiecewiseNormalization(unittest.TestCase):
    """N1: min(1, P/T) discarded everything above the threshold."""

    THRESHOLDS = (0.5, 0.6, 0.7, 0.8, 0.95)

    def test_anchor_points_hold_for_every_threshold(self):
        for t in self.THRESHOLDS:
            with self.subTest(threshold=t):
                self.assertAlmostEqual(normalize(0.0, t), 0.0, places=9)
                self.assertAlmostEqual(normalize(t, t), 0.5, places=9)
                self.assertAlmostEqual(normalize(1.0, t), 1.0, places=9)

    def test_monotonic_and_in_range(self):
        for t in self.THRESHOLDS:
            with self.subTest(threshold=t):
                previous = -1.0
                for i in range(1001):
                    s = normalize(i / 1000.0, t)
                    self.assertGreaterEqual(s, previous)
                    self.assertGreaterEqual(s, 0.0)
                    self.assertLessEqual(s, 1.0)
                    previous = s

    def test_continuous_at_the_threshold(self):
        for t in self.THRESHOLDS:
            with self.subTest(threshold=t):
                gap = abs(normalize(t + 1e-9, t) - normalize(t - 1e-9, t))
                self.assertLess(gap, 1e-6)

    def test_saturation_is_gone(self):
        """The whole point: 0.70 and 0.99 must no longer be the same number."""
        t = 0.7
        self.assertEqual(min(1.0, 0.70 / t), min(1.0, 0.99 / t))   # old scheme collapsed
        self.assertNotAlmostEqual(normalize(0.70, t), normalize(0.99, t), places=2)
        self.assertAlmostEqual(normalize(0.80, t), 0.667, places=3)
        self.assertAlmostEqual(normalize(0.99, t), 0.983, places=3)

    def test_degenerate_thresholds_do_not_divide_by_zero(self):
        self.assertEqual(normalize(0.0, 0.0), 0.0)
        self.assertEqual(normalize(0.5, 0.0), 1.0)
        self.assertAlmostEqual(normalize(0.6, 1.0), 0.3, places=9)


class TestGroundingDirection(unittest.TestCase):
    """
    Grounding is risk-INVERTED: cosine similarity means higher = safer. Fusion
    normalizes ungroundedness so its direction matches every other detector.
    """

    def test_matches_the_registers_verified_table(self):
        bundle = get_bundle(BUNDLES["customer_support"])          # grounding_threshold 0.6
        expected = {0.95: 0.0625, 0.80: 0.2500, 0.60: 0.5000, 0.30: 0.7500, 0.10: 0.9167}
        for similarity, s in expected.items():
            with self.subTest(similarity=similarity):
                result = fuse(DetectorSignals(grounding_similarity=similarity), bundle)
                self.assertAlmostEqual(result.normalized["grounding"], s, places=4)

    def test_similarity_at_threshold_is_exactly_the_midpoint(self):
        bundle = get_bundle(BUNDLES["customer_support"])
        result = fuse(DetectorSignals(grounding_similarity=bundle["grounding_threshold"]), bundle)
        self.assertAlmostEqual(result.normalized["grounding"], 0.5, places=9)


class TestCriticalFloors(unittest.TestCase):
    """
    P4: weights alone cannot make a detector act. The register measured toxicity at
    P=0.99 with nothing else firing fusing to 0.197 -> ALLOW under every persona.
    """

    def test_single_detector_at_critical_escalates(self):
        for name, path in BUNDLES.items():
            with self.subTest(bundle=name):
                bundle = get_bundle(path)
                result = fuse(DetectorSignals(toxicity_probability=0.99), bundle)
                self.assertIn("toxicity", result.critical_fired)
                self.assertEqual(result.action, "BLOCK")

    def test_below_critical_does_not_floor(self):
        bundle = get_bundle(BUNDLES["customer_support"])
        result = fuse(DetectorSignals(toxicity_probability=0.80), bundle)
        self.assertEqual(result.critical_fired, [])
        self.assertNotEqual(result.action, "BLOCK")

    def test_floor_landing_exactly_on_high_band_still_blocks(self):
        """
        The band comparison must be >=, not >. A floor sets fused risk to exactly
        high_band; with a strict > it would fall through to the graded middle.
        """
        bundle = get_bundle(BUNDLES["customer_support"])
        result = fuse(DetectorSignals(toxicity_probability=1.0, grounding_similarity=0.95), bundle)
        self.assertGreaterEqual(result.fused_risk, result.effective_high_band)
        self.assertEqual(result.action, "BLOCK")

    def test_critical_survives_a_zeroed_weight(self):
        """
        Safety must not depend on weight arithmetic - that was the argument for floors
        over weight floors. Zeroing toxicity's weight must not disable toxicity.
        """
        bundle = get_bundle(BUNDLES["customer_support"])
        bundle["detector_weights"] = {"t0": 1.0, "pii": 0.0, "grounding": 0.0, "toxicity": 0.0}
        result = fuse(DetectorSignals(toxicity_probability=0.99), bundle)
        self.assertEqual(result.action, "BLOCK")


class TestTier0Scoring(unittest.TestCase):
    """T0-2: T0 is categorical and must not be run through the piecewise normalizer."""

    def test_severity_lookup_uses_the_bundle_table(self):
        bundle = get_bundle(BUNDLES["customer_support"])
        table = bundle["t0_severity_scores"]
        for severity, expected in table.items():
            with self.subTest(severity=severity):
                self.assertAlmostEqual(score_t0([severity], bundle), expected, places=9)

    def test_max_aggregation_takes_the_worst_finding(self):
        bundle = get_bundle(BUNDLES["customer_support"])
        bundle["t0_aggregation"] = "max"
        self.assertAlmostEqual(score_t0(["low", "hard", "medium"], bundle), 1.0, places=9)

    def test_noisy_or_compounds_low_severity_findings(self):
        bundle = get_bundle(BUNDLES["customer_support"])
        bundle["t0_aggregation"] = "noisy_or"
        single = score_t0(["low"], bundle)
        many = score_t0(["low"] * 5, bundle)
        self.assertGreater(many, single)
        self.assertLessEqual(many, 1.0)

    def test_no_findings_scores_zero_and_is_not_weighted(self):
        bundle = get_bundle(BUNDLES["customer_support"])
        result = fuse(DetectorSignals(toxicity_probability=0.5), bundle)
        self.assertNotIn("t0", result.normalized)
        self.assertNotIn("t0", result.weights_applied)


class TestWeightRenormalization(unittest.TestCase):

    def test_absent_detector_is_dropped_not_scored_zero(self):
        """
        A non-RAG request has no grounding signal. Scoring it 0.0 would carry its weight
        as dead mass and dilute every other detector.
        """
        bundle = get_bundle(BUNDLES["customer_support"])
        result = fuse(DetectorSignals(pii_confidence=0.9, toxicity_probability=0.5), bundle)
        self.assertNotIn("grounding", result.weights_applied)
        self.assertAlmostEqual(sum(result.weights_applied.values()), 1.0, places=9)

    def test_applied_weights_always_sum_to_one(self):
        bundle = get_bundle(BUNDLES["customer_support"])
        cases = [
            DetectorSignals(pii_confidence=0.5),
            DetectorSignals(t0_severities=["high"], toxicity_probability=0.2),
            DetectorSignals(t0_severities=["low"], pii_confidence=0.4,
                            grounding_similarity=0.7, toxicity_probability=0.3),
        ]
        for i, signals in enumerate(cases):
            with self.subTest(case=i):
                self.assertAlmostEqual(sum(fuse(signals, bundle).weights_applied.values()), 1.0, places=9)

    def test_no_signals_at_all_is_allow(self):
        bundle = get_bundle(BUNDLES["customer_support"])
        result = fuse(DetectorSignals(), bundle)
        self.assertEqual(result.fused_risk, 0.0)
        self.assertEqual(result.action, "ALLOW")


class TestInputRiskTightening(unittest.TestCase):
    """
    This is what `injection_action: flag` actually does. Without it, flag and allow are
    behaviourally identical.
    """

    SIGNALS = dict(t0_severities=["medium"], pii_confidence=0.85, toxicity_probability=0.4)

    def test_flagged_input_escalates_an_identical_output(self):
        bundle = get_bundle(BUNDLES["customer_support"])
        clean = fuse(DetectorSignals(**self.SIGNALS), bundle)
        flagged = fuse(DetectorSignals(**self.SIGNALS, input_flagged=True, injection_risk=1.0), bundle)

        self.assertAlmostEqual(clean.fused_risk, flagged.fused_risk, places=9)
        self.assertFalse(clean.bands_tightened)
        self.assertTrue(flagged.bands_tightened)
        self.assertEqual(clean.action, "REDACT")
        self.assertEqual(flagged.action, "BLOCK")

    def test_tightening_is_proportional_to_injection_risk(self):
        bundle = get_bundle(BUNDLES["customer_support"])
        result = fuse(
            DetectorSignals(input_flagged=True, injection_risk=1.0, pii_confidence=0.1), bundle
        )
        factor = 1.0 - bundle["input_risk_tightening"]
        self.assertAlmostEqual(result.effective_high_band, bundle["high_band"] * factor, places=9)
        self.assertAlmostEqual(result.effective_low_band, bundle["low_band"] * factor, places=9)

    def test_unflagged_input_leaves_bands_untouched(self):
        bundle = get_bundle(BUNDLES["customer_support"])
        result = fuse(DetectorSignals(injection_risk=0.9, input_flagged=False), bundle)
        self.assertFalse(result.bands_tightened)
        self.assertAlmostEqual(result.effective_high_band, bundle["high_band"], places=9)


class TestActionLadder(unittest.TestCase):
    """Band selects severity; dominant detector selects the remedy."""

    def test_maskable_risk_redacts(self):
        bundle = get_bundle(BUNDLES["customer_support"])
        result = fuse(DetectorSignals(pii_confidence=0.85), bundle)
        self.assertEqual(result.dominant_detector, "pii")
        self.assertEqual(result.action, "REDACT")

    def test_ungrounded_answer_regenerates(self):
        """Masking cannot ground an unsupported answer, so the remedy is a retry."""
        bundle = get_bundle(BUNDLES["customer_support"])
        result = fuse(DetectorSignals(grounding_similarity=0.55), bundle)
        self.assertEqual(result.dominant_detector, "grounding")
        self.assertEqual(result.action, "REGENERATE")

    def test_toxicity_in_the_middle_band_flags(self):
        bundle = get_bundle(BUNDLES["customer_support"])
        result = fuse(DetectorSignals(toxicity_probability=0.75), bundle)
        self.assertEqual(result.dominant_detector, "toxicity")
        self.assertEqual(result.action, "FLAG")

    def test_below_low_band_allows(self):
        bundle = get_bundle(BUNDLES["customer_support"])
        result = fuse(DetectorSignals(toxicity_probability=0.2), bundle)
        self.assertEqual(result.action, "ALLOW")

    def test_every_action_is_on_the_declared_ladder(self):
        ladder = {"ALLOW", "REDACT", "REGENERATE", "FLAG", "BLOCK"}
        bundle = get_bundle(BUNDLES["customer_support"])
        for p in (0.0, 0.2, 0.5, 0.75, 0.9, 1.0):
            for sim in (None, 0.9, 0.5, 0.1):
                signals = DetectorSignals(toxicity_probability=p, grounding_similarity=sim)
                self.assertIn(fuse(signals, bundle).action, ladder)


class TestT2Gating(unittest.TestCase):

    def test_t2_only_inside_the_graded_band_and_only_when_enabled(self):
        cs = get_bundle(BUNDLES["customer_support"])       # t2_enabled: false
        ds = get_bundle(BUNDLES["decision_support"])       # t2_enabled: true

        self.assertFalse(fuse(DetectorSignals(toxicity_probability=0.75), cs).t2_recommended)

        middle = fuse(DetectorSignals(toxicity_probability=0.75), ds)
        self.assertTrue(ds["low_band"] <= middle.fused_risk <= ds["high_band"])
        self.assertTrue(middle.t2_recommended)

        self.assertFalse(fuse(DetectorSignals(toxicity_probability=0.01), ds).t2_recommended)


class TestLedgerProjection(unittest.TestCase):

    def test_raw_scores_are_persisted_for_calibration(self):
        """
        N1a: a calibration sweep needs the raw detector output. Storing only normalized
        scores makes the counterfactual unreconstructable without re-running every
        detector over historical traffic.
        """
        bundle = get_bundle(BUNDLES["customer_support"])
        row = fuse(DetectorSignals(pii_confidence=0.91, toxicity_probability=0.44), bundle).ledger_row()
        self.assertAlmostEqual(row["raw_scores"]["pii"], 0.91, places=9)
        self.assertAlmostEqual(row["raw_scores"]["toxicity"], 0.44, places=9)
        self.assertIn("normalized_scores", row)
        self.assertIn("effective_bands", row)


if __name__ == "__main__":
    unittest.main()
