import base64
import copy
import io
import json
from pathlib import Path
import runpy
import unittest
import zipfile

MODULE = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'scripts/verify-release-evidence.py'))
SHA = 'a' * 40
FIXTURE_SHA = 'b' * 40


class ReadinessTests(unittest.TestCase):
    def setUp(self):
        self.repository = 'releaseway/npm-actions'
        self.fixture = 'releaseway/npm-actions-fixture'
        self.ci = dict(id=1, repository={'full_name': self.repository}, head_sha=SHA, path='.github/workflows/check.yml', status='completed', conclusion='success', event='push')
        self.runs = {}
        self.evidence = {}
        self.artifacts = {}
        for number, scenario, workflow, state, suffix in [(2, 'npm-stage', 'publish.yml', 'staged', 'fixture'), (3, 'npm-direct', 'direct.yml', 'published', 'native')]:
            self.runs[number] = dict(id=number, repository={'full_name': self.fixture}, head_sha=FIXTURE_SHA, path='.github/workflows/' + workflow, status='completed', conclusion='success', event='workflow_dispatch', run_attempt=2)
            self.evidence[number] = dict(schema=1, candidate_repository=self.repository, candidate_sha=SHA, fixture_repository=self.fixture, fixture_commit=FIXTURE_SHA,
                                         run_id=number, run_attempt=2, workflow=workflow, scenario=scenario, state=state, version='0.0.7-' + suffix + '.0', version_source='git-tag',
                                         integrity='sha512-' + base64.b64encode(b'x' * 64).decode(), installed_node_versions=['22', '24', '26'])
            self.artifacts[number] = [dict(id=number, name='releaseway-acceptance-2', expired=False, size_in_bytes=1000)]
        globals_ = MODULE['verify'].__globals__
        globals_['api_json'] = self.api_json
        globals_['api_bytes'] = self.api_bytes

    def api_json(self, path):
        if '/workflows/' in path:
            return {'workflow_runs': self.ci_runs if hasattr(self, 'ci_runs') else [self.ci]}
        number = int(path.split('/actions/runs/')[1].split('/')[0].split('?')[0])
        return {'artifacts': self.artifacts[number]} if '/artifacts?' in path else self.runs[number]

    def api_bytes(self, path):
        number = int(path.split('/artifacts/')[1].split('/')[0])
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w') as bundle:
            bundle.writestr('acceptance.json', json.dumps(self.evidence[number]))
        return buffer.getvalue()

    def check(self):
        return MODULE['verify'](self.repository, SHA, [2, 3])

    def test_exact_candidate_success(self):
        self.assertEqual(self.check()['candidate_sha'], SHA)

    def test_incomplete_ci_or_other_sha(self):
        for field, value in [('head_sha', 'c' * 40), ('conclusion', 'failure'), ('status', 'in_progress'), ('event', 'pull_request'), ('path', '.github/workflows/other.yml')]:
            with self.subTest(field=field):
                original = self.ci[field]
                self.ci[field] = value
                with self.assertRaises(ValueError): self.check()
                self.ci[field] = original
        self.ci_runs = []
        with self.assertRaises(ValueError): self.check()

    def test_newer_failure_blocks_old_success(self):
        failed = dict(self.ci, id=4, conclusion='failure')
        self.ci_runs = [self.ci, failed]
        with self.assertRaises(ValueError): self.check()

    def test_newer_pr_does_not_hide_push_evidence(self):
        self.ci_runs = [self.ci, dict(self.ci, id=4, event='pull_request')]
        self.assertEqual(self.check()['ci_run_id'], self.ci['id'])

    def test_acceptance_binding_and_scenarios(self):
        for field, value in [('candidate_sha', 'c' * 40), ('candidate_repository', 'other/repo'), ('fixture_commit', 'c' * 40), ('run_attempt', 1), ('run_id', 9), ('workflow', 'other.yml'), ('state', 'already-published'), ('scenario', 'npm-stage'), ('integrity', 'sha512-invalid'), ('installed_node_versions', ['24'])]:
            with self.subTest(field=field):
                original = self.evidence[3][field]
                self.evidence[3][field] = value
                with self.assertRaises(ValueError): self.check()
                self.evidence[3][field] = original
        for item in self.evidence.values(): item['version_source'] = 'package-json'
        with self.assertRaises(ValueError): self.check()

    def test_node26_is_required(self):
        self.evidence[3]['installed_node_versions'] = ['22', '24']
        with self.assertRaisesRegex(ValueError, 'Node 22/24/26'): self.check()

    def test_missing_expired_and_previous_attempt(self):
        original = copy.deepcopy(self.artifacts[3])
        for artifacts in [[], [dict(original[0], expired=True)], [dict(original[0], name='releaseway-acceptance-1')], original * 2]:
            self.artifacts[3] = artifacts
            with self.assertRaises(ValueError): self.check()

    def test_failed_or_wrong_acceptance_run(self):
        for field, value in [('conclusion', 'failure'), ('status', 'in_progress'), ('path', '.github/workflows/other.yml'), ('repository', {'full_name': 'other/repo'}), ('event', 'push')]:
            original = self.runs[3][field]
            self.runs[3][field] = value
            with self.assertRaises(ValueError): self.check()
            self.runs[3][field] = original

    def test_required_run_ids(self):
        for ids in [[], [2], [2, 2], [2, 3, 4]]:
            with self.assertRaises(ValueError): MODULE['verify'](self.repository, SHA, ids)
        with self.assertRaises(ValueError): MODULE['verify'](self.repository, 'main', [2, 3])

    def test_other_repository_policies(self):
        for repository, scenario, workflow in [('releaseway/actions', 'actions-all', 'release-notes-acceptance.yml'), ('releaseway/homebrew-actions', 'homebrew-public', 'homebrew-acceptance.yml')]:
            self.ci.update(repository={'full_name': repository}, path='.github/workflows/test.yml')
            self.runs[2].update(repository={'full_name': 'releaseway/release-fixture'}, path='.github/workflows/' + workflow)
            self.evidence[2].update(candidate_repository=repository, fixture_repository='releaseway/release-fixture', scenario=scenario, workflow=workflow, state='verified')
            self.assertEqual(MODULE['verify'](repository, SHA, [2])['candidate_repository'], repository)


if __name__ == '__main__':
    unittest.main()
