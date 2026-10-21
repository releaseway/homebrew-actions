"""CI role, mode selection and aggregate regression checks."""
import copy
import json
import os
from pathlib import Path
import runpy
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
C = runpy.run_path(str(ROOT / 'scripts/ci-contract.py'))
G = runpy.run_path(str(ROOT / 'scripts/ci-gate.py'))
M = runpy.run_path(str(ROOT / 'scripts/ci-mode.py'))
F = runpy.run_path(str(ROOT / 'test/release-reuse.py'))


class CiTests(unittest.TestCase):
    def test_documentation_checks_syntax_and_local_files_without_prose_matching(self):
        checker = runpy.run_path(str(ROOT / 'scripts/check-docs.py'))
        function = checker['verify']
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            function.__globals__['ROOT'] = root
            (root / 'existing.md').write_text('target')
            guide = root / 'guide.md'
            fence = chr(96) * 3
            guide.write_text('Any new title\n\n[Link](existing.md)\n' + fence + 'yaml\nkey: value\n' + fence + '\n')
            function(guide)
            guide.write_text(fence + 'yaml\nkey: [unterminated\n' + fence + '\n')
            with self.assertRaises(ValueError):
                function(guide)
            guide.write_text('[Broken](missing.md)')
            with self.assertRaises(ValueError):
                function(guide)

    def test_mode_recomputes_authentication_and_input_equivalence(self):
        fixture = F['ReuseTests']()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        tools = M['choose'].__globals__['R']
        original = tools['load_anchor']
        tools['load_anchor'] = lambda *a: fixture.anchor
        try:
            self.assertEqual(M['choose'](fixture.repository, fixture.b, fixture.root)['mode'], 'docs')
            self.assertEqual(M['choose'](fixture.repository, fixture.b, fixture.root, True)['mode'], 'full')
            tools['load_anchor'] = lambda *a: (_ for _ in ()).throw(ValueError('no authenticated baseline'))
            self.assertEqual(M['choose'](fixture.repository, fixture.b, fixture.root)['mode'], 'full')
        finally:
            tools['load_anchor'] = original

    def test_skipped_full_is_only_acceptable_in_documentation_mode(self):
        needs = dict(inputs=dict(result='success'), docs=dict(result='success'), static=dict(result='skipped'))
        G['verify']('docs', needs)
        with self.assertRaises(ValueError):
            G['verify']('full', needs)
        for mode in ('docs', 'full', ''):
            with self.assertRaises(ValueError):
                G['verify'](mode, dict(needs, docs=dict(result='failure')))

    def test_ci_attempt_change_and_missing_full_coverage_are_rejected(self):
        fixture = F['ReuseTests']()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        ci = fixture.ci
        module = dict(require_ci=lambda *a: ci['run'], pages=lambda *a: iter(ci['jobs']),
                      api_json=lambda *a: dict(ci['run'], run_attempt=2))
        with patch('runpy.run_path', return_value=module), self.assertRaisesRegex(ValueError, 'changed'):
            C['collect'](fixture.repository, fixture.a, 'full')
        bad = copy.deepcopy(ci)
        bad['jobs'] = [job for job in bad['jobs'] if job['name'] != 'musl (26)']
        with self.assertRaises(ValueError):
            C['verify'](bad, fixture.repository, fixture.a, 'full')
        bad = copy.deepcopy(ci)
        bad['run']['event'] = 'pull_request'
        with self.assertRaises(ValueError):
            C['verify'](bad, fixture.repository, fixture.a, 'full')

    def test_stable_aggregate_covers_every_job_and_tags_have_separate_workflow(self):
        workflow = 'check.yml' if ROOT.name == 'npm-actions' else 'test.yml'
        def load(name):
            raw = subprocess.check_output(['ruby', '-ryaml', '-rjson', '-e',
                'puts JSON.generate(YAML.load_file(ARGV.fetch(0), aliases: true))',
                str(ROOT / '.github/workflows' / name)], text=True)
            return json.loads(raw)
        model = load(workflow)
        triggers = model.get('on', model.get('true'))
        self.assertIn('branches', triggers['push'])
        self.assertNotIn('paths-ignore', triggers['push'])
        gate = model['jobs']['validation']
        self.assertEqual(gate.get('name', 'validation'), 'validation')
        self.assertEqual(gate['if'], 'always()')
        self.assertEqual(set(gate['needs']), set(model['jobs']) - {'validation'})
        tag = load('tag.yml')
        self.assertEqual(len(tag['jobs']), 1)
        self.assertIn('tags', tag.get('on', tag.get('true'))['push'])
        self.assertNotIn('pnpm test', json.dumps(tag))


if __name__ == '__main__':
    unittest.main()
