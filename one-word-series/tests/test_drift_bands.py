"""Which threshold applied, which channel decided, and how the shot was made.

The first real footage produced a report whose every verdict came from colour
while structure never moved, and whose worst "same room" readings were further
apart than the synthetic harness's *different* rooms. Both facts were invisible
in the report itself. These tests are about making them impossible to miss
again — and about not letting a threshold measured on one population be spent
silently on another.
"""

from __future__ import annotations

import json
import subprocess
import sys
import unittest.mock
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import os  # noqa: E402

from oneword import cli, drift, metrics, vendors  # noqa: E402
from oneword.bible import build_bible  # noqa: E402


def make_clip(target: Path) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [vendors.ffmpeg_exe(), "-hide_banner", "-loglevel", "error",
         "-f", "lavfi", "-i", "color=c=navy:s=320x180:d=1:r=12",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-y", str(target)],
        capture_output=True, check=True,
    )
    return target


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


class MissingFootageTests(unittest.TestCase):
    """A series whose clips are gone must not read as a series that is fine.

    `.mp4` is in .gitignore, so a cloned or copied series directory keeps every
    report and none of the footage. Skipping those shots quietly produced a
    confident report over zero appearances.
    """

    def series(self, tmp, with_clips: bool):
        root = Path(tmp) / "rust"
        (root / "episode-01" / "clips").mkdir(parents=True)
        bible, _ = build_bible("rust", episodes=1, shots=3, allow_model=False, style="noir")
        bible.save(root / "bible.json")
        shots = [
            {"shot_id": str(n), "file": f"shot-0{n}-take-01.mp4",
             "location": bible.data["locations"]["L1"]["name"], "characters": []}
            for n in (1, 2, 3)
        ]
        (root / "episode-01" / "episode-report.json").write_text(
            json.dumps({
                "episode": 1, "vendor_is_generative": True, "shots": shots,
                "chain_links": {}, "generation_events": [],
            }),
            encoding="utf-8",
        )
        if with_clips:
            for shot in shots:
                make_clip(root / "episode-01" / "clips" / shot["file"])
        return root

    def test_a_series_with_no_footage_left_is_an_error_not_a_clean_report(self):
        with TemporaryDirectory() as tmp:
            root = self.series(tmp, with_clips=False)
            with self.assertRaises(drift.DriftError) as caught:
                drift.audit_series(root, use_model=False)
        message = str(caught.exception)
        self.assertIn("3 clips", message)
        self.assertIn(".gitignore", message)

    def test_footage_that_is_present_audits_normally(self):
        with TemporaryDirectory() as tmp:
            root = self.series(tmp, with_clips=True)
            report = drift.audit_series(root, use_model=False)
        self.assertEqual(report["missing_clips"], [])
        self.assertTrue(report["findings"])

    def test_a_wrong_directory_names_the_ones_that_would_work(self):
        """The error that sent someone to out/ when the run wrote out-real/."""

        with TemporaryDirectory() as tmp:
            self.series(Path(tmp) / "out-real", with_clips=True)
            empty = Path(tmp) / "out" / "rust"
            empty.mkdir(parents=True)
            build_bible("rust", episodes=1, shots=3, allow_model=False)[0].save(
                empty / "bible.json"
            )
            with self.assertRaises(drift.DriftError) as caught:
                drift.audit_series(empty, use_model=False)
        self.assertIn("out-real", str(caught.exception))


