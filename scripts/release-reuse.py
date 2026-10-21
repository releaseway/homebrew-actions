"""Versioned full/equivalent approvals. Original receipts keep their original SHA."""
import hashlib
import json
from pathlib import Path
import re
import runpy
import tempfile

ROOT = Path(__file__).resolve().parent
B = runpy.run_path(str(ROOT / 'evidence-bundle.py'))
I = runpy.run_path(str(ROOT / 'release-inputs.py'))
C = runpy.run_path(str(ROOT / 'ci-contract.py'))
POLICY = 'release-reuse-v1'


def policy():
    data = B['policy_data'](POLICY)
    return dict(id=POLICY, data=data, sha256=B['sha256'](B['canonical'](data)))


def checker(repository, commit, scenario_policy=None):
    value = B['checker_identity'](repository, commit, scenario_policy or B['current_policy'](repository))
    for name in ('release-reuse.py', 'release-inputs.py', 'ci-contract.py', 'prepare-release-evidence.py',
                 'verify-archived-release.py', 'ci-mode.py', 'ci-gate.py', 'check-docs.py', 'verify-tag-ci.py'):
        value['files'][name] = B['sha256']((ROOT / name).read_bytes())
    return value


def candidate_identity(repository, tag, candidate):
    return dict(repository=repository, tag=tag, sha=candidate)


def full(bundle, root, ci):
    identity = bundle['candidate']
    B['require'](bundle['policy']['id'] == B['current_policy'](identity['repository']), 'fresh full scenario policy differs')
    B['verify_bundle'](bundle, identity['repository'], identity['tag'], identity['sha'])
    inputs = I['snapshot'](root, identity['sha'])
    result = dict(schema=2, verification='full', candidate=identity, policy=policy(),
                  checker=checker(identity['repository'], bundle['checker']['commit']),
                  full=bundle, inputs=inputs, ci=ci)
    verify(result, identity['repository'], identity['tag'], identity['sha'])
    return result


def ci_from_full(bundle):
    repository = bundle['candidate']['repository']
    run_id = bundle['result']['ci_run_id']
    path = f'repos/{repository}/actions/runs/{run_id}'
    run = bundle['snapshots'][path]
    prefix = path + '/attempts/' + str(run['run_attempt']) + '/jobs?'
    jobs = [job for endpoint, value in bundle['snapshots'].items() if endpoint.startswith(prefix) for job in value['jobs']]
    return dict(contract=C['CONTRACT'], mode='full', run=run, jobs=jobs)


def verify(bundle, repository, tag, candidate):
    require = B['require']
    require(type(bundle['schema']) is int and bundle['schema'] == 2, 'unsupported reuse bundle schema')
    B['reject_bool_ids'](bundle)
    require(len(B['canonical'](bundle)) <= B['MAX_BUNDLE'], 'bundle exceeds limit')
    require(bundle['candidate'] == candidate_identity(repository, tag, candidate)
            and re.fullmatch('[0-9a-f]{40}', candidate or ''), 'candidate identity mismatch')
    require(bundle['policy'] == policy() and repository in policy()['data']['repositories'], 'reuse policy differs')
    recorded = bundle['checker']
    scenario = bundle['full']['policy']['id'] if bundle['verification'] == 'full' else bundle['baseline']['approval']['full']['policy']['id']
    expected_names = set(checker(repository, candidate, scenario)['files'])
    require(recorded['repository'] == repository and re.fullmatch('[0-9a-f]{40}', recorded['commit'])
            and set(recorded['files']) == expected_names
            and all(re.fullmatch('[0-9a-f]{64}', value) for value in recorded['files'].values()), 'checker identity differs')
    mode = bundle['verification']
    if mode == 'full':
        original = bundle['full']
        require(original['schema'] == 1 and original['candidate'] == bundle['candidate'], 'full approval source differs')
        require(original['policy']['id'] != 'legacy-20261020', 'legacy backfill cannot establish a new-policy full anchor')
        require(original['checker']['commit'] == recorded['commit'], 'full checker commit differs')
        B['verify_bundle'](original, repository, tag, candidate)
        run = bundle['ci']['run']
        require(run['id'] == original['result']['ci_run_id'], 'full CI source differs')
        run_path = f'repos/{repository}/actions/runs/{run["id"]}'
        require(run == original['snapshots'][run_path], 'full CI snapshot differs')
        jobs_prefix = run_path + '/attempts/' + str(run['run_attempt']) + '/jobs?'
        original_jobs = [job for path, value in original['snapshots'].items()
                         if path.startswith(jobs_prefix) for job in value['jobs']]
        require(bundle['ci']['jobs'] == original_jobs, 'full CI job snapshot differs')
        if repository == 'releaseway/actions':
            distribution_id = original['result']['distribution_ci_run_id']
            distribution_path = f'repos/{repository}/actions/runs/{distribution_id}'
            distribution_run = original['snapshots'][distribution_path]
            prefix = distribution_path + '/attempts/' + str(distribution_run['run_attempt']) + '/jobs?'
            distribution_jobs = [job for path, value in original['snapshots'].items()
                                 if path.startswith(prefix) for job in value['jobs']]
            C['verify'](dict(contract=C['CONTRACT'], mode='full', run=distribution_run, jobs=distribution_jobs),
                        repository, candidate, 'full', workflow='distribution.yml')
        require(bundle['inputs']['commit'] == candidate, 'full input source differs')
        I['manifest'](bundle['inputs'])
    elif mode == 'equivalent':
        anchor = bundle['baseline']
        original = anchor['approval']
        require(original['schema'] == 2 and original['verification'] == 'full', 'one direct full anchor is required')
        source = original['candidate']
        require(source['repository'] == repository, 'anchor repository differs')
        verify(original, repository, source['tag'], source['sha'])
        require(recorded['files'] == original['checker']['files'], 'verification implementation changed')
        require(anchor['identity']['repository'] == repository and anchor['identity']['tag'] == source['tag']
                and anchor['identity']['sha'] == source['sha'], 'anchor identity differs')
        require(anchor['identity']['sha256'] == B['sha256'](B['canonical'](original)), 'anchor digest differs')
        require(type(anchor['identity']['release_id']) is int and anchor['identity']['release_id'] > 0
                and type(anchor['identity']['asset_id']) is int and anchor['identity']['asset_id'] > 0,
                'anchor provider identity missing')
        require(bundle['proof']['baseline'] == original['inputs'], 'anchor tree proof differs')
        I['verify'](bundle['proof'], source['sha'], candidate)
    else:
        raise ValueError('unsupported verification mode')
    coverage = bundle['ci']['mode'] if mode == 'equivalent' else 'full'
    require(coverage in ('docs', 'full'), 'full or documentation CI required')
    C['verify'](bundle['ci'], repository, candidate, coverage)
    return dict(state='consistency_verified', candidate_repository=repository, candidate_sha=candidate,
                tag=tag, verification=mode)


