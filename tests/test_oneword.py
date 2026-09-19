"""Tests for one-word-series.

Nothing here touches a paid API.  The Ark tests drive the adapter through a
fake opener so the request shape, the no-retry-on-submit rule and the budget
cap are all asserted without spending anything.
"""

from __future__ import annotations

import io
import subprocess
import json
import unittest
import urllib.error
from unittest import mock
from pathlib import Path
from tempfile import TemporaryDirectory

from oneword import vendors, voice
from oneword.contracts import BlockerFinding, GeneratedClip
from oneword.bible import BIBLE_VERSION, SeriesBible, build_bible, slugify, validate
from oneword.pipeline import (
    PipelineError,
    build_shots,
    compose_prompt,
    run_episode,
    validate_episode_report,
)


def make_bible(episodes: int = 2, shots: int = 4) -> SeriesBible:
    bible, _ = build_bible("rust", episodes=episodes, shots=shots, allow_model=False)
    return bible


class StubVendor:
    """Writes a tiny real file; records every prompt it was given."""

    name = "stub-vendor"
    generative = True

    def __init__(self) -> None:
        self.calls: list[tuple[str, int, str]] = []

    def generate(self, shot, prompt, attempt, target: Path) -> GeneratedClip:
        target.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            [vendors.ffmpeg_exe(), "-hide_banner", "-loglevel", "error",
             "-f", "lavfi", "-i", "color=c=black:s=320x180:d=0.3:r=12",
             "-c:v", "libx264", "-pix_fmt", "yuv420p", "-y", str(target)],
            capture_output=True, check=True,
        )
        self.calls.append((str(shot["shot_id"]), attempt, prompt))
        return GeneratedClip(
            shot_id=str(shot["shot_id"]),
            attempt=attempt,
            provider=self.name,
            path=target,
            prompt=prompt,
        )


class ScriptedAuditor:
    """Fails the named shots for the first N rounds, then passes them."""

    name = "scripted"
    last_evidence = "RULE TRIAGE ONLY"

    def __init__(self, failing: dict[str, int]) -> None:
        self.failing = dict(failing)
        self.round = 0
        self._seen: dict[str, int] = {}

    def audit(self, clip, shot, bible=None):
        shot_id = str(shot["shot_id"])
        seen = self._seen.get(shot_id, 0)
        self._seen[shot_id] = seen + 1
        if seen < self.failing.get(shot_id, 0):
            return [
                BlockerFinding(
                    rule_id="SER-01",
                    evidence="the torn cuff is missing",
                    minimal_fix="restore the torn left cuff",
                    severity="regenerate",
                )
            ]
        return []


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


class BibleTests(unittest.TestCase):
    def test_locked_block_is_byte_stable(self):
        bible = make_bible()
        first = bible.locked_block("L1", ["C1", "C2"])
        second = bible.locked_block("L1", ["C1", "C2"])
        self.assertEqual(first, second)
        self.assertIn("torn left cuff", first)

    def test_every_episode_shares_the_same_locked_strings(self):
        bible = make_bible(episodes=3, shots=4)
        blocks = {
            number: bible.locked_block("L1", ["C1"]) for number in bible.episode_numbers
        }
        self.assertEqual(len(set(blocks.values())), 1)

    def test_unknown_ids_are_repaired_against_the_bible(self):
        data = make_bible().data
        data["episodes"][0]["beats"][0]["location_id"] = "L99"
        data["episodes"][0]["beats"][0]["character_ids"] = ["C42"]
        fixed = validate(data, episodes=2, shots=4)
        beat = fixed["episodes"][0]["beats"][0]
        self.assertIn(beat["location_id"], fixed["locations"])
        self.assertTrue(set(beat["character_ids"]) <= set(fixed["characters"]))

    def test_cjk_seed_word_gets_a_filesystem_safe_slug(self):
        self.assertTrue(slugify("锈").startswith("seed-"))
        self.assertEqual(slugify("Rust Belt"), "rust-belt")

    def test_bible_round_trips_through_disk(self):
        bible = make_bible()
        with TemporaryDirectory() as tmp:
            path = bible.save(Path(tmp) / "bible.json")
            again = SeriesBible.load(path)
        self.assertEqual(again.data["bible_version"], BIBLE_VERSION)
        self.assertEqual(again.locked_block("L1", ["C1"]), bible.locked_block("L1", ["C1"]))


class PromptTests(unittest.TestCase):
    def test_prompt_carries_locked_facts_and_rules(self):
        bible = make_bible()
        shot = build_shots(bible, 1)[0]
        prompt = compose_prompt(bible, shot)
        self.assertIn("[CHARACTER", prompt)
        self.assertIn("[LOCATION", prompt)
        self.assertIn("MUST: SER-01", prompt)
        self.assertIn("[NEGATIVE]", prompt)

    def test_identity_critical_beats_route_to_the_primary_tier(self):
        bible = make_bible(shots=5)
        tiers = {shot["beat"]: shot["model_tier"] for shot in build_shots(bible, 1)}
        self.assertEqual(tiers["turn"], "primary")
        self.assertEqual(tiers["establish"], "economy")


