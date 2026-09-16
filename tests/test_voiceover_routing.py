"""Voice routing, ElevenLabs spend safety and fallback recording. No network calls.

Run from the engine root:  python -m unittest tests.test_voiceover_routing -v
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pipeline import step2_voiceover as vo          # noqa: E402
from pipeline import tts_elevenlabs as el           # noqa: E402


class Routing(unittest.TestCase):
    def env(self, **kv):
        base = {"TTS_PROVIDER": "", "ELEVENLABS_API_KEY": "", "ELEVENLABS_VOICE_ID": "",
                "ELEVENLABS_VOICE_ARABIC": "", "GOOGLE_TTS_API_KEY": ""}
        base.update(kv)
        return patch.dict(os.environ, base)

    def test_elevenlabs_outranks_frontend_google_default(self):
        with self.env(TTS_PROVIDER="elevenlabs", ELEVENLABS_API_KEY="k", ELEVENLABS_VOICE_ARABIC="ar-voice",
                      GOOGLE_TTS_API_KEY="g"):
            self.assertEqual(vo._resolve_engine({"language": "Arabic", "tts_engine": "google"}),
                             ("elevenlabs", "ar-voice", ".mp3"))

    def test_explicit_edge_opts_out(self):
        with self.env(TTS_PROVIDER="elevenlabs", ELEVENLABS_API_KEY="k", ELEVENLABS_VOICE_ID="v"):
            self.assertEqual(vo._resolve_engine({"language": "Arabic", "tts_engine": "edge"})[0], "edge")

    def test_missing_voice_falls_back_to_normal_routing(self):
        with self.env(TTS_PROVIDER="elevenlabs", ELEVENLABS_API_KEY="k", GOOGLE_TTS_API_KEY="g"):
            self.assertEqual(vo._resolve_engine({"language": "Arabic", "tts_engine": "google"})[0], "google")

    def test_voice_precedence_script_then_language_sex_then_default(self):
        with self.env(ELEVENLABS_VOICE_ID="any", ELEVENLABS_VOICE_ARABIC="ar"):
            os.environ["ELEVENLABS_VOICE_ARABIC_MALE"] = "ar-m"
            try:
                self.assertEqual(el.resolve_voice({"language": "Arabic", "voice_sex": "male"}), "ar-m")
                self.assertEqual(el.resolve_voice({"language": "Arabic", "voice_sex": "female"}), "ar")
                self.assertEqual(el.resolve_voice({"language": "Turkish"}), "any")
                self.assertEqual(el.resolve_voice({"language": "Arabic", "elevenlabs_voices": {"Arabic": "ui"}}), "ui")
            finally:
                del os.environ["ELEVENLABS_VOICE_ARABIC_MALE"]


class Ledger(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.p = patch.object(el, "LEDGER", Path(self.tmp.name) / "u.sqlite3"); self.p.start()

    def tearDown(self):
        self.p.stop(); self.tmp.cleanup()

    def test_daily_ceiling_blocks_before_calling(self):
        with patch.dict(os.environ, {"ELEVENLABS_DAILY_CHARACTER_LIMIT": "10", "ELEVENLABS_API_KEY": "k"}), \
                patch.object(el.requests, "post") as post:
            with self.assertRaises(el.BudgetError):
                el.synth("x" * 11, str(Path(self.tmp.name) / "a.mp3"), "v", {"language": "Arabic"}, {})
            post.assert_not_called()

    def test_account_error_opens_circuit_and_stops_calls(self):
        bad = Mock(status_code=401, json=lambda: {"detail": {"status": "quota_exceeded"}}, headers={})
        circuit = {}
        with patch.dict(os.environ, {"ELEVENLABS_API_KEY": "k"}), patch.object(el.requests, "post", return_value=bad) as post:
            with self.assertRaises(el.AccountError):
                el.synth("hello there", str(Path(self.tmp.name) / "a.mp3"), "v", {}, circuit)
            with self.assertRaises(el.AccountError):
                el.synth("hello again", str(Path(self.tmp.name) / "b.mp3"), "v", {}, circuit)
            self.assertEqual(post.call_count, 1)

    def test_timeout_is_not_retried_and_keeps_reservation(self):
        with patch.dict(os.environ, {"ELEVENLABS_API_KEY": "k"}), \
                patch.object(el.requests, "post", side_effect=el.requests.Timeout) as post:
            with self.assertRaises(el.requests.Timeout):
                el.synth("hello there", str(Path(self.tmp.name) / "a.mp3"), "v", {}, {})
            self.assertEqual(post.call_count, 1)
        self.assertGreater(el.usage_today()["reserved"], 0)

    def test_success_records_real_credits_and_sentence_marks(self):
        import base64
        ok = Mock(status_code=200, headers={"character-cost": "9"}, json=lambda: {
            "audio_base64": base64.b64encode(b"\xff" * 400).decode(),
            "alignment": {"characters": list("Hi you. Bye"),
                          "character_start_times_seconds": [i * 0.1 for i in range(11)],
                          "character_end_times_seconds": [i * 0.1 + 0.1 for i in range(11)]}})
        with patch.dict(os.environ, {"ELEVENLABS_API_KEY": "k"}), patch.object(el.requests, "post", return_value=ok):
            marks, credits = el.synth("Hi you. Bye", str(Path(self.tmp.name) / "a.mp3"), "v", {}, {})
        self.assertEqual(credits, 9.0)
        self.assertEqual([m["text"] for m in marks], ["Hi you.", "Bye"])
        self.assertAlmostEqual(marks[1]["start"], 0.8)
        self.assertEqual(el.usage_today()["credits"], 9.0)


class Fallbacks(unittest.TestCase):
    def jobs(self, d):
        return [{"type": "fact", "number": i, "text": f"segment {i}", "path": str(Path(d) / f"f{i}.wav")} for i in range(3)]

    def test_google_billing_error_stops_after_one_call_and_records_edge(self):
        denied = Mock(status_code=403, text="denied", json=lambda: {"error": {
            "status": "PERMISSION_DENIED", "details": [{"reason": "BILLING_DISABLED"}]}})
        with tempfile.TemporaryDirectory() as d, patch("requests.post", return_value=denied) as post, \
                patch.object(vo, "_run_jobs", side_effect=lambda jobs, voice: _fake_edge(jobs)):
            out = vo._run_jobs_google(self.jobs(d), "ar-XA-Chirp3-HD-Kore", {"language": "Arabic"})
        self.assertEqual(post.call_count, 1)
        self.assertEqual({j["tts_engine"] for j, _ in out}, {"edge"})
        self.assertTrue(all(j["path"].endswith(".mp3") for j, _ in out))
        self.assertIn("BILLING_DISABLED", out[0][0]["tts_fallback_reason"])

    def test_run_reports_actual_engine_not_requested(self):
        with tempfile.TemporaryDirectory() as d:
            cwd = os.getcwd(); os.chdir(d)
            try:
                script = {"language": "Arabic", "hook": "a", "facts": [{"number": 1, "text": "b"}], "outro": "c",
                          "created_at": "1"}
                def fake_google(jobs, voice, s):
                    return vo._edge_fallback(jobs, s, "google")
                with patch.object(vo, "_resolve_engine", return_value=("google", "ar-XA-Chirp3-HD-Kore", ".wav")), \
                        patch.object(vo, "_run_jobs_google", side_effect=fake_google), \
                        patch.object(vo, "_run_jobs", side_effect=lambda jobs, voice: _fake_edge(jobs)):
                    out = vo.run(script)
            finally:
                os.chdir(cwd)
        self.assertEqual(out["tts_requested_engine"], "google")
        self.assertEqual(out["tts_engine"], "edge")
        self.assertEqual({s["tts_engine"] for s in out["audio_segments"]}, {"edge"})


def _fake_edge(jobs):
    for j in jobs:
        Path(j["path"]).parent.mkdir(parents=True, exist_ok=True)
        Path(j["path"]).write_bytes(b"mp3")
    return [(j, [{"text": j["text"], "start": 0, "dur": 1}]) for j in jobs]


if __name__ == "__main__":
    unittest.main()
