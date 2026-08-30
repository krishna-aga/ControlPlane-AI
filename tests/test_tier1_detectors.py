"""
Tier 1 detector test suite (grounding, toxicity).

Runs the REAL models (MiniLM, toxic-bert) - no mocking, since the whole point of these
two modules is the arithmetic done on real model output (worst-sentence selection, the
claim filter, the toxic-bert label-name alias). Slower than the rest of the suite by
design; that's the tradeoff for the T0-10-style bug this suite exists to catch (a
threshold, or here a label name, silently applied against the wrong thing).
"""

import unittest

from data_plane.detectors.grounding import score_grounding
from data_plane.detectors.toxicity import score_toxicity
from data_plane.input_gate import get_bundle

BUNDLE = get_bundle("bundles/customer_support_bundle.json")
CONTEXT = ["Company refund policy: customers may return unopened items within 30 days "
           "of purchase for a full refund. Opened items are not eligible."]


class TestGrounding(unittest.TestCase):

    def test_grounded_claim_scores_above_threshold(self):
        sim = score_grounding("Refunds are accepted within 30 days of purchase.", CONTEXT, BUNDLE)
        self.assertIsNotNone(sim)
        self.assertGreater(sim, BUNDLE["grounding_threshold"])

    def test_fabricated_claim_scores_low(self):
        sim = score_grounding(
            "We also offer free ponies with every order and a lifetime supply of glitter.",
            CONTEXT, BUNDLE)
        self.assertIsNotNone(sim)
        self.assertLess(sim, BUNDLE["grounding_threshold"])

    def test_worst_sentence_dominates_not_the_average(self):
        """T1-2: one fabricated sentence must not be diluted by correct text around it."""
        mixed = ("Refunds are accepted within 30 days of purchase. "
                 "We also offer free ponies with every order and a lifetime supply of glitter.")
        mixed_score = score_grounding(mixed, CONTEXT, BUNDLE)
        fabricated_alone = score_grounding(
            "We also offer free ponies with every order and a lifetime supply of glitter.",
            CONTEXT, BUNDLE)
        self.assertAlmostEqual(mixed_score, fabricated_alone, places=4)

    def test_non_claim_only_response_has_no_ground_truth(self):
        """§2A: 'Happy to help!' has nothing to check against - must be None, not 0.0."""
        self.assertIsNone(score_grounding("Happy to help! Anything else?", CONTEXT, BUNDLE))

    def test_no_context_is_none_not_zero(self):
        """§2E: absent from the max, never scored as the worst possible value."""
        self.assertIsNone(score_grounding("Refunds are accepted within 30 days.", [], BUNDLE))


class TestToxicity(unittest.TestCase):

    def test_clean_text_scores_near_zero(self):
        self.assertLess(score_toxicity("You are wonderful, have a great day!", BUNDLE), 0.1)

    def test_toxic_text_scores_high(self):
        self.assertGreater(
            score_toxicity("You are a worthless idiot and I will hurt you.", BUNDLE), 0.4)

    def test_worst_sentence_dominates_not_the_average(self):
        """§3D: one toxic sentence inside a long polite answer must not average away."""
        polite_wrapper = ("Thank you so much for reaching out today. "
                          "You are a worthless idiot and I will hurt you. "
                          "Have a wonderful rest of your day!")
        wrapped_score = score_toxicity(polite_wrapper, BUNDLE)
        alone_score = score_toxicity("You are a worthless idiot and I will hurt you.", BUNDLE)
        self.assertAlmostEqual(wrapped_score, alone_score, places=2)

    def test_label_names_are_correctly_aliased(self):
        """
        The live bug this suite exists to catch: unitary/toxic-bert emits Kaggle-style
        names (identity_hate), not the spec's Detoxify-style names (identity_attack).
        Matched blind, every weight lookup misses and this returns 0.0 forever.
        """
        score = score_toxicity("I hate people like you because of your religion.", BUNDLE)
        self.assertGreater(score, 0.05, "identity_hate -> identity_attack alias is not firing")

    def test_empty_text_is_zero_not_an_error(self):
        self.assertEqual(score_toxicity("", BUNDLE), 0.0)


if __name__ == "__main__":
    unittest.main()
