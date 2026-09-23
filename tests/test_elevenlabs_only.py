"""Routing/failure regression tests without loading torch or making paid API calls."""
import ast
import logging
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import Mock, patch


class ElevenOnlyTests(unittest.TestCase):
    def setUp(self):
        source = Path(__file__).parents[1] / 'pipeline' / 'step2_voiceover.py'
        tree = ast.parse(source.read_text(encoding='utf-8'))
        functions = [n for n in tree.body if isinstance(n, ast.FunctionDef)
                     and n.name in ('_resolve_engine', '_run_jobs_elevenlabs')]
        self.el = types.SimpleNamespace(api_key=Mock(return_value='test'),
            resolve_voice=Mock(return_value='x' * 20), preflight=Mock(return_value=(True, '', 10)),
            model=Mock(return_value='eleven_multilingual_v2'), synth=Mock())
        self.package = types.ModuleType('pipeline')
        self.package.tts_elevenlabs = self.el
        self.scope = {'log': logging.getLogger(__name__), 'Path': Path,
                      'os': __import__('os'), '_script_chars': lambda s: 10}
        exec(compile(ast.Module(body=functions, type_ignores=[]), str(source), 'exec'), self.scope)

    def test_stale_provider_preferences_cannot_choose_free_voice(self):
        with patch.dict(sys.modules, pipeline=self.package):
            for provider in ('edge', 'google', 'kokoro', 'auto', 'elevenlabs'):
                script = {'tts_engine': provider, 'tts_provider': provider}
                self.assertEqual(self.scope['_resolve_engine'](script), ('elevenlabs', 'x'*20, '.mp3'))

    def test_missing_key_or_budget_stops_instead_of_falling_back(self):
        with patch.dict(sys.modules, pipeline=self.package):
            self.el.api_key.return_value = ''
            with self.assertRaisesRegex(RuntimeError, 'API key'):
                self.scope['_resolve_engine']({})
            self.el.api_key.return_value = 'test'
            self.el.preflight.return_value = False, 'daily limit', 10
            with self.assertRaisesRegex(RuntimeError, 'daily limit'):
                self.scope['_resolve_engine']({})

    def test_partial_failure_never_calls_an_alternate_provider(self):
        jobs = [{'text': 'Hello', 'path': 'nonexistent-test-audio.mp3'}]
        self.el.synth.side_effect = RuntimeError('quota exceeded')
        with patch.dict(sys.modules, pipeline=self.package), patch('time.sleep'):
            with self.assertRaisesRegex(RuntimeError, 'No alternate voice provider'):
                self.scope['_run_jobs_elevenlabs'](jobs, 'x'*20, {})

    def test_success_keeps_elevenlabs_usage_metadata(self):
        self.el.synth.return_value = [], 10
        jobs = [{'text': 'Hello', 'path': 'nonexistent-test-audio.mp3'}]
        with patch.dict(sys.modules, pipeline=self.package):
            result = self.scope['_run_jobs_elevenlabs'](jobs, 'x'*20, {})
        self.assertEqual(result[0][0]['tts_engine'], 'elevenlabs')
        self.assertEqual(result[0][0]['tts_credits'], 10)


if __name__ == '__main__':
    unittest.main()
