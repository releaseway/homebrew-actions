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
            self.h.artifacts[n][0].update(digest='sha256:' + hashlib.sha256(self.raw[n]).hexdigest())
        original = self.h.api_json
        def get(path):
            if '/jobs?' in path:
                return {'jobs': [dict(id=100, name='checks', status='completed', conclusion='success')]}
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
                         'verify-evidence-bundle.py', 'release-evidence-policies.json']:
                self.assertEqual((ROOT / 'scripts' / name).read_bytes(), (sibling / 'scripts' / name).read_bytes(), repo + '/' + name)


if __name__ == '__main__':
    unittest.main()
