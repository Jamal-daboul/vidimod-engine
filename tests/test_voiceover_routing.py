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


class Isolated(unittest.TestCase):
    """Temp ledger + no network for the account-balance lookup."""
    remaining = None

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        ledger = Path(self.tmp.name) / "u.sqlite3"
        self.patches = [patch.object(el, "LEDGER", ledger),
                        patch.object(el, "_ACCOUNT_CACHE", Path(self.tmp.name) / "acct.json"),
                        patch.object(el, "account_remaining", side_effect=lambda: self.remaining)]
        for x in self.patches:
            x.start()

    def tearDown(self):
        for x in self.patches:
            x.stop()
        self.tmp.cleanup()


class Routing(Isolated):
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


class Ledger(Isolated):

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


class Guards(Isolated):
    EL = {"TTS_PROVIDER": "", "ELEVENLABS_API_KEY": "k", "ELEVENLABS_VOICE_ARABIC": "adminVoiceAAAAAAAAAAA",
          "ELEVENLABS_DAILY_CHARACTER_LIMIT": "", "GOOGLE_TTS_API_KEY": ""}

    def script(self, chars=100, **kv):
        return {"language": "Arabic", "hook": "x" * chars, "facts": [], "outro": "", **kv}

    def test_user_picked_voice_wins_over_admin_voice(self):
        with patch.dict(os.environ, self.EL):
            eng, vid, _ = vo._resolve_engine(self.script(tts_engine="elevenlabs", voice="rPNcQ53R703tTmtue1AT"))
        self.assertEqual((eng, vid), ("elevenlabs", "rPNcQ53R703tTmtue1AT"))

    def test_chirp_name_is_never_treated_as_a_voice_id(self):
        with patch.dict(os.environ, self.EL):
            self.assertEqual(el.resolve_voice({"language": "Arabic", "tts_engine": "elevenlabs", "voice": "Callirrhoe"}),
                             "adminVoiceAAAAAAAAAAA")

    def test_video_over_per_video_limit_uses_free_voice_for_the_whole_render(self):
        s = self.script(chars=2000, tts_engine="elevenlabs", voice="rPNcQ53R703tTmtue1AT")
        with patch.dict(os.environ, self.EL):
            eng, _, _ = vo._resolve_engine(s)
        self.assertEqual(eng, "edge")
        self.assertIn("per-video limit", s["tts_fallback_reason"])

    def test_owner_limits_on_script_override_defaults(self):
        s = self.script(chars=2000, tts_engine="elevenlabs", voice="rPNcQ53R703tTmtue1AT",
                        elevenlabs_limits={"per_video": 5000, "daily": 9000})
        with patch.dict(os.environ, self.EL):
            self.assertEqual(vo._resolve_engine(s)[0], "elevenlabs")

    def test_account_reserve_protects_the_balance(self):
        self.remaining = 1100            # 1100 left, reserve 1000 → a 200-credit video must not run
        s = self.script(chars=200, tts_engine="elevenlabs", voice="rPNcQ53R703tTmtue1AT")
        with patch.dict(os.environ, self.EL):
            self.assertEqual(vo._resolve_engine(s)[0], "edge")
        self.assertIn("balance protected", s["tts_fallback_reason"])

    def test_estimate_learns_the_real_rate_from_the_ledger(self):
        model_id = "eleven_multilingual_v2"
        self.assertEqual(el.estimate(1000, model_id), 1000)               # no history → list rate
        for i in range(5):
            rid = el._reserve("v", model_id, "x" * 100)
            el._finish(rid, "ok", credits=25)                             # measured 0.25/char
        with el._db() as con:
            con.execute("UPDATE calls SET chars=100")
        self.assertAlmostEqual(el.credit_ratio(model_id), 0.25)
        self.assertAlmostEqual(el.estimate(1000, model_id), 312.5)        # +25% margin

    def test_human_recorded_shots_are_not_counted(self):
        s = {"hook": "a" * 50, "facts": [{"number": 1, "text": "b" * 70}], "outro": "c" * 30,
             "human_voices": {"fact_1": "/x.wav"}}
        self.assertEqual(vo._script_chars(s), 80)


