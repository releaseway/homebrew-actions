"""New approvals keep old archives intact and reject forged equivalence."""
import copy
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
R = runpy.run_path(str(ROOT / 'scripts/release-reuse.py'))
B = R['B']
C = R['C']
F = runpy.run_path(str(ROOT / 'test/evidence-bundle.py'))


class ReuseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.git('init', '-q', '-b', 'main')
        for key, value in [('user.name', 'test'), ('user.email', 'test@example.invalid'),
                           ('commit.gpgsign', 'false'), ('core.hooksPath', '/dev/null')]:
            self.git('config', key, value)
        (self.root / 'source.py').write_text('print(1)')
        (self.root / 'README.md').write_text('old prose')
        self.a = self.commit()
        h = F['BundleTests']()
        h.setUp()
        h.h.ci['head_sha'] = self.a
        h.collection['workflow']['head_sha'] = self.a
        for evidence in h.h.evidence.values():
            evidence['candidate_sha'] = self.a
        for number in (2, 3):
            h.raw[number] = h.h.api_bytes(f'repos/fixture/actions/artifacts/{number}/zip')
            h.h.artifacts[number][0]['digest'] = 'sha256:' + B['sha256'](h.raw[number])
        self.repository = h.h.repository
        jobs = [dict(id=index + 100, name=name, status='completed', conclusion='success',
                     started_at='2026-10-21T00:00:01Z', completed_at='2026-10-21T00:10:00Z',
                     runner_name='fixture', runner_group_name='', labels=['ubuntu-24.04'])
                for index, name in enumerate(sorted(C['required_jobs'](self.repository, 'full')))]
        previous = h.get

        def get(path):
            if '/runs/1/attempts/1/jobs?' in path:
                return dict(jobs=jobs)
            return previous(path)
        h.module['verify'].__globals__['api_json'] = get
        original = B['collect'](h.module, self.repository, self.a, [2, 3], None, 'v1.2.3',
                               B['checker_identity'](self.repository, self.a), h.collection)[1]
        self.ci = dict(contract=C['CONTRACT'], mode='full', run=B['run_snapshot'](h.h.ci), jobs=jobs)
        self.full = R['full'](original, self.root, self.ci)
        self.anchor = dict(identity=dict(repository=self.repository, tag='v1.2.3', sha=self.a,
            release_id=11, asset_id=12, sha256=B['sha256'](B['canonical'](self.full))), approval=self.full)
        (self.root / 'README.md').write_text('new prose\n\nmore spacing')
        self.b = self.commit()
        self.docs_ci = dict(contract=C['CONTRACT'], mode='docs',
            run=dict(self.ci['run'], id=31, head_sha=self.b),
            jobs=[dict(job, name=name) for job, name in zip(jobs, sorted(C['required_jobs'](self.repository, 'docs')))])

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.root), *args], stderr=subprocess.DEVNULL, text=True).strip()

    def commit(self):
        self.git('add', '-A')
        self.git('commit', '-q', '-m', 'fixture')
        return self.git('rev-parse', 'HEAD')

    def make(self):
        return R['equivalent'](self.repository, 'v1.2.4', self.b, self.root, self.anchor, self.docs_ci)

    def verify(self, value):
        return B['verify_bundle'](value, self.repository, 'v1.2.4', self.b)

    def test_full_and_equivalent_replay_without_network_or_git(self):
        value = self.make()
        self.assertEqual(self.verify(value)['verification'], 'equivalent')
        self.assertEqual(value['baseline']['approval']['full']['candidate']['sha'], self.a)
        # Historical timestamp and expired original Actions artifacts do not cause live lookups.
        path = self.root / 'evidence.json'
        path.write_bytes(B['canonical'](value))
        result = subprocess.run([sys.executable, str(ROOT / 'scripts/verify-evidence-bundle.py'),
            '--file', str(path), '--repository', self.repository, '--tag', 'v1.2.4', '--candidate', self.b],
            env={'PATH': ''}, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr.decode())

    def test_policy_source_ci_anchor_and_original_byte_mutations_are_rejected(self):
        value = self.make()
        for mutate in [
            lambda b: b['baseline']['identity'].__setitem__('repository', 'other/repo'),
            lambda b: b['baseline']['identity'].__setitem__('sha256', '0' * 64),
            lambda b: b['baseline']['approval'].__setitem__('verification', 'equivalent'),
            lambda b: b['policy'].__setitem__('sha256', '0' * 64),
            lambda b: b['proof']['candidate']['trees'].clear(),
            lambda b: b['ci']['run'].__setitem__('head_sha', self.a),
            lambda b: b['ci']['run'].__setitem__('conclusion', 'failure'),
            lambda b: b['ci']['jobs'].pop(),
            lambda b: b['checker']['files'].__setitem__('release-inputs.py', 'f' * 64),
        ]:
            damaged = copy.deepcopy(value)
            mutate(damaged)
            with self.assertRaises((ValueError, KeyError)):
                self.verify(damaged)

    def test_pr_smoke_cannot_satisfy_full_and_input_changes_cannot_reuse(self):
        bad = copy.deepcopy(self.full)
        bad['ci']['mode'] = 'smoke'
        with self.assertRaises(ValueError):
            B['verify_bundle'](bad, self.repository, 'v1.2.3', self.a)
        bad = copy.deepcopy(self.full)
        bad['ci']['jobs'] = [job for job in bad['ci']['jobs'] if job['name'] != 'native (windows-11-arm, 26)']
        with self.assertRaises(ValueError):
            B['verify_bundle'](bad, self.repository, 'v1.2.3', self.a)
        with self.assertRaisesRegex(ValueError, 'native'):
            C['verify'](bad['ci'], self.repository, self.a, 'full')
        (self.root / 'source.py').write_text('print(2)')
        changed = self.commit()
        with self.assertRaises(ValueError):
            R['equivalent'](self.repository, 'v1.2.4', changed, self.root, self.anchor, self.docs_ci)

    def test_explicit_revocation_blocks_new_reuse(self):
        original = R['equivalent'].__globals__['revoked']
        R['equivalent'].__globals__['revoked'] = lambda *args: True
        try:
            with self.assertRaisesRegex(ValueError, 'revoked'):
                self.make()
        finally:
            R['equivalent'].__globals__['revoked'] = original


if __name__ == '__main__':
    unittest.main()
