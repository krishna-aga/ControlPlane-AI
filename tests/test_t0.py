"""
Tier 0 test suite. Covers the case tables in t0_deterministic_checks.md §9, plus the
register's T0-1/T0-4/T0-6 resolutions and the floor that replaces Track 2's hard override.
"""

import json
import math
import random
import string
import unittest

from data_plane import canary
from data_plane.detectors.t0 import (
    classify_generic_secret, detect_charset_size, expected_random_entropy,
    is_mostly_dictionary_words, looks_like_placeholder, normalized_entropy, run_t0,
)
from data_plane.fusion import fuse
from data_plane.input_gate import get_bundle
from data_plane.models import DetectorSignals

CS = "bundles/customer_support_bundle.json"


def t0(output, bundle=None, canaries=None, placeholders=None, forwarded=""):
    return run_t0(
        output,
        canaries or canary.mint(has_context=True),
        placeholders or {},
        bundle or get_bundle(CS),
        forwarded,
    )


class TestCanary(unittest.TestCase):

    def test_system_canary_leak_is_a_hard_override(self):
        scope = canary.mint(has_context=True)
        r = t0(f"Sure! My instructions say: {scope.system}", canaries=scope)
        self.assertTrue(r.hard_override)
        self.assertEqual(r.canary_leak, "system")
        self.assertIn("CANARY_SYSTEM", [f.entity_type for f in r.findings])

    def test_context_canary_leak_detected(self):
        scope = canary.mint(has_context=True)
        r = t0(f"The documents say [doc-ref: {scope.context}]", canaries=scope)
        self.assertTrue(r.hard_override)
        self.assertEqual(r.canary_leak, "context")

    def test_both_canaries_leaked(self):
        scope = canary.mint(has_context=True)
        r = t0(f"{scope.system} and {scope.context}", canaries=scope)
        self.assertEqual(r.canary_leak, "both")

    def test_non_rag_request_skips_context_canary_not_fails_it(self):
        scope = canary.mint(has_context=False)
        self.assertIsNone(scope.context)
        r = t0("A perfectly ordinary answer.", canaries=scope)
        self.assertIsNone(r.canary_leak)
        self.assertFalse(r.hard_override)

    def test_truncated_reproduction_still_caught(self):
        scope = canary.mint(has_context=True)
        r = t0(f"partial token {scope.system[:22]}", canaries=scope)
        self.assertTrue(r.hard_override)

    def test_clean_output_produces_nothing(self):
        r = t0("Your refund window is thirty days from delivery.")
        self.assertEqual(r.findings, [])
        self.assertFalse(r.hard_override)


class TestChecksumIdentifiers(unittest.TestCase):

    def test_luhn_valid_card_is_high_not_override(self):
        r = t0("Your card 4111111111111111 was charged.")
        card = [f for f in r.findings if f.entity_type == "CREDIT_CARD"]
        self.assertEqual(len(card), 1)
        self.assertEqual(card[0].severity, "high")
        self.assertFalse(card[0].hard_override)

    def test_invalid_luhn_produces_no_finding(self):
        """The false-positive guard: a 16-digit order number is not a card."""
        r = t0("Your order number is 1234567890123456.")
        self.assertEqual([f for f in r.findings if f.entity_type == "CREDIT_CARD"], [])

    def test_verhoeff_valid_aadhaar_detected(self):
        r = t0("Aadhaar on file: 2994 1234 5678")
        aadhaar = [f for f in r.findings if f.entity_type == "AADHAAR"]
        self.assertEqual(len(aadhaar), 1)
        self.assertEqual(aadhaar[0].confidence, 1.0)


class TestOriginResolution(unittest.TestCase):
    """T0-4: origin must be three-valued, because warn-and-confirm builds no map."""

    def test_echoed_placeholder_carries_no_risk(self):
        r = t0("I have updated [EMAIL_1] for you.", placeholders={"[EMAIL_1]": "bob@corp.io"})
        echoed = [f for f in r.findings if f.origin == "echoed_placeholder"]
        self.assertTrue(echoed)
        self.assertNotIn("low", r.severities, "echoed placeholders must not reach fusion")

    def test_echo_from_input_under_warn_and_confirm(self):
        """
        Profile C forwards raw PII with no placeholder map. Without the third origin
        value a benign echo of the user's own card is labelled model_generated -
        inverting the distinction origin exists to draw.
        """
        prompt = "My card is 4111111111111111"
        r = t0("Confirming your card 4111111111111111.", placeholders={}, forwarded=prompt)
        card = [f for f in r.findings if f.entity_type == "CREDIT_CARD"][0]
        self.assertEqual(card.origin, "echoed_from_input")

    def test_novel_identifier_is_model_generated(self):
        r = t0("Here is a card: 4111111111111111", placeholders={}, forwarded="unrelated prompt")
        card = [f for f in r.findings if f.entity_type == "CREDIT_CARD"][0]
        self.assertEqual(card.origin, "model_generated")