class LoopTests(unittest.TestCase):
    def run_once(self, failing, tmp, **kwargs):
        bible = make_bible(episodes=1, shots=4)
        vendor = StubVendor()
        auditor = ScriptedAuditor(failing)
        report = run_episode(
            bible, 1, Path(tmp),
            vendor=vendor, auditor=auditor,
            voice_engine=voice.SilentVoice(), **kwargs,
        )
        return report, vendor

    def test_only_blockers_are_regenerated(self):
        with TemporaryDirectory() as tmp:
            report, vendor = self.run_once({"2": 1}, tmp)
        regenerated = [call for call in vendor.calls if call[1] > 1]
        self.assertEqual([call[0] for call in regenerated], ["2"])
        self.assertEqual(report["summary"]["repaired_shot_ids"], ["2"])
        self.assertEqual(report["summary"]["total_generation_count"], 5)

    def test_repair_prompt_appends_the_minimal_fix_only(self):
        with TemporaryDirectory() as tmp:
            _, vendor = self.run_once({"2": 1}, tmp)
        first = next(call[2] for call in vendor.calls if call[0] == "2" and call[1] == 1)
        second = next(call[2] for call in vendor.calls if call[0] == "2" and call[1] == 2)
        self.assertTrue(second.startswith(first))
        self.assertIn("restore the torn left cuff", second)

    def test_repair_stops_at_two_rounds(self):
        with TemporaryDirectory() as tmp:
            report, vendor = self.run_once({"3": 99}, tmp)
        attempts = [call[1] for call in vendor.calls if call[0] == "3"]
        self.assertEqual(max(attempts), 3)  # initial + two repairs
        # It is still broken, and the report must not pretend otherwise.
        self.assertEqual(report["summary"]["status"], "BLOCKERS REMAIN")
        self.assertEqual(report["summary"]["unresolved_shot_ids"], ["3"])

    def test_a_clean_run_never_regenerates(self):
        with TemporaryDirectory() as tmp:
            report, vendor = self.run_once({}, tmp)
        self.assertEqual(report["summary"]["total_generation_count"], 4)
        self.assertEqual(report["summary"]["repaired_shot_ids"], [])
        self.assertFalse(report["summary"]["whole_film_rerun"])

    def test_report_rejects_a_regeneration_that_was_not_a_blocker(self):
        with TemporaryDirectory() as tmp:
            report, _ = self.run_once({}, tmp)
        report["generation_events"].append(
            {"round": 1, "shot_id": "1", "attempt": 2, "provider": "stub-vendor",
             "file": "x.mp4", "model_tier": "economy"}
        )
        report["summary"]["total_generation_count"] += 1
        with self.assertRaises(PipelineError):
            validate_episode_report(report)

    def test_report_rejects_mixed_vendors(self):
        with TemporaryDirectory() as tmp:
            report, _ = self.run_once({}, tmp)
        report["generation_events"][0]["provider"] = "someone-else"
        with self.assertRaises(PipelineError):
            validate_episode_report(report)


class AssemblyTests(unittest.TestCase):
    def test_offline_run_produces_a_playable_file_with_audio(self):
        bible = make_bible(episodes=1, shots=3)
        with TemporaryDirectory() as tmp:
            report = run_episode(
                bible, 1, Path(tmp),
                vendor=vendors.AnimaticVendor(),
                voice_engine=voice.build_voice("silent"),
                clip_seconds=2,
            )
            video = Path(tmp) / report["outputs"]["video"]
            self.assertTrue(video.is_file() and video.stat().st_size > 1000)
            self.assertGreater(report["summary"]["runtime_sec"], 5)

    def test_srt_timestamps_are_ordered_and_formatted(self):
        with TemporaryDirectory() as tmp:
            target = voice.write_srt(
                [{"start": 0.35, "end": 4.85, "text": "one"},
                 {"start": 5.35, "end": 9.85, "text": "two"}],
                Path(tmp) / "x.srt",
            )
            body = target.read_text(encoding="utf-8")
        self.assertIn("00:00:00,350 --> 00:00:04,850", body)
        self.assertIn("00:00:05,350 --> 00:00:09,850", body)

    def test_blank_lines_produce_no_subtitle_entry(self):
        with TemporaryDirectory() as tmp:
            target = voice.write_srt([{"start": 0, "end": 1, "text": "   "}], Path(tmp) / "x.srt")
            self.assertEqual(target.read_text(encoding="utf-8").strip(), "")