class OneVoicePerVideo(Isolated):
    """The reported bug: a 409 on the hook sent ONE line to edge-tts mid-video."""
    def ok(self, text):
        import base64
        return Mock(status_code=200, headers={"character-cost": str(len(text) // 4)}, json=lambda: {
            "audio_base64": base64.b64encode(bytes([255]) * 400).decode(),
            "alignment": {"characters": list(text), "character_start_times_seconds": [i * .05 for i in range(len(text))],
                          "character_end_times_seconds": [i * .05 + .05 for i in range(len(text))]}})

    def conflict(self):
        return Mock(status_code=409, headers={}, json=lambda: {"detail": {"status": "already_running"}})

    def jobs(self, d, n=4):
        return [{"type": "fact", "number": i, "text": f"segment number {i} here.", "path": str(Path(d) / f"f{i}.mp3")}
                for i in range(n)]

    def test_409_already_running_is_retried_not_failed(self):
        with patch.dict(os.environ, {"ELEVENLABS_API_KEY": "k"}), patch("time.sleep"),                 patch.object(el.requests, "post", side_effect=[self.conflict(), self.ok("hello there you")]) as post:
            marks, credits = el.synth("hello there you", str(Path(self.tmp.name) / "a.mp3"), "v", {}, {})
        self.assertEqual(post.call_count, 2)
        self.assertEqual(credits, 3.0)

    def test_transient_failure_recovers_in_second_pass_all_elevenlabs(self):
        calls = {"n": 0}
        def flaky(text, path, voice, script, circuit):
            calls["n"] += 1
            if calls["n"] == 2:                      # the first parallel line fails once
                raise RuntimeError("ElevenLabs HTTP 500")
            Path(path).write_bytes(b"mp3")
            return [{"text": text, "start": 0, "dur": 1}], 5.0
        with tempfile.TemporaryDirectory() as d, patch.object(el, "synth", side_effect=flaky), patch("time.sleep"):
            out = vo._run_jobs_elevenlabs(self.jobs(d), "voiceIDAAAAAAAAAAAAA", {"language": "Arabic"})
        self.assertEqual({j["tts_engine"] for j, _ in out}, {"elevenlabs"})
        self.assertEqual(len(out), 4)

    def test_persistent_failure_revoices_whole_video_with_one_voice(self):
        def one_bad(text, path, voice, script, circuit):
            if "number 2" in text:
                raise RuntimeError("ElevenLabs HTTP 500")
            Path(path).write_bytes(b"mp3")
            return [{"text": text, "start": 0, "dur": 1}], 5.0
        with tempfile.TemporaryDirectory() as d, patch.object(el, "synth", side_effect=one_bad), patch("time.sleep"),                 patch.object(vo, "_run_jobs", side_effect=lambda jobs, voice: _fake_edge(jobs)):
            out = vo._run_jobs_elevenlabs(self.jobs(d), "voiceIDAAAAAAAAAAAAA", {"language": "Arabic"})
        self.assertEqual({j["tts_engine"] for j, _ in out}, {"edge"})
        self.assertEqual(len(out), 4)
        self.assertTrue(all("whole video" in j["tts_fallback_reason"] for j, _ in out))
        self.assertTrue(all("tts_credits" not in j for j, _ in out))

    def test_first_line_runs_alone_before_parallel_lines(self):
        import threading
        active, peak, first_done = {"n": 0}, {"n": 0}, threading.Event()
        lock = threading.Lock()
        def track(text, path, voice, script, circuit):
            with lock:
                active["n"] += 1
                peak["first"] = peak.get("first", active["n"]) if "number 0" in text else peak.get("first", 0)
            import time as _t
            _t.sleep(0.05)
            with lock:
                active["n"] -= 1
            Path(path).write_bytes(b"mp3")
            return [{"text": text, "start": 0, "dur": 1}], 5.0
        with tempfile.TemporaryDirectory() as d, patch.object(el, "synth", side_effect=track):
            vo._run_jobs_elevenlabs(self.jobs(d), "voiceIDAAAAAAAAAAAAA", {"language": "Arabic"})
        self.assertEqual(peak["first"], 1)           # nothing else was running during the warm-up line


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
