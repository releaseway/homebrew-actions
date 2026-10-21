"""Provider identity failures and complete offline replay for the new actions gate."""
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
S = runpy.run_path(str(ROOT / 'scripts/verify-release-set-receipts.py'))
R = runpy.run_path(str(ROOT / 'test/release-evidence.py'))
SHA, SOURCE, RUN, ATTEMPT = R['SHA'], R['FIXTURE_SHA'], 4, 2
PREFIX = 'distribution-set-20261021-a8f724-v'
REPO, FIXTURE = 'releaseway/actions', 'releaseway/release-fixture'


def zip_files(files):
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as z:
        for name, value in files.items():
            z.writestr(name, value)
    return out.getvalue()


def report(phase, pages=False):
    policy = S['expected_policy'](PREFIX, phase)
    files, releases = [], []
    for i, v in enumerate(['0.9.0', '1.0.0', '1.1.0', '1.3.0-beta.1'] + (['1.2.0'] if phase == 'final' else [])):
        r = dict(releaseId=100 + i, tag=PREFIX + v, commit=SOURCE, prerelease=True, immutable=True,
                 status='skipped-empty' if v == '0.9.0' else 'selected', assets=[])
        releases.append(r)
        if v == '0.9.0':
            continue
        for j, m in enumerate(policy['files']):
            raw = S['payload'](v)[m['from']].encode(); digest = S['digest'](raw)
            asset = dict(id=1000 + i * 3 + j, name=m['from'], size=len(raw), digest=digest, state='uploaded')
            r['assets'].append(asset)
            f = dict(path=r['tag'] + '/' + m['to'], size=len(raw), sha256=digest[7:], origin='selected',
                     releaseId=r['releaseId'], assetId=asset['id'], **{'from': m['from']})
            files.append(f)
            if r['tag'] == policy['aliases']['tag'] and j < 2:
                files.append(dict(f, path=m['to'], aliasOf=f['path']))
    for path, text in S['documentation'](phase).items():
        files.append(dict(path=path, size=len(text.encode()), sha256=S['digest'](text.encode())[7:], origin='base'))
    src = dict(kind='github-release-set', repository=FIXTURE, verification='digest', policy=policy,
               policyDigest=S['digest'](S['compact'](policy)), destination='', releases=releases, aliasTag=policy['aliases']['tag'])
    result = dict(schema=2, base=True, baseline={'kind': 'local-site'}, source=src, files=files)
    if pages:
        result['contentDigest'] = S['content_digest'](files)
        result['baseline'] = dict(kind='workflow-artifact', repository=FIXTURE, runId=str(RUN), headSha=SOURCE,
                                 callerSha=SOURCE, artifactId='300' if phase == 'initial' else '301',
                                 artifactDigest='sha256:' + ('c' if phase == 'initial' else 'd') * 64)
        files.append(dict(path='.nojekyll', size=0, sha256=hashlib.sha256(b'').hexdigest(), origin='selected', generatedBy='pages'))
    result['digest'] = S['content_digest'](files)
    return result


class ReleaseSetEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.h = R['ActionsDistributionTests'](); self.h.setUp(); self.module = R['MODULE']
        self.raw, self.deployments, self.statuses = {}, {}, {}
        self.artifacts = self.h.h.artifacts[RUN]
        self.raw[4] = self.h.api_bytes(f'repos/{FIXTURE}/actions/artifacts/4/zip')
        self.raw[2] = self.h.api_bytes(f'repos/{FIXTURE}/actions/artifacts/2/zip')
        self.jobs = []
        for i, runner in enumerate(('ubuntu-24.04', 'macos-15', 'windows-2025')):
            self.jobs.append(self.job(600 + i, f'public-source ({runner})', [runner]))
        context = dict(candidate=SHA, sourceCommit=SOURCE, runId=str(RUN), attempt=str(ATTEMPT), schema=1)
        core = []
        for platform in ('Linux', 'macOS', 'Windows'):
            r = report('initial')
            core.append(dict(context, kind='release-set-core', platform=platform, report=r, rerunDigest=r['digest'],
                             nativeMarker='release-set:1.1.0', cases=sorted(S['CORE_CASES'])))
        phases = []
        for i, phase in enumerate(('initial', 'final')):
            site_id, report_id, dep_id, job_id = 300 + i, 400 + i, 500 + i, 700 + i
            self.raw[site_id] = zip_files(S['documentation'](phase))
            r = report(phase, pages=True); r['baseline']['artifactDigest'] = S['digest'](self.raw[site_id])
            raw_report = (json.dumps(r, indent=2) + '\n').encode()
            self.raw[report_id] = zip_files({'report.json': raw_report})
            self.artifacts.extend([dict(id=site_id, name=f'release-set-docs-{phase}-{RUN}-{ATTEMPT}'),
                                   dict(id=report_id, name=f'releaseway-pages-{RUN}-{ATTEMPT}-00000000-0000-0000-0000-00000000000{i}-report')])
            phases.append(dict(context, kind='release-set-pages', phase=phase, report=r, reportSha=S['digest'](raw_report),
                               reportArtifactId=str(report_id), reportArtifactDigest=S['digest'](self.raw[report_id]),
                               siteArtifactId=str(site_id), siteArtifactDigest=S['digest'](self.raw[site_id]), served=True,
                               deploymentId=str(dep_id), pageUrl=S['PAGE_URL'], deliveryDigest=r['digest']))
            self.jobs.append(self.job(job_id, f'release-set-{phase} / deploy', ['ubuntu-24.04']))
            self.deployments[dep_id] = dict(id=dep_id, sha=SOURCE, environment='github-pages', task='deploy', created_at='2026-10-21T00:10:00Z')
            self.statuses[dep_id] = [dict(id=800 + i, state='success', environment_url=S['PAGE_URL'],
                                        log_url=f'https://github.com/{FIXTURE}/actions/runs/{RUN}/job/{job_id}', created_at='2026-10-21T00:11:00Z')]
        self.receipt = dict(context, kind='release-set', prefix=PREFIX, cases=sorted(S['CASES']), core=core, pages=phases)
        self.raw[6] = zip_files({'acceptance.json': json.dumps(self.receipt)})
        self.artifacts.append(dict(id=6, name=f'release-set-evidence-{RUN}-{ATTEMPT}'))
        self.decorate()
        g = self.module['verify'].__globals__; g['api_json'] = self.get; g['api_bytes'] = self.bytes
        self.collection = dict(mode='pre-publication', collected_at='2026-10-21T00:20:00Z',
                               workflow=dict(repository=REPO, run_id=10, run_attempt=1, head_sha=SHA, status='in_progress', conclusion=None))

    def job(self, number, name, labels):
        return dict(id=number, name=name, labels=labels, status='completed', conclusion='success', runner_name='fixture', runner_group_name='',
                    started_at='2026-10-21T00:01:00Z', completed_at='2026-10-21T00:12:00Z')

    def decorate(self):
        for artifact in self.artifacts + self.h.h.artifacts[2]:
            raw = self.raw[artifact['id']]
            artifact.update(expired=False, size_in_bytes=len(raw), digest=S['digest'](raw),
                            created_at='2026-10-21T00:10:00Z', expires_at='2026-10-28T00:10:00Z',
                            workflow_run=dict(id=RUN if artifact['id'] != 2 else 2, head_sha=SOURCE))

    def bytes(self, path):
        return self.raw[int(path.split('/artifacts/')[1].split('/')[0])]

    def get(self, path):
        if '/deployments/' in path:
            number = int(path.split('/deployments/')[1].split('/')[0])
            return self.statuses[number] if '/statuses?' in path else self.deployments[number]
        if '/jobs?' in path:
            return {'jobs': self.jobs if f'/runs/{RUN}/' in path else [self.job(900, 'checks', ['ubuntu-24.04'])]}
        if path.endswith('/runs/1'):
            return self.h.h.ci
        if path.endswith('/runs/5'):
            return self.h.distribution_ci[0]
        return self.h.api_json(path)

    def check(self):
        return self.module['verify'](REPO, SHA, [2], RUN)

    def collect(self):
        return B['collect'](self.module, REPO, SHA, [2], RUN, 'v1.2.3', B['checker_identity'](REPO, SHA, 'actions-release-set-v2'),
                            self.collection, 'actions-release-set-v2')[1]

    def test_fresh_actions_requires_new_policy_legacy_replay_still_works(self):
        self.assertEqual(B['current_policy'](REPO), 'actions-release-set-v2')
        self.assertEqual(self.check()['distribution']['release_set']['artifact_id'], 6)
        self.artifacts.pop()
        with self.assertRaisesRegex(ValueError, 'release-set'):
            self.check()
        self.assertEqual(self.module['verify'](REPO, SHA, [2], RUN, policy_id='release-v1')['distribution']['artifact_id'], 4)
        self.assertNotIn('verify-release-set-receipts.py', B['checker_identity'](REPO, SHA)['files'])

    def test_provider_mismatches_block_approval(self):
        for mutate in [lambda: self.artifacts[-1].update(expired=True),
                       lambda: self.artifacts[-1].update(name='release-set-evidence-4-1'),
                       lambda: self.artifacts[1]['workflow_run'].update(id=3),
                       lambda: self.deployments[500].update(sha='c' * 40),
                       lambda: self.statuses[500][0].update(log_url='https://github.com/wrong'),
                       lambda: self.statuses[500][0].update(state='failure'),
                       lambda: self.jobs[0].update(labels=['macos-15']),
                       lambda: self.raw.update({400: self.raw[400] + b'changed'})]:
            self.setUp(); mutate()
            with self.subTest(mutate=mutate), self.assertRaises(ValueError):
                self.check()

    def test_original_reports_and_exact_zip_members_are_required(self):
        self.raw[400] = zip_files({'report.json': '{}'})
        self.decorate()
        with self.assertRaisesRegex(ValueError, 'metadata'):
            self.check()
        for files in [{'report.json': '{}', 'extra': ''}, {'../report.json': '{}'}]:
            with self.assertRaises(ValueError):
                S['read_archive'](zip_files(files), {'report.json'})

    def test_consistent_forged_provenance_cannot_replace_original_reports(self):
        # Content digests deliberately omit provenance. Alter every matching
        # asset/file reference coherently so semantic checks alone cannot bind it.
        for r in self.receipt['core'] + self.receipt['pages']:
            source = r['report']['source']
            release = next(x for x in source['releases'] if x['tag'] == PREFIX + '1.0.0')
            asset = next(x for x in release['assets'] if x['name'] == 'bootstrap.sh')
            original = asset['id']; asset['id'] = 123456
            for f in r['report']['files']:
                if f.get('releaseId') == release['releaseId'] and f.get('assetId') == original:
                    f['assetId'] = asset['id']
        S['verify_receipt'](SHA, SOURCE, RUN, ATTEMPT, self.receipt)
        self.raw[6] = zip_files({'acceptance.json': json.dumps(self.receipt)}); self.decorate()
        with self.assertRaisesRegex(ValueError, 'original report'):
            self.check()

    def test_old_policy_cannot_approve_a_fresh_draft(self):
        legacy = B['collect'](self.module, REPO, SHA, [2], RUN, 'v1.2.3',
                              B['checker_identity'](REPO, SHA), self.collection, 'release-v1')[1]
        prepare = runpy.run_path(str(ROOT / 'scripts/prepare-release-evidence.py'))
        self.assertEqual(prepare['current_policy'](REPO), 'actions-release-set-v2')
        with self.assertRaisesRegex(ValueError, 'frozen policy differs'):
            prepare['bind_inputs'](legacy, REPO, 'v1.2.3', SHA, [2], RUN, 'actions-release-set-v2')

    def test_complete_original_bundle_replays_with_empty_path(self):
        bundle = self.collect()
        self.assertEqual(len(bundle['archives']), 7)
        self.assertEqual(B['verify_bundle'](bundle, REPO, 'v1.2.3', SHA)['state'], 'consistency_verified')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'bundle.json'; path.write_bytes(B['canonical'](bundle))
            r = subprocess.run([sys.executable, str(ROOT / 'scripts/verify-evidence-bundle.py'), '--file', str(path),
                                '--repository', REPO, '--tag', 'v1.2.3', '--candidate', SHA], env={'PATH': ''}, capture_output=True)
            self.assertEqual(r.returncode, 0, r.stderr.decode())
        bad = copy.deepcopy(bundle)
        next(v for v in bad['snapshots'].values() if isinstance(v, list))[0]['log_url'] = 'wrong'
        with self.assertRaises(ValueError):
            B['verify_bundle'](bad, REPO, 'v1.2.3', SHA)


if __name__ == '__main__':
    unittest.main()
