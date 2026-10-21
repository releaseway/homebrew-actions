"""Validate the revision/platform contract against provider-bound original native ZIPs."""
import base64
from pathlib import Path
import re
import runpy

B = runpy.run_path(str(Path(__file__).with_name('evidence-bundle.py')))
aggregate = runpy.run_path(str(Path(__file__).with_name('verify-homebrew-native.py')))['aggregate']
require = B['require']
TARGETS = ['macos-arm64', 'macos-x86_64', 'linux-arm64', 'linux-x86_64']
RUNNERS = dict(zip(TARGETS, ['macos-latest', 'macos-15-intel', 'ubuntu-24.04-arm', 'ubuntu-24.04']))
CONTEXT = ['formula', 'formula_sha256', 'source_commit', 'spec_commit', 'spec_path', 'spec_sha256', 'version',
           'version_scheme', 'revision', 'declaration_mode', 'declared_platforms', 'required_platforms', 'runners']
ROLES = {
    'source-r0': ('source', TARGETS, 0),
    'source-r1': ('source', TARGETS, 1),
    'source-rerun': ('source', TARGETS, 1),
    'prebuilt-linux': ('linux', ['linux-x86_64'], 0),
    'prebuilt-macos': ('macos', ['macos-arm64'], 0),
    'prebuilt-mixed': ('mixed', ['macos-arm64', 'linux-x86_64'], 0),
}
NAMES = {'source': 'releaseway-source-platform-fixture', 'linux': 'releaseway-prebuilt-linux-fixture',
         'macos': 'releaseway-prebuilt-macos-fixture', 'mixed': 'releaseway-prebuilt-mixed-fixture'}
REJECTIONS = {
    'revision-decrease': 'Refusing Formula revision downgrade',
    'source-replacement': 'Same-version Formula updates require the same source commit',
    'missing-asset': 'missing asset deliberately-missing-native-fixture.tar.gz',
    'missing-receipt': 'native receipt set incomplete',
    'native-arch-mismatch': 'Native hardware architecture mismatch',
}


def sha(value, length=40):
    require(isinstance(value, str) and re.fullmatch('[a-f0-9]{' + str(length) + '}', value), 'invalid feature content identity')
    return value


def positive(value):
    require(type(value) is int and value > 0, 'invalid feature provider ID')
    return value