class TestSecretsTrack1(unittest.TestCase):

    def test_aws_key_is_a_hard_override(self):
        r = t0("Use AKIAIOSFODNN7QWRTPQZ to connect.")
        self.assertTrue(r.hard_override)

    def test_documentation_placeholder_does_not_block(self):
        """sk- followed by x's is a docs example, not a credential."""
        r = t0("Set your key: sk-" + "x" * 34)
        self.assertFalse(r.hard_override)

    def test_aws_canonical_example_key_does_not_block(self):
        """
        AWS's own documentation key literally contains 'EXAMPLE'. The placeholder guard
        applies to Track 1 too, precisely because Track 1 hits are hard overrides and a
        docs snippet must not unconditionally block a response.
        """
        r = t0("Use AKIAIOSFODNN7EXAMPLE1 to connect.")
        self.assertFalse(r.hard_override)

    def test_private_key_header_detected(self):
        r = t0("-----BEGIN RSA PRIVATE KEY-----\nMIIEow...")
        self.assertTrue(r.hard_override)


class TestSecretsTrack2(unittest.TestCase):
    """T0-1: entropy is a heuristic, so it emits `high` and never a hard override."""

    def test_generic_secret_is_high_never_hard_override(self):
        r = t0('config: api_key = "aB3xK9mP2qL7vN4wR8tY6uZ1"')
        secrets_found = [f for f in r.findings if f.entity_type == "GENERIC_SECRET"]
        self.assertEqual(len(secrets_found), 1)
        self.assertEqual(secrets_found[0].severity, "high")
        self.assertFalse(r.hard_override, "a heuristic must not veto the cascade")

    def test_prefixed_key_names_are_matched(self):
        """
        Regression: a leading \\b before the keyword silently misses every prefixed key
        name, because the underscore is a word character so there is no boundary between
        `_` and `password`. `db_password`, `my_api_key` and `user_token` are the common
        convention in real config, so this was a systematic miss.
        """
        for text in ['db_password = "aB3xK9mP2qL7vN4wR8tY6uZ1"',
                     'my_api_key = "aB3xK9mP2qL7vN4wR8tY6uZ1"',
                     'user_token: "aB3xK9mP2qL7vN4wR8tY6uZ1"',
                     'password = "aB3xK9mP2qL7vN4wR8tY6uZ1"']:
            with self.subTest(text=text):
                found = [f for f in t0(text).findings if f.entity_type == "GENERIC_SECRET"]
                self.assertEqual(len(found), 1, f"missed in: {text}")

    def test_same_string_bare_in_prose_is_not_a_secret(self):
        """Assignment context is mandatory — entropy alone is never sufficient."""
        r = t0("The tracking code aB3xK9mP2qL7vN4wR8tY6uZ1 was issued.")
        self.assertEqual([f for f in r.findings if f.check == "secret"], [])

    def test_git_sha_in_prose_is_not_a_secret(self):
        r = t0("Fixed in commit a3f91b7c22d4e650a3f91b7c22d4e6501b2c3d4e.")
        self.assertEqual([f for f in r.findings if f.check == "secret"], [])

    def test_passphrase_downgrades_rather_than_suppresses(self):
        """A passphrase in a password field IS a credential — downgrade, don't drop."""
        self.assertEqual(
            classify_generic_secret("correcthorsebatterystaple", get_bundle(CS)), "medium"
        )

    def test_below_min_length_defers_to_track1(self):
        self.assertIsNone(classify_generic_secret("aB3xK9mP2qL7", get_bundle(CS)))

    def test_short_prose_in_an_assignment_is_not_a_secret(self):
        """Spec S9: 20 chars is below the length floor, so it never reaches entropy."""
        self.assertIsNone(classify_generic_secret("the refund window is", get_bundle(CS)))

    def test_long_prose_in_a_credential_field_is_flagged_medium(self):
        """
        Consequence of the T0-9 ordering fix. Prose inside `password = "..."` is a
        passphrase, not prose - and the assignment gate means `description = "..."`
        never reaches this code at all.
        """
        self.assertEqual(
            classify_generic_secret("the refund window is thirty days", get_bundle(CS)),
            "medium",
        )

    def test_thresholds_come_from_the_bundle_not_the_module(self):
        bundle = dict(get_bundle(CS))
        bundle["secret_min_length"] = 999
        self.assertIsNone(classify_generic_secret("aB3xK9mP2qL7vN4wR8tY6uZ1", bundle))


