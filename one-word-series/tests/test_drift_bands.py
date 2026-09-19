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
import unittest.mock
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

    def test_a_chained_shot_records_what_it_continued(self):
        """So it can be measured against that shot, not only the reference."""

        report = episode_report([event(2, continues="1")], {"2": "1"})
        self.assertEqual(drift.chain_state_for(report, "2")["continues"], "1")

    def test_an_unchained_shot_has_nothing_to_continue(self):
        report = episode_report([event(2)], {})
        self.assertEqual(drift.chain_state_for(report, "2").get("continues", ""), "")

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
    def test_there_is_one_band_and_it_says_what_measured_it(self):
        """The chain-state split was a hypothesis the real footage refuted.

        A chained shot resembles the shot it continued, not the reference, and
        inherits that shot's offset — so chain state does not describe the
        comparison the band is applied to. See docs/drift-calibration.md.
        """

        self.assertEqual(set(drift.BANDS), {"reference"})
        self.assertTrue(drift.BANDS["reference"]["measured_on"])

    def test_the_band_decides_the_verdict(self):
        self.assertEqual(drift.verdict_for(0.01), drift.CONSISTENT)
        self.assertEqual(drift.verdict_for(0.05), drift.REVIEW)
        self.assertEqual(drift.verdict_for(0.12), drift.DRIFTED)

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


class SilentModelFailureTests(unittest.TestCase):
    """A configured model that never answered must not look like no model.

    This is the same class of bug as the swallowed HTTP 404: the run degrades
    correctly and tells you nothing, so you set a text-only model id, wait, and
    get a report identical to the one you would get with no key at all.
    """

    def test_a_failed_call_records_why(self):
        errors: list[str] = []
        with unittest.mock.patch.object(drift.llm, "configured", return_value=True), \
             unittest.mock.patch.object(
                 drift, "_chat_vision", side_effect=RuntimeError("model is not multimodal")), \
             unittest.mock.patch.object(drift, "_data_url", return_value="data:,"):
            result = drift.visual_compare(Path("a.jpg"), Path("b.jpg"), "facts", errors)

        self.assertIsNone(result)
        self.assertEqual(len(errors), 1)
        self.assertIn("not multimodal", errors[0])

    def test_the_same_failure_is_not_recorded_twice(self):
        errors: list[str] = []
        with unittest.mock.patch.object(drift.llm, "configured", return_value=True), \
             unittest.mock.patch.object(
                 drift, "_chat_vision", side_effect=RuntimeError("same reason")), \
             unittest.mock.patch.object(drift, "_data_url", return_value="data:,"):
            for _ in range(5):
                drift.visual_compare(Path("a.jpg"), Path("b.jpg"), "facts", errors)
        self.assertEqual(len(errors), 1)

    def test_no_model_configured_records_nothing(self):
        """Not an error — just nobody looked."""

        errors: list[str] = []
        with unittest.mock.patch.object(drift.llm, "configured", return_value=False):
            self.assertIsNone(drift.visual_compare(Path("a"), Path("b"), "f", errors))
        self.assertEqual(errors, [])


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
