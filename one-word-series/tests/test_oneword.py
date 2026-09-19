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

from oneword import cli, vendors, voice
from tests.pricing import TEST_MODEL, use_test_price
from oneword.contracts import BlockerFinding, GeneratedClip
from oneword.bible import BIBLE_VERSION, SeriesBible, build_bible, slugify, validate
from oneword.pipeline import (
    PipelineError,
    build_shots,
    compose_prompt,
    dialogue_block,
    run_episode,
    validate_episode_report,
)


def make_bible(episodes: int = 2, shots: int = 4) -> SeriesBible:
    bible, _ = build_bible("rust", episodes=episodes, shots=shots, allow_model=False)
    return bible


def with_test_price(case, model=TEST_MODEL, resolution="720p", duration=5, price=0.75):
    """Prices live outside the code now, so a test that spends must supply one.

    Delegates to `tests/pricing.py`, which also clears ONEWORD_PRICE — that env
    var outranks the table, so a developer who exported one for a real run used
    to get failures from their shell rather than from their code.
    """

    use_test_price(case, model, resolution, duration, price)


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
    def setUp(self):
        with_test_price(self)

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
        # Surfaced as ArkHTTPError now, still carrying the status code.
        with self.assertRaises(vendors.ArkHTTPError) as caught:
            vendor.submit("x")
        self.assertEqual(caught.exception.code, 429)
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


class ArkErrorMessageTests(unittest.TestCase):
    def setUp(self):
        with_test_price(self)

    """A 404 must arrive carrying the platform's own explanation."""

    def config(self, **kwargs):
        base = dict(api_key="test-key", model="doubao-seedance-1-0-lite-t2v-250428",
                    resolution="720p", duration=5, budget_cny=10.0)
        base.update(kwargs)
        return vendors.ArkConfig(**base)

    def opener_raising(self, code, body):
        def opener(request, timeout=None):
            raise urllib.error.HTTPError(
                request.full_url, code, "error", {},
                io.BytesIO(body.encode("utf-8")),
            )
        return opener

    def test_the_platform_message_survives_into_the_error(self):
        body = '{"error":{"code":"ModelNotFound","message":"model not opened"}}'
        vendor = vendors.SeedanceVendor(self.config(), opener=self.opener_raising(404, body))
        with self.assertRaises(vendors.ArkHTTPError) as caught:
            vendor.submit("x")
        text = str(caught.exception)
        self.assertIn("ModelNotFound", text)
        self.assertIn("model not opened", text)

    def test_a_404_explains_the_likely_cause(self):
        vendor = vendors.SeedanceVendor(self.config(), opener=self.opener_raising(404, "{}"))
        with self.assertRaises(vendors.ArkHTTPError) as caught:
            vendor.submit("x")
        self.assertIn("not activated", str(caught.exception))

    def test_the_status_code_survives_for_the_retry_classifier(self):
        vendor = vendors.SeedanceVendor(self.config(), opener=self.opener_raising(429, "{}"))
        try:
            vendor.submit("x")
        except vendors.ArkHTTPError as exc:
            self.assertEqual(exc.code, 429)
            self.assertTrue(vendors.SeedanceVendor._retryable(exc))

    def test_the_api_key_never_appears_in_the_error(self):
        vendor = vendors.SeedanceVendor(
            self.config(api_key="super-secret"), opener=self.opener_raising(404, "{}")
        )
        with self.assertRaises(vendors.ArkHTTPError) as caught:
            vendor.submit("x")
        self.assertNotIn("super-secret", str(caught.exception))


