"""Public evidence contract: original bytes, bounded parsing, atomic writes and offline replay."""
import base64
import copy
import hashlib
import io
import json
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[1]
B = runpy.run_path(str(ROOT / 'scripts/evidence-bundle.py'))
R = runpy.run_path(str(ROOT / 'test/release-evidence.py'))
SHA = 'a' * 40


class BundleTests(unittest.TestCase):
    def setUp(self):
        self.h = R['ReadinessTests']()
        self.h.setUp()
        self.module = R['MODULE']
        self.h.ci['run_attempt'] = 1
        self.raw = {n: self.h.api_bytes(f'repos/fixture/actions/artifacts/{n}/zip') for n in [2, 3]}
        for n in [2, 3]:
            self.h.artifacts[n][0].update(digest='sha256:' + hashlib.sha256(self.raw[n]).hexdigest(),
                created_at='2026-10-21T00:00:00Z', expires_at='2026-11-20T00:00:00Z')
        original = self.h.api_json
        def get(path):
            if '/jobs?' in path:
                return {'jobs': [dict(id=100, name='checks', status='completed', conclusion='success',
                    started_at='2026-10-21T00:00:01Z', completed_at='2026-10-21T00:10:00Z',
                    runner_name='fixture', runner_group_name='', labels=['ubuntu-24.04'])]}
            if path.endswith('/runs/1'):
                return self.h.ci
            return original(path)
        self.get = get
        self.module['verify'].__globals__['api_json'] = get
        self.module['verify'].__globals__['api_bytes'] = lambda path: self.raw[int(path.split('/artifacts/')[1].split('/')[0])]
        self.collection = dict(mode='pre-publication', collected_at='2026-10-21T00:00:00Z',
                               workflow=dict(repository=self.h.repository, run_id=10, run_attempt=1, head_sha=SHA,
                                             status='in_progress', conclusion=None))

    def collect(self):
        return B['collect'](self.module, self.h.repository, SHA, [2, 3], None, 'v1.2.3',
                            B['checker_identity'](self.h.repository, SHA), self.collection)[1]

    def verify(self, bundle):
        return B['verify_bundle'](bundle, self.h.repository, 'v1.2.3', SHA)

    def test_roundtrip_original_zip_offline_and_expiry(self):
        bundle = self.collect()
        self.assertEqual(self.verify(bundle)['state'], 'consistency_verified')
        for entry in bundle['archives'].values():
            self.assertIn(base64.b64decode(entry['base64']), self.raw.values())
        # Expiry is provider state at approval time, never a reason to discard archived bytes later.
        self.assertEqual(self.verify(copy.deepcopy(bundle))['candidate_sha'], SHA)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'releaseway-evidence.json'
            B['atomic_write'](path, B['canonical'](bundle))
            result = subprocess.run([sys.executable, str(ROOT / 'scripts/verify-evidence-bundle.py'),
                                     '--file', str(path), '--repository', self.h.repository,
                                     '--tag', 'v1.2.3', '--candidate', SHA], env={'PATH': ''}, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr.decode())

    def test_identity_policy_result_and_byte_mutations(self):
        good = self.collect()
        mutations = [
            lambda b: b['candidate'].update(sha='b' * 40),
            lambda b: b['policy'].update(sha256='0' * 64),
            lambda b: b['result'].update(ci_run_id=99),
            lambda b: b['requests'].update(acceptance_runs=[True, 3]),
            lambda b: b['archives'][next(iter(b['archives']))].update(base64='invalid!'),
            lambda b: b['archives'][next(iter(b['archives']))].update(sha256='0' * 64),
            lambda b: b['snapshots']['repos/releaseway/npm-actions/actions/runs/1'].update(run_attempt=2),
            lambda b: b['snapshots'].update(unexpected={'id': 99}),
        ]
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                bad = copy.deepcopy(good)
                mutation(bad)
                with self.assertRaises(ValueError):
                    self.verify(bad)

    def test_race_and_provider_digest_block_collection(self):
        self.h.artifacts[2][0]['digest'] = 'sha256:' + '0' * 64
        with self.assertRaisesRegex(ValueError, 'digest'):
            self.collect()
        self.setUp()
        calls = {}
        def racing(path):
            calls[path] = calls.get(path, 0) + 1
            value = copy.deepcopy(self.get(path))
            if path.endswith('/runs/2') and calls[path] > 1:
                value['run_attempt'] += 1
            return value
        self.module['verify'].__globals__['api_json'] = racing
        with self.assertRaises(ValueError):
            self.collect()

    def test_expired_source_blocks_collection(self):
        self.h.artifacts[2][0]['expired'] = True
        with self.assertRaisesRegex(ValueError, 'expired'):
            self.collect()

    def test_real_json_transport_does_not_archive_api_json_as_zip(self):
        globals_ = self.module['verify'].__globals__
        def transport(path):
            if '/artifacts/' in path and path.endswith('/zip'):
                return self.raw[int(path.split('/artifacts/')[1].split('/')[0])]
            return json.dumps(self.get(path)).encode()
        globals_['api_bytes'] = transport
        globals_['api_json'] = lambda path: B['parse_json'](globals_['api_bytes'](path))
        bundle = self.collect()
        self.assertEqual(len(bundle['archives']), 2)
        self.assertEqual(self.verify(bundle)['state'], 'consistency_verified')

    def test_json_duplicates_and_zip_contract(self):
        with self.assertRaisesRegex(ValueError, 'duplicate'):
            B['parse_json']('{"id":1,"id":2}')
        for names in [['../acceptance.json'], ['acceptance.json', 'extra'], ['acceptance.json', 'acceptance.json']]:
            raw = io.BytesIO()
            with zipfile.ZipFile(raw, 'w') as archive:
                for name in names:
                    archive.writestr(name, '{}')
            with self.assertRaises(ValueError):
                B['receipts'](raw.getvalue(), 'acceptance')
        raw = io.BytesIO()
        with zipfile.ZipFile(raw, 'w', zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('acceptance.json', 'x' * (1024 * 1024 + 1))
        with self.assertRaises(ValueError):
            B['receipts'](raw.getvalue(), 'acceptance')

    def test_provider_run_metadata_is_mandatory(self):
        good = self.collect()
        path = 'repos/releaseway/npm-actions-fixture/actions/runs/2'
        for key in ['workflow_id', 'created_at', 'updated_at', 'run_started_at']:
            bad = copy.deepcopy(good)
            del bad['snapshots'][path][key]
            with self.subTest(key=key), self.assertRaises((ValueError, KeyError)):
                self.verify(bad)
        for value in ['not-a-time', '2026-10-21T00:00:00', True]:
            bad = copy.deepcopy(good)
            bad['snapshots'][path]['created_at'] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.verify(bad)
        bad = copy.deepcopy(good); bad['collection']['collected_at'] = 'not-a-time'
        with self.assertRaises(ValueError): self.verify(bad)
        bad = copy.deepcopy(good)
        next(value for value in bad['snapshots'].values() if 'jobs' in value)['jobs'][0].pop('labels')
        with self.assertRaisesRegex(ValueError, 'metadata'): self.verify(bad)

    def test_atomic_existing_file_and_size_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'evidence.json'
            B['atomic_write'](path, b'good')
            with self.assertRaises(ValueError):
                B['atomic_write'](path, b'x' * (B['MAX_BUNDLE'] + 1))
            self.assertEqual(path.read_bytes(), b'good')
            self.assertEqual(list(Path(directory).iterdir()), [path])

    def test_common_helper_parity_when_workspace_available(self):
        for repo in ['actions', 'npm-actions', 'homebrew-actions']:
            sibling = ROOT.parent / repo
            if not (sibling / '.git').exists():
                continue
            for name in ['verify-release-evidence.py', 'verify-distribution-receipts.py', 'evidence-bundle.py',
                         'verify-evidence-bundle.py', 'release-evidence-policies.json', 'verify-release-set-receipts.py',
                         'verify-archived-release.py', 'prepare-archive-evidence.py', 'prepare-release-evidence.py',
                         'release-inputs.py', 'release-reuse.py', 'ci-contract.py', 'ci-mode.py', 'ci-gate.py',
                         'check-docs.py', 'verify-tag-ci.py', 'release-approval-revocations.json']:
                self.assertEqual((ROOT / 'scripts' / name).read_bytes(), (sibling / 'scripts' / name).read_bytes(), repo + '/' + name)
            for name in ['verify-homebrew-features.py', 'verify-homebrew-native.py']:
                self.assertEqual((ROOT / 'scripts' / name).read_bytes(), (sibling / 'scripts' / name).read_bytes(), repo + '/' + name)
        native = ROOT / 'internal/scripts/aggregate-formula-validation.py'
        if native.exists():
            self.assertEqual(native.read_bytes(), (ROOT / 'scripts/verify-homebrew-native.py').read_bytes())


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.prepare_module = runpy.run_path(str(ROOT / 'scripts/prepare-archive-evidence.py'))
        self.archive = self.prepare_module['A']
        self.prepare = self.prepare_module['prepare']
        self.expected = B['policy_data']('legacy-20261020')['releases']['releaseway/npm-actions']

    def invoke(self, **changes):
        values = dict(repository='releaseway/npm-actions', tag=self.expected['tag'],
                      publication_run=self.expected['publication_run'],
                      run_ids=self.expected['acceptance_runs'], distribution_run=None, output='unused')
        values.update(changes)
        return self.prepare(**values)

    def test_legacy_allowlist_rejects_unrelated_inputs_before_network(self):
        self.archive['release'] = lambda *a, **k: self.fail('must reject before network')
        for changes in [dict(tag='v0.7.2'), dict(publication_run=1), dict(run_ids=[1, 2]), dict(distribution_run=1)]:
            with self.assertRaisesRegex(ValueError, 'allowlisted'):
                self.invoke(**changes)

    def test_frozen_sidecar_is_authenticated_without_original_api(self):
        calls = []
        self.archive['release'] = lambda repo, tag, **kw: {'draft': False}
        self.archive['authenticate'] = lambda *a: calls.append(a)
        self.archive['tag_commit'] = lambda *a: 'c' * 40
        result = self.invoke()
        self.assertEqual(result, dict(state='existing', collector='c' * 40))
        self.assertEqual(len(calls), 1)

    def test_draft_missing_asset_is_explicit_conflict(self):
        self.archive['release'] = lambda *a, **k: {'draft': True, 'assets': []}
        with self.assertRaisesRegex(ValueError, 'archive_conflict'):
            self.invoke()

    def test_sidecar_workflow_keeps_stable_latest_and_scope(self):
        workflow = (ROOT / '.github/workflows/archive-release-evidence.yml').read_text()
        for text in ['prerelease: "true"', 'latest: "false"', 'mode: tag', 'commit: ${{ steps.archive.outputs.collector }}',
                     'assets: releaseway-evidence.json', 'notes: file', 'verify-archived-release.py']:
            self.assertIn(text, workflow)
        self.assertNotIn('pull_request', workflow)


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.h = BundleTests()
        self.h.setUp()
        self.bundle = self.h.collect()
        self.h.module['api_json'] = self.h.get
        self.raw = B['canonical'](self.bundle)
        self.module = runpy.run_path(str(ROOT / 'scripts/prepare-release-evidence.py'))
        self.archive = self.module['A']
        self.archive['tag_commit'] = lambda *args: SHA
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.output = Path(self.directory.name) / 'releaseway-evidence.json'
        B['atomic_write'](self.output, b'preserved')

    def prepare(self, ids=None):
        return self.module['prepare']('releaseway/npm-actions', 'v1.2.3', SHA,
                                      [2, 3] if ids is None else ids, None, self.output)

    def test_published_archive_reconciles_without_actions_api(self):
        self.archive['release'] = lambda *a, **k: dict(draft=False)
        def authenticate(repo, tag, candidate, output):
            B['atomic_write'](output, self.raw)
            return {'release_url': 'https://example.test/release'}
        self.archive['authenticate'] = authenticate
        result = self.prepare()
        self.assertEqual(result['state'], 'existing')
        self.assertEqual(self.output.read_bytes(), self.raw)

    def test_published_missing_or_changed_approval_preserves_existing_file(self):
        self.archive['release'] = lambda *a, **k: dict(draft=False)
        def authenticate(repo, tag, candidate, output):
            B['atomic_write'](output, self.raw)
            return {'release_url': 'https://example.test/release'}
        self.archive['authenticate'] = authenticate
        with self.assertRaisesRegex(ValueError, 'archive_conflict'):
            self.prepare(ids=[4, 5])
        self.assertEqual(self.output.read_bytes(), b'preserved')
        def missing(*args):
            raise ValueError('archive asset missing')
        self.archive['authenticate'] = missing
        with self.assertRaisesRegex(ValueError, 'missing'):
            self.prepare()
        self.assertEqual(self.output.read_bytes(), b'preserved')

    def test_draft_reuses_first_approval_bytes_across_collection_clocks(self):
        self.archive['release'] = lambda *a, **k: dict(draft=True, assets=[dict(name='releaseway-evidence.json')])
        self.archive['download_bundle'] = lambda *a, **k: self.raw
        # The live run metadata is still checked, but no ZIPs are downloaded again.
        module = self.h.module
        original_run_path = self.module['runpy'].run_path
        def load(path):
            if str(path).endswith('/verify-release-evidence.py'):
                return module
            return original_run_path(path)
        from unittest.mock import patch
        with patch.object(self.module['runpy'], 'run_path', side_effect=load):
            self.assertEqual(self.prepare()['state'], 'prepared')
            self.assertEqual(self.prepare()['state'], 'prepared')
        self.assertEqual(self.output.read_bytes(), self.raw)

    def test_changed_draft_attempt_blocks_publication(self):
        self.archive['release'] = lambda *a, **k: dict(draft=True, assets=[dict(name='releaseway-evidence.json')])
        self.archive['download_bundle'] = lambda *a, **k: self.raw
        self.h.h.runs[2]['run_attempt'] += 1
        original_run_path = self.module['runpy'].run_path
        def load(path):
            return self.h.module if str(path).endswith('/verify-release-evidence.py') else original_run_path(path)
        from unittest.mock import patch
        with patch.object(self.module['runpy'], 'run_path', side_effect=load):
            with self.assertRaisesRegex(ValueError, 'archive_conflict'):
                self.prepare()
        self.assertEqual(self.output.read_bytes(), b'preserved')

    def test_official_workflow_gates_publish_and_authenticates_after(self):
        workflow = (ROOT / '.github/workflows/release.yml').read_text()
        self.assertLess(workflow.index('prepare-release-evidence.py'), workflow.index('name: Publish immutable'))
        self.assertLess(workflow.index('name: Publish immutable'), workflow.index('verify-archived-release.py'))
        self.assertIn('assets: releaseway-evidence.json', workflow)
        self.assertIn("if: steps.evidence.outputs.state == 'prepared'", workflow)

    def test_fresh_and_partial_empty_draft_collect_and_block_on_failure(self):
        from unittest.mock import patch
        import os
        original_run_path = self.module['runpy'].run_path
        def load(path):
            return self.h.module if str(path).endswith('/verify-release-evidence.py') else original_run_path(path)
        env = dict(GITHUB_SHA=SHA, GITHUB_RUN_ID='10', GITHUB_RUN_ATTEMPT='1')
        with patch.object(self.module['runpy'], 'run_path', side_effect=load), patch.dict(os.environ, env):
            for observed in [None, dict(draft=True, assets=[])]:
                self.archive['release'] = lambda *a, **k: observed
                self.assertEqual(self.module['_prepare_legacy']('releaseway/npm-actions', 'v1.2.3', SHA,
                                 [2, 3], None, self.output)['state'], 'prepared')
                self.assertEqual(B['parse_json'](self.output.read_bytes())['collection']['workflow']['status'], 'in_progress')
            B['atomic_write'](self.output, b'preserved')
            self.h.h.artifacts[2][0]['expired'] = True
            with self.assertRaisesRegex(ValueError, 'expired'):
                self.module['_prepare_legacy']('releaseway/npm-actions', 'v1.2.3', SHA, [2, 3], None, self.output)
            self.assertEqual(self.output.read_bytes(), b'preserved')


if __name__ == '__main__':
    unittest.main()
