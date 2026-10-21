"""Independent approval fixtures exercise policy selection, native provenance and offline replay."""
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
SHA, SOURCE, SPEC = 'a' * 40, 'b' * 40, 'c' * 40
REPO, FIXTURE = 'releaseway/homebrew-actions', 'releaseway/release-fixture'
TARGETS = ['macos-arm64', 'macos-x86_64', 'linux-arm64', 'linux-x86_64']
RUNNERS = ['macos-latest', 'macos-15-intel', 'ubuntu-24.04-arm', 'ubuntu-24.04']


def zip_json(name, value):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(name, json.dumps(value))
    return stream.getvalue()


class FeatureTests(unittest.TestCase):
    def setUp(self):
        self.module = runpy.run_path(str(ROOT / 'scripts/verify-release-evidence.py'))
        self.runs = {}
        for number, repository, commit, workflow, event in [
            (1, REPO, SHA, 'test.yml', 'push'), (2, FIXTURE, SOURCE, 'homebrew-acceptance.yml', 'workflow_dispatch'),
            (3, FIXTURE, SOURCE, 'homebrew-features-acceptance.yml', 'workflow_dispatch')]:
            self.runs[number] = dict(id=number, run_attempt=1, workflow_id=90 + number, repository={'full_name': repository},
                head_sha=commit, path='.github/workflows/' + workflow, event=event, status='completed', conclusion='success',
                created_at='2026-10-21T00:00:00Z', updated_at='2026-10-21T01:00:00Z', run_started_at='2026-10-21T00:00:01Z')
        self.e = dict(schema=2, candidate_repository=REPO, candidate_sha=SHA, fixture_repository=FIXTURE,
            fixture_commit=SOURCE, run_id=3, run_attempt=1, workflow='homebrew-features-acceptance.yml',
            scenario='homebrew-revision-platforms', state='verified', version='1.3.1', rerun_state='unchanged',
            tap=dict(zero='d' * 40, one='e' * 40, final='f' * 40), invocations=[], consumers=[], negatives=[], cases=[])
        self.e['dataset'] = dict(schema=1, source_commit=SOURCE, spec_commit=SPEC, spec_branch='homebrew-feature-spec-3-1',
            version='1.3.1', release_tag='homebrew-features-1.3.1', spec_path='.github/homebrew/features/source.yml',
            source_spec_sha256='0' * 64, packaging_spec_sha256='1' * 64)
        self.e['build'] = dict(schema=1, version='1.3.1', toolchain='go version go1.26.7 linux/amd64',
            source_files={'go.mod': '2' * 64, 'main.go': '3' * 64}, platforms=[])
        self.e['fixture_release'] = dict(id=100, tag='homebrew-features-1.3.1', commit=SOURCE, assets=[])
        for n, platform in enumerate(TARGETS):
            asset = 'releaseway-native-fixture_' + platform.replace('-', '_') + '.tar.gz'
            goos, goarch = ('darwin' if platform.startswith('macos') else 'linux'), ('arm64' if platform.endswith('arm64') else 'amd64')
            self.e['build']['platforms'].append(dict(platform=platform, goos=goos, goarch=goarch, asset=asset, binary_sha256='4' * 64, sha256='5' * 64, size=100))
            self.e['fixture_release']['assets'].append(dict(id=110 + n, name=asset, size=100, digest='sha256:' + '5' * 64))
        self.raw, self.artifacts = {}, {2: [], 3: []}
        definitions = [('source-r0', 'source', TARGETS, 0), ('source-r1', 'source', TARGETS, 1),
            ('source-rerun', 'source', TARGETS, 1), ('prebuilt-linux', 'linux', ['linux-x86_64'], 0),
            ('prebuilt-macos', 'macos', ['macos-arm64'], 0), ('prebuilt-mixed', 'mixed', ['macos-arm64', 'linux-x86_64'], 0)]
        for n, (role, spec, targets, revision) in enumerate(definitions):
            name = 'releaseway-source-platform-fixture' if spec == 'source' else 'releaseway-prebuilt-' + spec + '-fixture'
            context = dict(formula=name, formula_sha256=('7' if revision else '6') * 64, source_commit=SOURCE,
                spec_commit=SPEC if revision else SOURCE, spec_path='.github/homebrew/features/' + spec + '.yml',
                spec_sha256=('1' if revision else '0') * 64, version='1.3.1', version_scheme=0, revision=revision,
                declaration_mode='explicit', declared_platforms=targets, required_platforms=targets,
                runners={p: RUNNERS[TARGETS.index(p)] for p in targets})
            scope = dict(invocation_id=f'00000000-0000-4000-8000-{n:012d}', automation_repository=REPO,
                automation_sha=SHA, caller_repository=FIXTURE, caller_sha=SOURCE, run_id=3, run_attempt=1)
            receipts = []
            for platform in targets:
                arch = platform.split('-')[1]
                runtime = dict(os='Darwin' if platform.startswith('macos') else 'Linux', uname_arch=arch, hardware_arch=arch,
                    brew_host_cpu=arch, runner_arch=arch, runner_os='macOS' if platform.startswith('macos') else 'Linux',
                    ruby_platform=arch, brew_version='Homebrew 7.0.0', image_os='fixture', image_version='1')
                receipts.append(dict(schema=1, kind='homebrew-native', **context, **scope, platform=platform,
                    requested_runner=context['runners'][platform], runtime=runtime, checks=dict(audit='success', install='success', test='success'),
                    completed_at='2026-10-21T00:30:00Z'))
            validation = dict(schema=1, kind='homebrew-validation', **context, **scope, validated_platforms=targets, native_receipts=receipts)
            metadata = self.metadata(10 + n, 'homebrew-validation-' + scope['invocation_id'], b'placeholder')
            self.artifacts[3].append(metadata)
            self.e['invocations'].append(dict(role=role, artifact={}, validation=validation))
            self.refresh(n)
        unsupported = [('releaseway-prebuilt-linux-fixture', 'Linux'), ('releaseway-prebuilt-macos-fixture', 'arm64'),
                       ('releaseway-prebuilt-linux-fixture', 'x86_64'), ('releaseway-prebuilt-macos-fixture', 'macOS')]
        for n, platform in enumerate(TARGETS):
            goos, goarch = ('darwin' if platform.startswith('macos') else 'linux'), ('arm64' if platform.endswith('arm64') else 'amd64')
            self.e['consumers'].append(dict(schema=1, kind='homebrew-feature-consumer', platform=platform, candidate_sha=SHA,
                source_commit=SOURCE, spec_commit=SPEC, fixture_commit=SOURCE, run_id=3, run_attempt=1, version='1.3.1',
                tap_zero='d' * 40, tap_one='e' * 40, tap_final='f' * 40, zero_formula_sha256='6' * 64, one_formula_sha256='7' * 64,
                initial_package='1.3.1', updated_package='1.3.1_1', native_output=f'releaseway-native-fixture 1.3.1 {goos}/{goarch} go1.26.7',
                unsupported_rejected=True, unsupported_formula=unsupported[n][0], unsupported_requirement=unsupported[n][1]))
            for prefix in ['source-install-', 'revision-upgrade-', 'unsupported-']:
                c = dict(id=prefix + platform, state='verified', platform=platform)
                if prefix == 'source-install-': c['invocation'] = 'source-r1'
                self.e['cases'].append(c)
        reasons = [('revision-decrease', 'Refusing Formula revision downgrade'),
            ('source-replacement', 'Same-version Formula updates require the same source commit'),
            ('missing-asset', 'missing asset deliberately-missing-native-fixture.tar.gz'),
            ('missing-receipt', 'native receipt set incomplete'), ('native-arch-mismatch', 'Native hardware architecture mismatch')]
        for n, (case, reason) in enumerate(reasons):
            negative = dict(case=case, state='rejected', before='f' * 40, after='f' * 40, reason=reason)
            if n < 3: negative.update(run_id=200 + n, run_attempt=1, failed_job_id=300 + n, fixture_commit=SOURCE, workflow='homebrew-features-negative.yml', conclusion='failure')
            if n == 4: negative.update(actual_os='Linux', actual_arch='x86_64', requested_platform='linux-arm64')
            self.e['negatives'].append(negative)
            self.e['cases'].append(dict(id=case + '-rejected', state='verified'))
        self.e['cases'] += [dict(id='revision-rerun', state='verified', invocation='source-rerun')]
        self.e['cases'] += [dict(id=role, state='verified', invocation=role) for role in ['prebuilt-linux', 'prebuilt-macos', 'prebuilt-mixed']]
        self.legacy = dict(schema=1, candidate_repository=REPO, candidate_sha=SHA, fixture_repository=FIXTURE, fixture_commit=SOURCE,
            run_id=2, run_attempt=1, workflow='homebrew-acceptance.yml', scenario='homebrew-public', state='verified')
        globals_ = self.module['verify'].__globals__
        globals_['api_json'], globals_['api_bytes'] = self.api_json, self.api_bytes

    def metadata(self, number, name, raw):
        return dict(id=number, name=name, digest='sha256:' + hashlib.sha256(raw).hexdigest(), size_in_bytes=len(raw), expired=False,
                    created_at='2026-10-21T00:30:00Z', expires_at='2026-11-20T00:30:00Z')

    def refresh(self, n):
        invocation = self.e['invocations'][n]
        raw = zip_json('homebrew-validation.json', invocation['validation'])
        metadata = self.metadata(10 + n, 'homebrew-validation-' + invocation['validation']['invocation_id'], raw)
        self.artifacts[3][n].update(metadata)
        invocation.update(artifact={k: metadata[k] for k in ['id', 'name', 'size_in_bytes', 'digest', 'created_at', 'expires_at']},
                          zip_sha256=hashlib.sha256(raw).hexdigest(), zip_base64=base64.b64encode(raw).decode())
        self.raw[10 + n] = raw

    def api_json(self, path):
        if '/workflows/' in path: return {'workflow_runs': [self.runs[1]]}
        number = int(path.split('/actions/runs/')[1].split('/')[0].split('?')[0])
        if '/jobs?' in path: return {'jobs': [dict(id=400 + number, name='approval', status='completed', conclusion='success',
            started_at='2026-10-21T00:00:01Z', completed_at='2026-10-21T01:00:00Z', runner_name='fixture', runner_group_name='', labels=['ubuntu-24.04'])]}
        if '/artifacts?' in path:
            raw = self.api_bytes(f'artifacts/{number}/zip')
            return {'artifacts': [self.metadata(number, 'releaseway-acceptance-1', raw), *self.artifacts[number]]}
        return copy.deepcopy(self.runs[number])

    def api_bytes(self, path):
        number = int(path.split('artifacts/')[1].split('/')[0])
        return zip_json('acceptance.json', self.legacy if number == 2 else self.e) if number in [2, 3] else self.raw[number]

    def check(self, ids=None, **kwargs):
        return self.module['verify'](REPO, SHA, [2, 3] if ids is None else ids, **kwargs)

    def test_complete_default_policy_and_legacy_archive_selection(self):
        self.assertEqual(len(self.check()['acceptance']), 2)
        for ids in [[2], [3], [2, 2]]:
            with self.subTest(ids=ids), self.assertRaises(ValueError): self.check(ids)
        self.assertEqual(len(self.check([2], policy_id='release-v1')['acceptance']), 1)
        self.assertEqual(B['current_policy'](REPO), 'homebrew-platforms-v2')

    def test_domain_mutations_reject_partial_or_substituted_proofs(self):
        good = copy.deepcopy(self.e)
        mutations = [lambda e: e['cases'].pop(), lambda e: e['consumers'].pop(),
            lambda e: e['consumers'][0].update(updated_package='1.3.1'), lambda e: e['consumers'][0].update(unsupported_rejected=False),
            lambda e: e['consumers'][0].update(one_formula_sha256='6' * 64), lambda e: e['negatives'][0].update(after='d' * 40),
            lambda e: e['negatives'][0].update(reason='unrelated failure'), lambda e: e['build']['platforms'][0].update(goarch='amd64'),
            lambda e: e['dataset'].update(spec_commit=SOURCE), lambda e: e.update(run_attempt=2),
            lambda e: e['invocations'][0].update(role='source-r1'), lambda e: e.update(rerun_state='published')]
        for mutation in mutations:
            self.e = copy.deepcopy(good); mutation(self.e)
            with self.subTest(mutation=mutation), self.assertRaises(ValueError): self.check()

    def test_native_context_and_runtime_substitution_rejected_even_with_valid_provider_zip(self):
        for key, value in [('source_commit', SPEC), ('spec_commit', SOURCE), ('revision', 2), ('run_attempt', 2), ('formula_sha256', '6' * 64)]:
            self.setUp()
            v = self.e['invocations'][1]['validation']; v[key] = value
            for receipt in v['native_receipts']: receipt[key] = value
            self.refresh(1)
            with self.subTest(key=key), self.assertRaises(ValueError): self.check()
        self.setUp()
        self.e['invocations'][1]['validation']['native_receipts'][0]['runtime']['hardware_arch'] = 'x86_64'
        self.refresh(1)
        with self.assertRaisesRegex(ValueError, 'architecture'): self.check()
        self.setUp()
        self.e['invocations'][1]['validation']['native_receipts'].pop(); self.refresh(1)
        with self.assertRaisesRegex(ValueError, 'incomplete'): self.check()
        self.setUp()
        v = self.e['invocations'][1]['validation']
        v['native_receipts'] = [r for r in v['native_receipts'] if r['platform'] == 'linux-x86_64']
        changed = dict(required_platforms=['linux-x86_64'], declared_platforms=['linux-x86_64'], runners={'linux-x86_64': 'ubuntu-24.04'})
        v.update(changed, validated_platforms=['linux-x86_64'])
        for receipt in v['native_receipts']: receipt.update(changed)
        self.refresh(1)
        with self.assertRaisesRegex(ValueError, 'platform'): self.check()

    def test_original_artifact_binding_and_missing_current_attempt(self):
        self.raw[10] += b'changed provider bytes'
        with self.assertRaisesRegex(ValueError, 'provider bytes'): self.check()
        self.setUp(); self.artifacts[3].pop(0)
        with self.assertRaisesRegex(ValueError, 'missing'): self.check()
        self.setUp(); self.runs[3]['run_attempt'] = 2
        with self.assertRaises(ValueError): self.check()

    def test_future_bundle_preserves_eight_zips_and_replays_with_empty_path(self):
        collection = dict(mode='pre-publication', collected_at='2026-10-21T02:00:00Z',
                          workflow=dict(repository=REPO, run_id=500, run_attempt=1, head_sha=SHA, status='in_progress', conclusion=None))
        _, bundle = B['collect'](self.module, REPO, SHA, [2, 3], None, 'v1.2.3',
            B['checker_identity'](REPO, SHA, 'homebrew-platforms-v2'), collection, 'homebrew-platforms-v2')
        self.assertEqual(len(bundle['archives']), 8)
        self.assertEqual(len(bundle['checker']['files']), 7)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'bundle.json'; B['atomic_write'](path, B['canonical'](bundle))
            result = subprocess.run([sys.executable, str(ROOT / 'scripts/verify-evidence-bundle.py'), '--file', str(path),
                '--repository', REPO, '--tag', 'v1.2.3', '--candidate', SHA], env={'PATH': ''}, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr.decode())
        bad = copy.deepcopy(bundle); del bad['checker']['files']['verify-homebrew-native.py']
        with self.assertRaisesRegex(ValueError, 'inventory'): B['verify_bundle'](bad, REPO, 'v1.2.3', SHA)
        _, old = B['collect'](self.module, REPO, SHA, [2], None, 'v1.2.3', B['checker_identity'](REPO, SHA), collection, 'release-v1')
        self.assertEqual(len(old['checker']['files']), 5)
        self.assertEqual(B['verify_bundle'](old, REPO, 'v1.2.3', SHA)['state'], 'consistency_verified')


if __name__ == '__main__':
    unittest.main()