class TestEntropyMechanics(unittest.TestCase):

    def test_charset_classified_by_class_not_observed_count(self):
        """
        The implementation trap: counting distinct observed characters gives a
        repetitive string a tiny denominator and inflates its ratio — the exact
        opposite of the intent.
        """
        self.assertEqual(detect_charset_size("a3f91b7c"), 16)
        self.assertEqual(detect_charset_size("aB3xK9mP"), 62)
        self.assertLess(normalized_entropy("a" * 32), 0.01)

    def test_separator_stripping_lifts_uuid_format_keys(self):
        """
        Hyphens impose a double penalty: they lower raw entropy AND push charset
        classification out of hex(16) into printable(95), raising the ceiling.
        Regression guard — without stripping this is a systematic false negative.
        """
        key = "6b0d549b-6f03-675a-1600-a35a099950d8"
        naive_charset = detect_charset_size(key)
        self.assertEqual(naive_charset, 95, "unstripped, this classifies as printable")
        self.assertEqual(detect_charset_size(key.replace("-", "")), 16)
        self.assertGreater(normalized_entropy(key), 0.80)

    def test_ordering_holds_random_above_prose(self):
        self.assertGreater(
            normalized_entropy("aB3xK9mP2qL7vN4wR8tY6uZ1cD5eF7gH"),
            normalized_entropy("the refund window is thirty days"),
        )

    def test_placeholder_and_dictionary_guards(self):
        self.assertTrue(looks_like_placeholder("sk-xxxxxxxxxxxxxxxx"))
        self.assertTrue(looks_like_placeholder("YOUR_API_KEY_HERE"))
        self.assertTrue(is_mostly_dictionary_words("correcthorsebatterystaple"))
        self.assertFalse(is_mostly_dictionary_words("aB3xK9mP2qL7vN4wR8tY6uZ1"))


class TestExpectedRandomNormalization(unittest.TestCase):
    """
    T0-10. The spec divided by the THEORETICAL maximum entropy, log2(k) — a score
    randomness cannot reach. Drawing 24 characters from a 16-symbol alphabet makes
    repeats unavoidable, so real random hex measured 0.868 against a ceiling of 4.000
    and fell under the 0.9 threshold: 227 of 300 sampled secrets were missed.

    Dividing by the EXPECTED entropy of a random string of the same shape
    (Miller-Madow) makes the ratio ~1.0 for random input at any length and alphabet.
    """

    def _random(self, alphabet, n, count, seed):
        rng = random.Random(seed)
        return ["".join(rng.choice(alphabet) for _ in range(n)) for _ in range(count)]

    def test_denominator_is_below_the_theoretical_maximum(self):
        """The whole fix in one assertion: grade against real randomness, not perfection."""
        self.assertLess(expected_random_entropy(16, 24), math.log2(16))
        self.assertLess(expected_random_entropy(62, 24), math.log2(62))

    def test_denominator_approaches_the_maximum_as_length_grows(self):
        """Sampling bias shrinks with more draws, so the correction should shrink too."""
        short, long = expected_random_entropy(16, 24), expected_random_entropy(16, 512)
        self.assertLess(short, long)
        self.assertAlmostEqual(long, math.log2(16), places=1)

    def test_random_secrets_are_caught_at_every_length(self):
        """Regression for the 78% miss rate at the shipped defaults."""
        bundle = get_bundle(CS)
        for n, floor in [(24, 0.85), (32, 0.90), (48, 0.95)]:
            with self.subTest(length=n):
                samples = self._random("0123456789abcdef", n, 200, seed=n)
                caught = sum(1 for s in samples if classify_generic_secret(s, bundle) == "high")
                self.assertGreaterEqual(
                    caught / 200, floor,
                    f"recall {caught}/200 at {n} hex chars is below {floor:.0%}",
                )

    def test_non_random_strings_stay_below_the_threshold(self):
        """The margin the fix must not eat: random clusters ~0.98, non-random ~0.74-0.88."""
        for s in ["supportticketreferenceid", "mycompanyname2024internal",
                  "abcdefghijabcdefghijabcd"]:
            with self.subTest(s=s):
                self.assertLess(normalized_entropy(s), 0.9)

    def test_ratio_is_clamped_to_unit_range(self):
        """Miller-Madow undershoots slightly for large alphabets, so raw ratios can exceed 1."""
        for s in self._random(string.ascii_letters + string.digits, 24, 100, seed=11):
            self.assertLessEqual(normalized_entropy(s), 1.0)
            self.assertGreaterEqual(normalized_entropy(s), 0.0)

    def test_the_reported_regression_case(self):
        """`db_password = "8db03397863983ead5ea8804"` was leaking silently."""
        r = t0('db_password = "8db03397863983ead5ea8804"')
        found = [f for f in r.findings if f.entity_type == "GENERIC_SECRET"]
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].severity, "high")