def verify_features(e, candidate, run, artifacts, api_bytes):
    policy = B['policy_data']('homebrew-platforms-v2')
    fixture, source, run_id, attempt = 'releaseway/release-fixture', sha(run['head_sha']), positive(run['id']), positive(run['run_attempt'])
    require(type(e['schema']) is int and e['schema'] == 2 and e['scenario'] == 'homebrew-revision-platforms'
            and e['workflow'] == 'homebrew-features-acceptance.yml' and e['state'] == 'verified', 'invalid feature approval schema')
    require(e['candidate_repository'] == 'releaseway/homebrew-actions' and e['candidate_sha'] == candidate
            and e['fixture_repository'] == fixture and e['fixture_commit'] == source
            and type(e['run_id']) is int and e['run_id'] == run_id
            and type(e['run_attempt']) is int and e['run_attempt'] == attempt, 'feature approval identity differs')
    version = f'1.{run_id}.{attempt}'
    dataset = e['dataset']
    require(type(dataset['schema']) is int and dataset['schema'] == 1 and dataset['source_commit'] == source
            and sha(dataset['spec_commit']) != source and dataset['version'] == e['version'] == version
            and dataset['release_tag'] == 'homebrew-features-' + version
            and dataset['spec_branch'] == f'homebrew-feature-spec-{run_id}-{attempt}'
            and dataset['spec_path'] == '.github/homebrew/features/source.yml', 'feature source/spec dataset differs')
    packaging = dataset['spec_commit']
    sha(dataset['source_spec_sha256'], 64)
    sha(dataset['packaging_spec_sha256'], 64)
    require(dataset['source_spec_sha256'] != dataset['packaging_spec_sha256'], 'packaging spec must change revision')
    require(set(e['tap']) == {'zero', 'one', 'final'}, 'invalid feature tap snapshots')
    for commit in e['tap'].values():
        sha(commit)
    require(e['tap']['zero'] != e['tap']['one'] and e['rerun_state'] == 'unchanged', 'revision publication/rerun state differs')

    build, release = e['build'], e['fixture_release']
    require(type(build['schema']) is int and build['schema'] == 1 and build['version'] == version
            and re.match(r'go version go1\.26\.\S+ ', build['toolchain']), 'compiled fixture toolchain/version differs')
    require(set(build['source_files']) == {'go.mod', 'main.go'}, 'compiled fixture source inventory differs')
    for digest in build['source_files'].values():
        sha(digest, 64)
    positive(release['id'])
    require(release['tag'] == dataset['release_tag'] and release['commit'] == source, 'binary release source differs')
    require([entry['platform'] for entry in build['platforms']] == TARGETS and len(release['assets']) == 4,
            'compiled native target/asset inventory differs')
    assets = {asset['name']: asset for asset in release['assets']}
    require(len(assets) == 4 and len({positive(asset['id']) for asset in assets.values()}) == 4, 'binary release asset identities collide')
    for entry in build['platforms']:
        platform = entry['platform']
        goos, goarch = ('darwin' if platform.startswith('macos') else 'linux'), ('arm64' if platform.endswith('arm64') else 'amd64')
        name = 'releaseway-native-fixture_' + platform.replace('-', '_') + '.tar.gz'
        require(entry['goos'] == goos and entry['goarch'] == goarch and entry['asset'] == name, 'compiled binary ABI/asset differs')
        sha(entry['binary_sha256'], 64)
        sha(entry['sha256'], 64)
        asset = assets[name]
        require(positive(entry['size']) == asset['size'] and type(asset['size']) is int
                and asset['digest'] == 'sha256:' + entry['sha256'], 'compiled fixture release digest/size differs')

    require(len(e['invocations']) == policy['native_invocations'], 'native invocation inventory incomplete')
    by_role, ids, uuids = {}, set(), set()
    for invocation in e['invocations']:
        role = invocation['role']
        require(role in ROLES and role not in by_role, 'missing/duplicate feature invocation role')
        spec, targets, revision = ROLES[role]
        embedded = invocation['artifact']
        artifact_id = positive(embedded['id'])
        require(artifact_id not in ids, 'native artifact IDs collided')
        ids.add(artifact_id)
        matches = [a for a in artifacts if a['id'] == artifact_id]
        require(len(matches) == 1, 'original native artifact missing or ambiguous')
        metadata = matches[0]
        require(metadata['expired'] is False and type(metadata['size_in_bytes']) is int
                and 0 < metadata['size_in_bytes'] <= B['MAX_ZIP'], 'original native artifact expired or too large')
        require(embedded == {key: metadata[key] for key in ['id', 'name', 'size_in_bytes', 'digest', 'created_at', 'expires_at']},
                'embedded native provider metadata differs')
        raw = base64.b64decode(invocation['zip_base64'], validate=True)
        require(0 < len(raw) <= B['MAX_ZIP'] and B['sha256'](raw) == invocation['zip_sha256']
                and metadata['digest'] == 'sha256:' + B['sha256'](raw), 'native ZIP digest differs')
        # Never approve a self-consistent embedded replacement for another provider artifact.
        require(api_bytes(f'repos/{fixture}/actions/artifacts/{artifact_id}/zip') == raw, 'embedded native ZIP differs from original provider bytes')
        validation = B['receipts'](raw, 'native')[0]
        require(validation == invocation['validation'], 'parsed native ZIP differs from embedded validation')
        context = {key: validation[key] for key in CONTEXT}
        scope = dict(invocation_id=validation['invocation_id'], automation_repository='releaseway/homebrew-actions',
                     automation_sha=candidate, caller_repository=fixture, caller_sha=source, run_id=run_id, run_attempt=attempt)
        require(aggregate(context, scope, validation['native_receipts']) == validation, 'native receipt aggregation differs')
        require(scope['invocation_id'] not in uuids and metadata['name'] == 'homebrew-validation-' + scope['invocation_id'],
                'native invocation UUID/name collided or differs')
        uuids.add(scope['invocation_id'])
        require(context['formula'] == NAMES[spec] and context['source_commit'] == source
                and context['spec_commit'] == (packaging if revision else source)
                and context['spec_path'] == f'.github/homebrew/features/{spec}.yml'
                and context['version'] == version and context['version_scheme'] == 0 and context['revision'] == revision
                and context['declaration_mode'] == 'explicit'
                and context['required_platforms'] == context['declared_platforms'] == targets
                and context['runners'] == {target: RUNNERS[target] for target in targets}, 'feature invocation source/revision/platform differs')
        if spec == 'source':
            require(context['spec_sha256'] == dataset['packaging_spec_sha256' if revision else 'source_spec_sha256'], 'source packaging spec digest differs')
        by_role[role] = validation
    require(set(by_role) == set(ROLES) and sum(len(v['native_receipts']) for v in by_role.values()) == policy['native_receipts'],
            'required native receipt inventory differs')
    zero, one, rerun = [by_role[role]['formula_sha256'] for role in ['source-r0', 'source-r1', 'source-rerun']]
    require(zero != one == rerun, 'revision/rerun Formula digests differ')

    unsupported = {'macos-arm64': ('releaseway-prebuilt-linux-fixture', 'Linux'), 'macos-x86_64': ('releaseway-prebuilt-macos-fixture', 'arm64'),
                   'linux-arm64': ('releaseway-prebuilt-linux-fixture', 'x86_64'), 'linux-x86_64': ('releaseway-prebuilt-macos-fixture', 'macOS')}
    require(len(e['consumers']) == 4, 'four same-job native upgrade consumers required')
    seen, cases = set(), {}
    for consumer in e['consumers']:
        platform = consumer['platform']
        require(platform in TARGETS and platform not in seen, 'native consumer target missing/duplicated')
        seen.add(platform)
        require(type(consumer['schema']) is int and consumer['schema'] == 1 and consumer['kind'] == 'homebrew-feature-consumer', 'invalid native consumer schema')
        expected = dict(candidate_sha=candidate, source_commit=source, spec_commit=packaging, fixture_commit=source,
                        run_id=run_id, run_attempt=attempt, version=version,
                        tap_zero=e['tap']['zero'], tap_one=e['tap']['one'], tap_final=e['tap']['final'],
                        zero_formula_sha256=zero, one_formula_sha256=one, initial_package=version, updated_package=version + '_1',
                        unsupported_rejected=True, unsupported_formula=unsupported[platform][0], unsupported_requirement=unsupported[platform][1])
        require(all(consumer[key] == value and type(consumer[key]) is type(value) for key, value in expected.items()), 'same-job upgrade/unsupported consumer proof differs')
        goos, goarch = ('darwin' if platform.startswith('macos') else 'linux'), ('arm64' if platform.endswith('arm64') else 'amd64')
        require(consumer['native_output'].startswith(f'releaseway-native-fixture {version} {goos}/{goarch} go1.'), 'consumer executable ABI differs')
        for prefix in ['source-install-', 'revision-upgrade-', 'unsupported-']:
            case = dict(id=prefix + platform, state='verified', platform=platform)
            if prefix == 'source-install-':
                case['invocation'] = 'source-r1'
            cases[case['id']] = case

    require(len(e['negatives']) == 5 and {item['case'] for item in e['negatives']} == set(REJECTIONS), 'required negative cases incomplete')
    child_ids = set()
    for item in e['negatives']:
        case = item['case']
        require(item['state'] == 'rejected' and sha(item['before']) == item['after'] and item['reason'] == REJECTIONS[case], 'negative case rejection/no-write proof differs')
        if case in ['revision-decrease', 'source-replacement', 'missing-asset']:
            child = positive(item['run_id'])
            require(child not in child_ids and child != run_id and positive(item['run_attempt']) and positive(item['failed_job_id'])
                    and item['fixture_commit'] == source and item['workflow'] == 'homebrew-features-negative.yml'
                    and item['conclusion'] == 'failure', 'negative child run identity differs')
            child_ids.add(child)
        elif case == 'native-arch-mismatch':
            require(item['actual_os'] == 'Linux' and item['actual_arch'] in ['x86_64', 'amd64']
                    and item['requested_platform'] == 'linux-arm64', 'actual native mismatch proof differs')
        cases[case + '-rejected'] = dict(id=case + '-rejected', state='verified')
    for role in ['source-rerun', 'prebuilt-linux', 'prebuilt-macos', 'prebuilt-mixed']:
        name = 'revision-rerun' if role == 'source-rerun' else role
        cases[name] = dict(id=name, state='verified', invocation=role)
    require(set(cases) == set(policy['required_cases']) and len(e['cases']) == len(cases)
            and {case['id'] for case in e['cases']} == set(cases)
            and all(case == cases[case['id']] for case in e['cases']), 'required feature case proofs incomplete or differ')
