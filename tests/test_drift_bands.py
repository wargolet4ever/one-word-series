"""Which threshold applied, which channel decided, and how the shot was made.

The first real footage produced a report whose every verdict came from colour
while structure never moved, and whose worst "same room" readings were further
apart than the synthetic harness's *different* rooms. Both facts were invisible
in the report itself. These tests are about making them impossible to miss
again — and about not letting a threshold measured on one population be spent
silently on another.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from oneword import drift, metrics  # noqa: E402


def episode_report(events, chain_links=None):
    return {
        "episode": 1,
        "chain_links": chain_links if chain_links is not None else {},
        "generation_events": events,
    }


def event(shot_id, *, attempt=1, continues=None, chain_dropped=None, refs_dropped=None):
    return {
        "shot_id": str(shot_id),
        "attempt": attempt,
        "continues_shot": continues,
        "chain_dropped": chain_dropped,
        "references_dropped": refs_dropped,
    }


class ChainStateTests(unittest.TestCase):
    def test_a_shot_that_continued_another_says_which(self):
        report = episode_report([event(2, continues="1")], {"2": "1"})
        state = drift.chain_state_for(report, "2")
        self.assertEqual(state["state"], drift.CHAINED)
        self.assertIn("shot 1", state["detail"])

    def test_a_refused_first_frame_is_not_reported_as_chained(self):
        """This is the whole point: the cut may jump and the report must say so."""

        report = episode_report(
            [event(2, chain_dropped="InputImageSensitiveContentDetected")], {"2": "1"}
        )
        state = drift.chain_state_for(report, "2")
        self.assertEqual(state["state"], drift.CHAIN_REFUSED)
        self.assertIn("Sensitive", state["detail"])

    def test_a_refused_chain_is_judged_as_an_unchained_shot(self):
        """Because that is what it is — a text-to-video shot."""

        self.assertEqual(drift.population_for(drift.CHAIN_REFUSED), "unchained")
        self.assertEqual(drift.population_for(drift.CHAINED), "chained")

    def test_the_last_take_is_what_describes_the_shot(self):
        """A repaired shot is the one in the film; take 1 is not."""

        report = episode_report(
            [event(2, attempt=1, continues="1"), event(2, attempt=2, chain_dropped="refused")],
            {"2": "1"},
        )
        self.assertEqual(drift.chain_state_for(report, "2")["state"], drift.CHAIN_REFUSED)

    def test_an_episode_that_planned_no_chains_says_so(self):
        report = episode_report([event(2)], {})
        state = drift.chain_state_for(report, "2")
        self.assertEqual(state["state"], drift.UNCHAINED)
        self.assertIn("chaining off", state["detail"])

    def test_a_cut_to_another_room_is_distinguished_from_a_failure(self):
        report = episode_report([event(3)], {"2": "1"})
        self.assertIn("another location", drift.chain_state_for(report, "3")["detail"])

    def test_a_shot_with_no_events_is_unknown_not_guessed(self):
        self.assertEqual(
            drift.chain_state_for(episode_report([]), "2")["state"], drift.CHAIN_UNKNOWN
        )


class PortraitStateTests(unittest.TestCase):
    def test_a_refused_portrait_is_recoverable_from_the_report(self):
        report = episode_report([event(2, refs_dropped="may contain a real person")])
        self.assertIn("real person", drift.portraits_dropped_for(report, "2"))

    def test_a_shot_that_kept_its_portraits_reports_nothing(self):
        self.assertEqual(drift.portraits_dropped_for(episode_report([event(2)]), "2"), "")


class BandTests(unittest.TestCase):
    def test_the_chained_band_is_marked_unmeasured(self):
        """Borrowing a number is allowed; pretending it was measured is not."""

        self.assertIsNone(drift.BANDS["chained"]["measured_on"])
        self.assertTrue(drift.BANDS["unchained"]["measured_on"])

    def test_a_verdict_uses_the_band_it_was_given(self):
        try:
            drift.BANDS["chained"] = {
                "consistent_max": 0.02, "drifted_min": 0.04, "measured_on": "a test",
            }
            self.assertEqual(drift.verdict_for(0.03, "chained"), drift.REVIEW)
            self.assertEqual(drift.verdict_for(0.03, "unchained"), drift.CONSISTENT)
            self.assertEqual(drift.verdict_for(0.05, "chained"), drift.DRIFTED)
        finally:
            drift.BANDS["chained"] = {
                "consistent_max": drift.SAME_PLACE_MAX,
                "drifted_min": drift.DIFFERENT_PLACE_MIN,
                "measured_on": None,
            }

    def test_an_unknown_population_falls_back_rather_than_crashing(self):
        self.assertEqual(drift.verdict_for(0.01, "nonsense"), drift.CONSISTENT)


class ChannelTests(unittest.TestCase):
    """On real footage structure barely moves, so the blend has one live channel."""

    def test_a_colour_only_difference_is_named_as_such(self):
        # The shape of every DRIFTED row in the first real report.
        self.assertEqual(
            metrics.tripped_by({"colour": 0.168, "structure": 0.007, "composite": 0.112}),
            "colour",
        )

    def test_a_structural_difference_is_named_too(self):
        self.assertEqual(
            metrics.tripped_by({"colour": 0.002, "structure": 0.20, "composite": 0.071}),
            "structure",
        )

    def test_an_even_split_is_reported_as_both(self):
        self.assertEqual(
            metrics.tripped_by({"colour": 0.05, "structure": 0.05, "composite": 0.05}), "both"
        )

    def test_two_identical_frames_trip_nothing(self):
        self.assertEqual(
            metrics.tripped_by({"colour": 0.0, "structure": 0.0, "composite": 0.0}), "neither"
        )

    def test_the_share_is_computed_after_weighting(self):
        """0.65/0.35 means equal raw numbers are not an equal contribution."""

        weighted = metrics.tripped_by({"colour": 0.10, "structure": 0.10, "composite": 0.10})
        self.assertEqual(weighted, "both")


class ValidatorTests(unittest.TestCase):
    def base(self, **overrides):
        finding = {
            "episode": 1, "shot_id": "2", "kind": "location", "id": "L1", "name": "Stairwell",
            "verdict": drift.CONSISTENT, "evidence_source": drift.PIXEL_SCREEN,
            "distance": {"colour": 0.01, "structure": 0.01, "composite": 0.01},
            "differences": [], "tripped_by": "both",
        }
        finding.update(overrides)
        return {
            "findings": [finding],
            "summary": {"status": "CONSISTENT", "not_checked": 0},
        }

    def test_a_number_without_an_account_of_it_is_refused(self):
        with self.assertRaises(drift.DriftError):
            drift.validate_drift_report(self.base(tripped_by=None))

    def test_a_properly_attributed_finding_passes(self):
        drift.validate_drift_report(self.base())


if __name__ == "__main__":
    unittest.main()