class ProgressTests(unittest.TestCase):
    def setUp(self):
        with_test_price(self)

    """A paid run must show it is alive while a clip generates."""

    def vendor(self, statuses, events):
        config = vendors.ArkConfig(
            api_key="k", model="doubao-seedance-1-0-lite-t2v-250428",
            resolution="720p", duration=5, budget_cny=10.0,
        )
        replies = iter(
            [json.dumps({"id": "task-1"}).encode("utf-8")]
            + [json.dumps(s).encode("utf-8") for s in statuses]
        )

        def opener(request, timeout=None):
            return FakeResponse(next(replies))

        return vendors.SeedanceVendor(
            config, opener=opener, on_progress=events.append
        )

    def test_each_poll_reports_status_and_elapsed(self):
        events = []
        vendor = self.vendor(
            [{"status": "running"}, {"status": "running"}, {"status": "succeeded"}], events
        )
        with mock.patch.object(vendors.time, "sleep"):
            vendor.poll("task-1", shot_id="2")
        self.assertTrue(events)
        self.assertEqual([e["status"] for e in events][-1], "succeeded")
        self.assertTrue(all(e["shot_id"] == "2" for e in events))
        self.assertTrue(all("elapsed" in e for e in events))

    def test_a_vendor_without_a_callback_still_works(self):
        config = vendors.ArkConfig(
            api_key="k", model="doubao-seedance-1-0-lite-t2v-250428",
            resolution="720p", duration=5, budget_cny=10.0,
        )
        replies = iter([json.dumps({"status": "succeeded"}).encode("utf-8")])
        vendor = vendors.SeedanceVendor(
            config, opener=lambda request, timeout=None: FakeResponse(next(replies))
        )
        with mock.patch.object(vendors.time, "sleep"):
            self.assertEqual(vendor.poll("task-1")["status"], "succeeded")

    def test_the_printer_rewrites_one_line_and_keeps_the_finished_one(self):
        import io as _io

        class Tty(_io.StringIO):
            def isatty(self):
                return True

        stream = Tty()
        report = cli._progress_printer(stream)
        report({"shot_id": "1", "status": "running", "elapsed": 10.0})
        report({"shot_id": "1", "status": "running", "elapsed": 20.0})
        report({"shot_id": "1", "status": "saved", "elapsed": 30.0, "done": True,
                "spent_cny": 1.86})
        text = stream.getvalue()
        self.assertEqual(text.count("\n"), 1)          # only the finished line stays
        self.assertEqual(text.count("\r"), 3)          # the rest rewrote in place
        self.assertIn("¥1.86", text)


class DialogueInPromptTests(unittest.TestCase):
    """Letting the video model perform the line instead of dubbing over it."""

    def setUp(self):
        self.bible = make_bible(episodes=1, shots=4)
        self.shot = build_shots(self.bible, 1)[0]

    def test_a_silent_vendor_gets_no_dialogue(self):
        prompt = compose_prompt(self.bible, self.shot, spoken=False)
        self.assertNotIn("[DIALOGUE]", prompt)

    def test_a_speaking_vendor_gets_the_line_and_the_speaker(self):
        prompt = compose_prompt(self.bible, self.shot, spoken=True)
        self.assertIn("[DIALOGUE]", prompt)
        self.assertIn(self.shot["line"], prompt)
        self.assertIn("Wen", prompt)
        self.assertIn("lip-synced", prompt)

    def test_dialogue_also_forbids_the_words_being_drawn(self):
        """The failure mode of asking for dialogue is text across the frame."""

        with_line = compose_prompt(self.bible, self.shot, spoken=True)
        without = compose_prompt(self.bible, self.shot, spoken=False)
        self.assertIn("burned-in text", with_line)
        self.assertNotIn("burned-in text", without)

    def test_a_shot_with_no_line_adds_nothing(self):
        silent = dict(self.shot, line="   ")
        self.assertNotIn("[DIALOGUE]", compose_prompt(self.bible, silent, spoken=True))

    def test_the_language_follows_the_line(self):
        chinese = dict(self.shot, line="你说这个房间是封死的。")
        self.assertIn("Mandarin Chinese", compose_prompt(self.bible, chinese, spoken=True))
        self.assertIn("English", compose_prompt(self.bible, self.shot, spoken=True))

    def test_the_prompt_is_still_byte_stable(self):
        first = compose_prompt(self.bible, self.shot, spoken=True)
        second = compose_prompt(self.bible, self.shot, spoken=True)
        self.assertEqual(first, second)

    def test_the_locked_block_is_unchanged_by_dialogue(self):
        """Adding dialogue must not disturb the continuity guarantee."""

        head = lambda prompt: prompt.split("[ACTION]")[0]
        self.assertEqual(
            head(compose_prompt(self.bible, self.shot, spoken=True)),
            head(compose_prompt(self.bible, self.shot, spoken=False)),
        )

    def test_the_offline_vendor_declares_it_cannot_speak(self):
        self.assertFalse(vendors.AnimaticVendor().speaks)