class TestBlocklist(unittest.TestCase):

    def test_blocklist_terms_come_from_the_bundle(self):
        bundle = dict(get_bundle(CS))
        bundle["blocklist_terms"] = ["projectfalcon", "acquisition"]
        r = t0("The projectfalcon timeline slipped.", bundle=bundle)
        hits = [f for f in r.findings if f.check == "blocklist"]
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].severity, "medium")

    def test_word_boundaries_respected(self):
        bundle = dict(get_bundle(CS))
        bundle["blocklist_terms"] = ["ace"]
        self.assertEqual(
            [f for f in t0("The spacecraft launched.", bundle=bundle).findings
             if f.check == "blocklist"], []
        )

    def test_empty_blocklist_is_a_no_op(self):
        self.assertEqual(get_bundle(CS)["blocklist_terms"], [])
        self.assertEqual(t0("anything at all").findings, [])


class TestCategoricalFloor(unittest.TestCase):
    """
    The replacement for Track 2's hard override. Without it a `high` T0 finding is
    diluted by the t0 weight the moment any T1 detector also runs — the P4 defect
    reappearing on Tier 0.
    """

    NOISY_T1 = dict(pii_confidence=0.1, grounding_similarity=0.95, toxicity_probability=0.05)

    def test_high_severity_blocks_despite_weight_dilution(self):
        bundle = get_bundle(CS)
        r = fuse(DetectorSignals(t0_severities=["high"], **self.NOISY_T1), bundle)
        self.assertIn("t0", r.critical_fired)
        self.assertEqual(r.action, "BLOCK")

    def test_medium_severity_stays_below_the_floor(self):
        bundle = get_bundle(CS)
        r = fuse(DetectorSignals(t0_severities=["medium"], **self.NOISY_T1), bundle)
        self.assertNotIn("t0", r.critical_fired)
        self.assertNotEqual(r.action, "BLOCK")

    def test_floor_severity_is_bundle_driven(self):
        bundle = dict(get_bundle(CS))
        bundle["t0_floor_severity"] = "medium"          # tenant tightened it
        r = fuse(DetectorSignals(t0_severities=["medium"], **self.NOISY_T1), bundle)
        self.assertIn("t0", r.critical_fired)

    def test_floor_is_categorical_not_on_the_s_scale(self):
        """T0 has no threshold, so it must never be compared against critical_S."""
        self.assertNotIn("t0", get_bundle(CS)["detector_critical_thresholds"])


class TestLedgerPrivacy(unittest.TestCase):

    def test_no_raw_value_secret_or_canary_reaches_the_ledger(self):
        scope = canary.mint(has_context=True)
        output = (f"card 4111111111111111 key AKIAIOSFODNN7EXAMPLE1 "
                  f"canary {scope.system}")
        row = json.dumps(t0(output, canaries=scope).ledger_row())

        self.assertNotIn("4111111111111111", row)
        self.assertNotIn("AKIAIOSFODNN7EXAMPLE1", row)
        self.assertNotIn(scope.system, row)
        self.assertIn("CREDIT_CARD", row)
        self.assertIn("character_span", row)

    def test_finding_type_cannot_carry_a_value(self):
        """Structural, not procedural: the field does not exist to be forgotten."""
        from data_plane.models import Finding
        self.assertNotIn("value", Finding.model_fields)


if __name__ == "__main__":
    unittest.main()