class SeedanceTests(unittest.TestCase):
    def config(self, **kwargs):
        base = dict(api_key="test-key", model="doubao-seedance-1-0-lite-t2v-250428",
                    resolution="720p", duration=5, budget_cny=1.0)
        base.update(kwargs)
        return vendors.ArkConfig(**base)

    def test_submit_sends_the_ark_native_shape(self):
        seen = {}

        def opener(request, timeout=None):
            seen["url"] = request.full_url
            seen["auth"] = request.headers.get("Authorization")
            seen["body"] = json.loads(request.data.decode("utf-8"))
            return FakeResponse(json.dumps({"id": "task-1"}).encode("utf-8"))

        vendor = vendors.SeedanceVendor(self.config(), opener=opener)
        task_id = vendor.submit("a stairwell")
        self.assertEqual(task_id, "task-1")
        self.assertTrue(seen["url"].endswith("/contents/generations/tasks"))
        self.assertEqual(seen["auth"], "Bearer test-key")
        self.assertEqual(seen["body"]["model"], "doubao-seedance-1-0-lite-t2v-250428")
        text = seen["body"]["content"][0]["text"]
        self.assertIn("--resolution 720p", text)
        self.assertIn("--duration 5", text)

    def test_submit_never_retries(self):
        calls = {"n": 0}

        def opener(request, timeout=None):
            calls["n"] += 1
            raise urllib.error.HTTPError(request.full_url, 429, "busy", {}, None)

        vendor = vendors.SeedanceVendor(self.config(), opener=opener)
        with self.assertRaises(urllib.error.HTTPError):
            vendor.submit("x")
        self.assertEqual(calls["n"], 1)

    def test_budget_cap_refuses_the_next_clip(self):
        def opener(request, timeout=None):
            return FakeResponse(json.dumps({"id": "t"}).encode("utf-8"))

        vendor = vendors.SeedanceVendor(self.config(budget_cny=1.0), opener=opener)
        vendor.submit("first")  # 0.75
        with self.assertRaises(vendors.BudgetExceeded):
            vendor.submit("second")

    def test_unpriced_combination_is_refused_not_guessed(self):
        with self.assertRaises(vendors.VendorError):
            vendors.estimated_cost_cny(self.config(resolution="4k"))

    def test_failed_task_raises_with_the_platform_reason(self):
        def opener(request, timeout=None):
            return FakeResponse(
                json.dumps(
                    {"status": "failed", "error": {"code": "QuotaExceeded", "message": "no quota"}}
                ).encode("utf-8")
            )

        vendor = vendors.SeedanceVendor(self.config(), opener=opener)
        with self.assertRaisesRegex(vendors.VendorError, "QuotaExceeded"):
            vendor.poll("task-1")

    def test_api_key_never_appears_in_an_error_message(self):
        def opener(request, timeout=None):
            return FakeResponse(json.dumps({"status": "failed", "error": {}}).encode("utf-8"))

        vendor = vendors.SeedanceVendor(self.config(api_key="super-secret"), opener=opener)
        try:
            vendor.poll("task-1")
        except vendors.VendorError as exc:
            self.assertNotIn("super-secret", str(exc))


class VoiceTests(unittest.TestCase):
    def test_a_character_keeps_one_voice_across_episodes(self):
        engine = voice.VolcTTSVoice(appid="a", token="b", opener=lambda *a, **k: None)
        first = engine.voice_type({"gender": "female"}, "C2")
        second = engine.voice_type({"gender": "female"}, "C2")
        self.assertEqual(first, second)

    def test_auto_falls_back_to_silence_when_no_engine_exists(self):
        # A machine with no TTS binary must still get its film.
        with mock.patch.object(voice, "EspeakVoice", side_effect=voice.VoiceError("no espeak")), \
             mock.patch.object(voice, "VolcTTSVoice", side_effect=voice.VoiceError("no creds")):
            engine = voice.build_voice("auto")
        self.assertEqual(engine.name, "silent")

    def test_an_explicitly_named_engine_still_fails_loudly(self):
        with mock.patch.object(voice, "EspeakVoice", side_effect=voice.VoiceError("no espeak")):
            with self.assertRaises(voice.VoiceError):
                voice.build_voice("espeak")

    def test_silent_engine_returns_nothing(self):
        with TemporaryDirectory() as tmp:
            self.assertIsNone(
                voice.SilentVoice().synthesize("hello", {}, "C1", Path(tmp) / "a.wav")
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)


class ContractTests(unittest.TestCase):
    def test_an_unknown_severity_is_rejected_at_construction(self):
        with self.assertRaises(ValueError):
            BlockerFinding(rule_id="X", evidence="e", minimal_fix="f", severity="minor")

    def test_both_vendors_satisfy_the_protocol(self):
        from oneword.contracts import FilmVendor

        self.assertIsInstance(vendors.AnimaticVendor(), FilmVendor)
        self.assertTrue(vendors.AnimaticVendor().generative is False)

    def test_the_package_exports_a_usable_surface(self):
        import oneword

        for name in ("build_bible", "run_episode", "build_vendor", "build_voice"):
            self.assertTrue(callable(getattr(oneword, name)), name)

    def test_the_verifier_is_optional_not_required(self):
        from oneword.audit import continuity_agent_available

        self.assertIsInstance(continuity_agent_available(), bool)