def revoked(repository, identity):
    path = ROOT / 'release-approval-revocations.json'
    data = B['parse_json'](path.read_bytes())
    B['require'](data['schema'] == 1 and type(data['schema']) is int and isinstance(data['approvals'], list), 'invalid revocation registry')
    return any(item['repository'] == repository and item['tag'] == identity['tag']
               and item['sha256'] == identity['sha256'] for item in data['approvals'])


def load_anchor(repository, tag=None, full_only=False):
    archive = runpy.run_path(str(ROOT / 'verify-archived-release.py'))
    if tag is None:
        latest = B['parse_json'](archive['gh'](['api', f'repos/{repository}/releases/latest']))
        tag = latest['tag_name']
    B['require'](re.fullmatch('v[0-9]+\\.[0-9]+\\.[0-9]+', tag or ''), 'stable baseline tag required')
    observed = archive['release'](repository, tag)
    B['require'](observed['prerelease'] is False, 'stable baseline required')
    with tempfile.TemporaryDirectory(prefix='releaseway-anchor-') as directory:
        output = Path(directory) / 'approval.json'
        authenticated = archive['authenticate'](repository, tag, output=output)
        raw = output.read_bytes()
    bundle = B['parse_json'](raw)
    B['require'](B['canonical'](bundle) == raw, 'canonical baseline bundle required')
    B['require'](bundle['schema'] == 2, 'new-policy full baseline required')
    if bundle['verification'] == 'equivalent':
        B['require'](not full_only, 'equivalent anchor chains are not allowed')
        pinned = bundle['baseline']['identity']
        direct = load_anchor(repository, pinned['tag'], full_only=True)
        B['require'](direct['identity'] == pinned and direct['approval']['verification'] == 'full', 'original full anchor changed')
        return direct
    B['require'](bundle['verification'] == 'full', 'full baseline required')
    B['require'](bundle['full']['policy']['id'] == B['current_policy'](repository), 'full scenario policy changed')
    B['require'](authenticated['release_id'] == observed['id'], 'baseline Release changed during authentication')
    identity = dict(repository=repository, tag=tag, sha=authenticated['candidate'],
                    release_id=observed['id'], asset_id=authenticated['asset_id'], sha256=authenticated['sha256'])
    B['require'](not revoked(repository, identity), 'full approval explicitly revoked')
    return dict(identity=identity, approval=bundle)


def equivalent(repository, tag, candidate, root, anchor, ci):
    B['require'](not revoked(repository, anchor['identity']), 'full approval explicitly revoked')
    original = anchor['approval']
    B['require'](original['schema'] == 2 and original['verification'] == 'full', 'direct full baseline required')
    source = original['candidate']['sha']
    proof = I['compare'](root, source, candidate)
    result = dict(schema=2, verification='equivalent', candidate=candidate_identity(repository, tag, candidate),
                  policy=policy(), checker=checker(repository, ci['run']['head_sha']),
                  baseline=anchor, proof=proof, ci=ci)
    verify(result, repository, tag, candidate)
    return result
