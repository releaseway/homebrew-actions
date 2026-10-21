import base64
import copy
import io
import json
import hashlib
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
        self.ci.update(run_attempt=1, workflow_id=90, created_at='2026-10-21T00:00:00Z',
                       updated_at='2026-10-21T00:10:00Z', run_started_at='2026-10-21T00:00:01Z')
        self.runs = {}
        self.evidence = {}
        self.artifacts = {}
        for number, scenario, workflow, state, suffix in [(2, 'npm-stage', 'publish.yml', 'staged', 'fixture'), (3, 'npm-direct', 'direct.yml', 'published', 'native')]:
            self.runs[number] = dict(id=number, repository={'full_name': self.fixture}, head_sha=FIXTURE_SHA, path='.github/workflows/' + workflow, status='completed', conclusion='success', event='workflow_dispatch', run_attempt=2)
            self.runs[number].update(workflow_id=90 + number, created_at='2026-10-21T00:00:00Z',
                                     updated_at='2026-10-21T00:10:00Z', run_started_at='2026-10-21T00:00:01Z')
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
            if repository == 'releaseway/actions':
                with self.assertRaises(ValueError): MODULE['verify'](repository, SHA, [2])
            else:
                with self.assertRaisesRegex(ValueError, 'one run per'):
                    MODULE['verify'](repository, SHA, [2])
                self.assertEqual(MODULE['verify'](repository, SHA, [2], policy_id='release-v1')['candidate_repository'], repository)



# Full distribution receipts use the fixture's existing inventory/provenance contract.
RUN = '4'
ATTEMPT = '2'
def report(kind='github-release', artifact=False):
    names=['.nojekyll','.well-known/fixture.json'] if artifact else ['bootstrap.sh','diagnose.ps1','bootstrap.mjs']
    files=[dict(path=n,size=2,sha256=hashlib.sha256(b'ok').hexdigest(),origin='selected',**({'from':n} if not artifact else {})) for n in names]
    entries=sorted([[f['path'],f['size'],f['sha256']]for f in files],key=lambda e:e[0].encode())
    digest='sha256:'+hashlib.sha256(json.dumps(entries,separators=(',',':')).encode()).hexdigest()
    src=dict(kind='github-release',repository='releaseway/release-fixture',commit=FIXTURE_SHA,releaseId=4,tag=f'distribution-{RUN}-{ATTEMPT}',assets=[dict(id=i+1,name=f['path'],size=2,digest='sha256:'+f['sha256'])for i,f in enumerate(files)])
    if kind=='workflow-artifact':src=dict(kind=kind,artifactId='9',runId=RUN,callerSha=FIXTURE_SHA,artifactDigest='sha256:'+'c'*64)
    return dict(schema=1,files=files,digest=digest,source=src,latestId='4')
def receipts():
    result=[dict(schema=1,kind='core',candidate=SHA,sourceCommit=FIXTURE_SHA,runId=RUN,attempt=ATTEMPT,platform=p,cases=['public-release','cross-repo-read','negative-tamper','rerun','native-script'],report=report())for p in ['Linux','macOS','Windows']]
    for i,mode in enumerate(['latest','release','artifact']):
        r=report('workflow-artifact' if mode=='artifact' else 'github-release',artifact=True)
        result.append(dict(schema=1,kind='pages',mode=mode,candidate=SHA,sourceCommit=FIXTURE_SHA,runId=RUN,attempt=ATTEMPT,report=r,served=True,deploymentId=str(100+i),reportArtifactId=str(200+i),pageUrl='https://releaseway.github.io/release-fixture/',deliveryDigest=r['digest'],sourceArtifactId='9'if mode=='artifact' else '',negative404=(mode=='artifact'),negativeDigest=(mode=='artifact')))
    return result

class ActionsDistributionTests(unittest.TestCase):
    def setUp(self):
        self.h = ReadinessTests()
        self.h.setUp()
        self.h.ci.update(repository={'full_name': 'releaseway/actions'}, path='.github/workflows/test.yml')
        self.h.runs[2].update(repository={'full_name': 'releaseway/release-fixture'}, path='.github/workflows/release-notes-acceptance.yml')
        self.h.evidence[2].update(candidate_repository='releaseway/actions', fixture_repository='releaseway/release-fixture', scenario='actions-all', workflow='release-notes-acceptance.yml', state='verified')
        self.h.runs[4] = dict(self.h.runs[2], id=4, path='.github/workflows/distribution-acceptance.yml')
        self.h.artifacts[4] = [dict(id=4, name='distribution-evidence-4-2', expired=False, size_in_bytes=1000)]
        self.distribution_ci = [dict(self.h.ci, id=5, path='.github/workflows/distribution.yml')]
        self.receipts = receipts()
        globals_ = MODULE['verify'].__globals__
        globals_['api_json'] = self.api_json
        globals_['api_bytes'] = self.api_bytes

    def api_json(self, path):
        if '/workflows/distribution.yml/' in path:
            return {'workflow_runs': self.distribution_ci}
        return self.h.api_json(path)

    def api_bytes(self, path):
        if '/artifacts/4/' not in path:
            return self.h.api_bytes(path)
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w') as bundle:
            for i, receipt in enumerate(self.receipts):
                bundle.writestr(f'{i}/receipt.json', json.dumps(receipt))
        return buffer.getvalue()

    def check(self, run_id=4):
        # Historical policy remains readable for already archived approvals.
        return MODULE['verify']('releaseway/actions', SHA, [2], run_id, policy_id='release-v1')

    def test_complete_set_and_notes_only(self):
        self.assertEqual(self.check()['distribution']['run_id'], 4)
        with self.assertRaises(ValueError): self.check(None)

    def test_missing_duplicate_public_only_or_wrong_receipts(self):
        good = copy.deepcopy(self.receipts)
        for bad in [good[:3], good[:-1], good + [good[0]], [good[0]] + good[:-1]]:
            self.receipts = bad
            with self.assertRaises(ValueError): self.check()
        for field, value in [('candidate','d'*40),('sourceCommit','d'*40),('runId','19'),('attempt','1'),('served',False),('deliveryDigest','sha256:'+'0'*64),('mode','unknown')]:
            self.receipts = copy.deepcopy(good)
            self.receipts[-1][field] = value
            with self.assertRaises(ValueError): self.check()
        self.receipts = copy.deepcopy(good)
        self.receipts[-1]['report']['files'][0]['sha256'] = '0'*64
        with self.assertRaises(ValueError): self.check()

    def test_distribution_ci_and_run_identity(self):
        ci = copy.deepcopy(self.distribution_ci)
        for bad in [[], [dict(ci[0], conclusion='failure')], ci+[dict(ci[0],id=6,status='in_progress')]]:
            self.distribution_ci=bad
            with self.assertRaises(ValueError): self.check()
        self.distribution_ci=ci
        for key,value in [('conclusion','failure'),('event','push'),('path','.github/workflows/other.yml'),('repository',{'full_name':'other/repo'})]:
            original=self.h.runs[4][key];self.h.runs[4][key]=value
            with self.assertRaises(ValueError): self.check()
            self.h.runs[4][key]=original

    def test_expired_stale_or_ambiguous_distribution_artifact(self):
        good=copy.deepcopy(self.h.artifacts[4])
        for artifacts in [[],[dict(good[0],expired=True)],[dict(good[0],name='distribution-evidence-4-1')],good*2]:
            self.h.artifacts[4]=artifacts
            with self.assertRaises(ValueError): self.check()

if __name__ == '__main__':
    unittest.main()