class CliSaysWhyNobodyLookedTests(unittest.TestCase):
    """The CLI is what people read; the HTML is what they open afterwards.

    The silent-failure fix went into the HTML report only, so the terminal kept
    printing "no multimodal model looked" whether a key was missing or a
    configured model had failed every call. Someone set a key, saw the same
    output, and reasonably concluded the key was fine.
    """

    def drift_report(self, **overrides):
        report = {
            "model_looked": False,
            "model_errors": [],
            "missing_clips": [],
            "findings": [],
            "summary": {
                "status": "PARTIAL", "checked": 18, "drifted": 0, "review": 0,
                "not_checked": 10, "storyboard_episodes": [],
            },
        }
        report.update(overrides)
        return report

    def printed(self, report, environment):
        import contextlib
        import io

        out = io.StringIO()
        with unittest.mock.patch.dict(os.environ, environment, clear=False):
            for name in ("LLM_API_KEY", "LLM_MODEL"):
                if name not in environment:
                    os.environ.pop(name, None)
            with contextlib.redirect_stdout(out):
                cli._print_drift(report)
        return out.getvalue()

    def test_a_failing_model_is_not_reported_as_no_model(self):
        text = self.printed(
            self.drift_report(model_errors=["HTTPError: 400 model is not multimodal"]),
            {"LLM_API_KEY": "k", "LLM_MODEL": "text-only"},
        )
        self.assertIn("WAS configured", text)
        self.assertIn("not multimodal", text)
        self.assertNotIn("no multimodal model looked", text)

    def test_some_calls_failing_is_not_reported_as_all_of_them(self):
        """The contradiction that shipped: "nothing was examined" printed
        directly above verdicts a model had reached.

        Branching on "were there errors" instead of "did anything succeed"
        turns a partial failure into a false claim — and a false claim about
        what was examined is the one thing this tool may never make.
        """

        text = self.printed(
            self.drift_report(
                model_looked=True,
                model_errors=["ModelUnavailable: TimeoutError: read operation timed out"],
                summary=dict(self.drift_report()["summary"], not_checked=1),
            ),
            {"LLM_API_KEY": "k", "LLM_MODEL": "vision"},
        )
        self.assertIn("1 call(s) to the model failed", text)
        self.assertIn("the rest answered", text)
        self.assertNotIn("every call", text)
        self.assertNotIn("Nothing below was examined", text)

    def test_a_missing_key_names_which_half_is_missing(self):
        text = self.printed(self.drift_report(), {"LLM_API_KEY": "k"})
        self.assertIn("LLM_MODEL not set", text)
        self.assertNotIn("LLM_API_KEY and LLM_MODEL not set", text)

    def test_both_missing_says_so(self):
        text = self.printed(self.drift_report(), {})
        self.assertIn("LLM_API_KEY and LLM_MODEL not set", text)

    def test_storyboard_footage_is_not_a_misconfiguration(self):
        """Grading stand-in pixels with a vision model would buy a false verdict."""

        text = self.printed(
            self.drift_report(
                summary=dict(self.drift_report()["summary"], storyboard_episodes=[1, 2])
            ),
            {"LLM_API_KEY": "k", "LLM_MODEL": "m"},
        )
        self.assertIn("on purpose", text)
        self.assertNotIn("this is a bug", text)

    def test_skipped_clips_are_named_in_the_terminal_too(self):
        text = self.printed(
            self.drift_report(missing_clips=["ep1 shot 2 (shot-02-take-01.mp4)"]), {}
        )
        self.assertIn("not on disk", text)
        self.assertIn("shot-02-take-01.mp4", text)


class ErrorBodyTests(unittest.TestCase):
    """`HTTP Error 404: Not Found` is true and useless.

    The platform puts the reason in the body and urllib hands it over exactly
    once, on the exception. The video adapter learned this the expensive way
    and grew ArkHTTPError; llm.py and audit.py kept throwing the body away, so
    a wrong or unactivated vision model id reported a bare number and sent
    someone to check their key, their network and their spelling in that order.
    """

    def http_error(self, code=404, body=None, raw=None):
        import io
        import urllib.error

        payload = raw if raw is not None else json.dumps(body).encode("utf-8")
        return urllib.error.HTTPError("https://x/v1", code, "Not Found", {}, io.BytesIO(payload))

    def test_the_platforms_reason_survives(self):
        detail = drift.llm.error_detail(self.http_error(body={
            "error": {"code": "ModelNotFound", "message": "does not exist or you have no access"}
        }))
        self.assertIn("404", detail)
        self.assertIn("ModelNotFound", detail)
        self.assertIn("no access", detail)

    def test_a_non_json_body_is_still_shown(self):
        detail = drift.llm.error_detail(self.http_error(code=502, raw=b"<html>nginx</html>"))
        self.assertIn("502", detail)
        self.assertIn("nginx", detail)

    def test_an_exception_with_no_body_degrades_to_its_type(self):
        self.assertIn("TimeoutError", drift.llm.error_detail(TimeoutError("timed out")))

    def test_an_unreadable_body_does_not_become_a_second_failure(self):
        class Hostile(Exception):
            code = 500

            def read(self):
                raise OSError("stream already consumed")

        self.assertEqual(drift.llm.error_detail(Hostile()), "HTTP 500")

    def test_a_failing_vision_call_names_the_model_and_the_reason(self):
        from oneword import audit

        with unittest.mock.patch.dict(
            os.environ, {"LLM_MODEL": "text-only-model", "LLM_API_KEY": "k"}
        ), unittest.mock.patch.object(
            audit.llm, "post_chat",
            side_effect=self.http_error(body={"error": {"message": "no vision capability"}}),
        ):
            with self.assertRaises(drift.llm.ModelUnavailable) as caught:
                audit._chat_vision("system", [])
        message = str(caught.exception)
        self.assertIn("text-only-model", message)
        self.assertIn("no vision capability", message)


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
