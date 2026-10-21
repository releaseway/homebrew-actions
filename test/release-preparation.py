"""Publication preparation freezes one approval and keeps failures atomic."""
import contextlib
import copy
import os
from pathlib import Path
import runpy
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
F = runpy.run_path(str(ROOT / 'test/release-reuse.py'))


class PreparationTests(unittest.TestCase):
    def setUp(self):
        self.h = F['ReuseTests']()
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)
        self.module = runpy.run_path(str(ROOT / 'scripts/prepare-release-evidence.py'))
        self.reuse = runpy.run_path(str(ROOT / 'scripts/release-reuse.py'))
        self.archive = self.module['A']
        self.archive['tag_commit'] = lambda *a: self.h.b
        self.archive['release'] = lambda *a, **k: None
        self.reuse['load_anchor'] = lambda *a, **k: self.h.anchor
        self.reuse['C']['collect'] = lambda *a, **k: self.h.docs_ci
        self.output = self.h.root / 'output.json'
        self.output.write_bytes(b'preserved')
        original = self.module['runpy'].run_path
        self.original_load = original
        def load(path):
            return self.reuse if str(path).endswith('/release-reuse.py') else original(path)
        self.loader = patch.object(self.module['runpy'], 'run_path', side_effect=load)
        self.loader.start()
        self.addCleanup(self.loader.stop)
        previous = Path.cwd()
        os.chdir(self.h.root)
        self.addCleanup(os.chdir, previous)

    def prepare(self, **kwargs):
        return self.module['prepare'](self.h.repository, 'v1.2.4', self.h.b, [], None, self.output, **kwargs)

    def test_equivalent_prepare_draft_retry_and_immutable_reconciliation(self):
        first = self.prepare()
        self.assertEqual(first['verification'], 'equivalent')
        self.assertEqual(first['original-sha'], self.h.a)
        raw = self.output.read_bytes()
        self.archive['release'] = lambda *a, **k: dict(draft=True, assets=[dict(name='releaseway-evidence.json')])
        self.archive['download_bundle'] = lambda *a, **k: raw
        self.assertEqual(self.prepare()['state'], 'prepared')
        self.assertEqual(self.output.read_bytes(), raw)
        self.archive['release'] = lambda *a, **k: dict(draft=False)
        def authenticate(repo, tag, candidate, output):
            output.write_bytes(raw)
            return dict(release_url='https://example.test/release')
        self.archive['authenticate'] = authenticate
        self.assertEqual(self.prepare()['state'], 'existing')
        self.assertEqual(self.output.read_bytes(), raw)

    def test_full_creation_wraps_candidate_ci_and_does_not_repeat_suites(self):
        self.archive['tag_commit'] = lambda *a: self.h.a
        def collect(repository, tag, candidate, ids, distribution, output):
            output.write_bytes(F['B']['canonical'](self.h.full['full']))
            return dict(state='prepared')
        self.module['prepare'].__globals__['_prepare_legacy'] = collect
        result = self.module['prepare'](self.h.repository, 'v1.2.3', self.h.a, [2, 3], None, self.output)
        self.assertEqual(result['verification'], 'full')
        self.assertEqual(F['B']['parse_json'](self.output.read_bytes())['schema'], 2)
        # A substituted wrong identity is rejected before it replaces an existing output.
        self.archive['tag_commit'] = lambda *a: self.h.b
        frozen = self.output.read_bytes()
        with self.assertRaises(ValueError):
            self.module['prepare'](self.h.repository, 'v1.2.4', self.h.b, [2, 3], None, self.output)
        self.assertEqual(self.output.read_bytes(), frozen)

    def test_pending_full_rechecks_current_attempts_and_verifier(self):
        inner = self.h.full['full']
        raw = F['B']['canonical'](self.h.full)
        self.archive['tag_commit'] = lambda *a: self.h.a
        self.archive['release'] = lambda *a, **k: dict(draft=True, assets=[dict(name='releaseway-evidence.json')])
        self.archive['download_bundle'] = lambda *a, **k: raw
        self.reuse['C']['collect'] = lambda *a, **k: self.h.ci
        provider = self.original_load(str(ROOT / 'scripts/verify-release-evidence.py'))
        provider['api_json'] = lambda path: copy.deepcopy(inner['snapshots'][path])
        provider['verify'].__globals__['api_json'] = provider['api_json']
        self.loader.stop()
        def load(path):
            if str(path).endswith('/verify-release-evidence.py'):
                return provider
            if str(path).endswith('/release-reuse.py'):
                return self.reuse
            return self.original_load(path)
        def pending():
            return self.module['prepare'](self.h.repository, 'v1.2.3', self.h.a, [], None, self.output)
        with patch.object(self.module['runpy'], 'run_path', side_effect=load):
            self.assertEqual(pending()['state'], 'prepared')
            self.assertEqual(self.output.read_bytes(), raw)
            def changed(path):
                value = copy.deepcopy(inner['snapshots'][path])
                if path.endswith('/runs/2'):
                    value['run_attempt'] += 1
                return value
            provider['verify'].__globals__['api_json'] = changed
            provider['api_json'] = changed
            with self.assertRaisesRegex(ValueError, 'archive_conflict'):
                pending()
            self.assertEqual(self.output.read_bytes(), raw)
            damaged = copy.deepcopy(self.h.full)
            damaged['checker']['files']['ci-contract.py'] = 'f' * 64
            self.archive['download_bundle'] = lambda *a, **k: F['B']['canonical'](damaged)
            with self.assertRaisesRegex(ValueError, 'verifier changed'):
                pending()
            self.assertEqual(self.output.read_bytes(), raw)

    def test_pending_attempt_change_missing_baseline_and_forced_full_do_not_overwrite(self):
        self.prepare()
        raw = self.output.read_bytes()
        self.archive['release'] = lambda *a, **k: dict(draft=True, assets=[dict(name='releaseway-evidence.json')])
        self.archive['download_bundle'] = lambda *a, **k: raw
        changed = copy.deepcopy(self.h.docs_ci)
        changed['run']['run_attempt'] += 1
        self.reuse['C']['collect'] = lambda *a, **k: changed
        with self.assertRaisesRegex(ValueError, 'archive_conflict'):
            self.prepare()
        self.assertEqual(self.output.read_bytes(), raw)
        self.archive['release'] = lambda *a, **k: None
        self.reuse['load_anchor'] = lambda *a, **k: (_ for _ in ()).throw(ValueError('not authenticated'))
        with self.assertRaisesRegex(ValueError, 'requires-full'):
            self.prepare()
        with self.assertRaisesRegex(ValueError, 'requires-full'):
            self.prepare(force_full=True)
        self.assertEqual(self.output.read_bytes(), raw)


if __name__ == '__main__':
    unittest.main()
