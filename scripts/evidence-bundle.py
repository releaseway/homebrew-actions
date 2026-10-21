"""Bounded evidence collection and offline replay of the installed release policy."""
import base64
import binascii
import hashlib
import io
import json
import os
import re
import runpy
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

MAX_BUNDLE = 8 * 1024 * 1024
MAX_ZIP = 1024 * 1024
MAX_SNAPSHOTS = 2 * 1024 * 1024
ROOT = Path(__file__).resolve().parent
POLICY_FILE = ROOT / 'release-evidence-policies.json'


def require(condition, message):
    if not condition:
        raise ValueError(message)


def canonical(value):
    return (json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False) + '\n').encode()


def sha256(raw):
    return hashlib.sha256(raw).hexdigest()


def timestamp(value):
    require(isinstance(value, str) and datetime.fromisoformat(value.replace('Z', '+00:00')).utcoffset() is not None,
            'invalid evidence timestamp')


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, 'duplicate JSON key: ' + key)
        result[key] = value
    return result


def parse_json(raw):
    return json.loads(raw, object_pairs_hook=unique_object,
                      parse_constant=lambda value: (_ for _ in ()).throw(ValueError('invalid JSON number')))


def reject_bool_ids(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if key in ('id', 'run_id', 'run_attempt', 'artifact_id') or key.endswith('_id') or key.endswith('Id'):
                require(not isinstance(item, bool), 'boolean ID is invalid')
            reject_bool_ids(item)
    elif isinstance(value, list):
        for item in value:
            reject_bool_ids(item)


def receipts(raw, kind):
    require(0 < len(raw) <= MAX_ZIP, 'artifact ZIP exceeds limit')
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        members = archive.infolist()
        names = [member.filename for member in members]
        require(len(set(names)) == len(names), 'duplicate ZIP member')
        require(sum(member.file_size for member in members) <= MAX_ZIP, 'decompressed artifact exceeds limit')
        expected = 6 if kind == 'distribution' else 1
        require(len(members) == expected, 'unexpected artifact member count')
        result = []
        for member in members:
            name = member.filename
            path = PurePosixPath(name)
            require(not member.is_dir() and not path.is_absolute() and '..' not in path.parts
                    and '\\' not in name and not (member.external_attr >> 16 & 0o170000) == 0o120000,
                    'invalid artifact member path')
            require((path.name == 'receipt.json' if kind == 'distribution' else
                     name == ('homebrew-validation.json' if kind == 'native' else 'acceptance.json')),
                    'unexpected artifact member')
            require(member.file_size <= 100 * 1024, 'receipt exceeds limit')
            item = parse_json(archive.read(member))
            reject_bool_ids(item)
            result.append(item)
        return result


def policy_data(policy_id):
    policies = parse_json(POLICY_FILE.read_bytes())
    require(policy_id in policies, 'unsupported policy')
    return policies[policy_id]


def current_policy(repository):
    return 'homebrew-platforms-v2' if repository == 'releaseway/homebrew-actions' else 'release-v1'


RUN_FIELDS = ('id', 'run_attempt', 'workflow_id', 'head_sha', 'head_branch', 'path', 'event', 'status',
              'conclusion', 'created_at', 'updated_at', 'run_started_at')
ARTIFACT_FIELDS = ('id', 'name', 'size_in_bytes', 'digest', 'expired', 'created_at', 'expires_at')
JOB_FIELDS = ('id', 'name', 'status', 'conclusion', 'started_at', 'completed_at', 'runner_name',
              'runner_group_name', 'labels')


def run_snapshot(run):
    result = {key: run[key] for key in RUN_FIELDS if key in run}
    result['repository'] = {'full_name': run['repository']['full_name']}
    return result


def snapshot(value):
    if 'workflow_runs' in value:
        return {'workflow_runs': [run_snapshot(run) for run in value['workflow_runs']]}
    if 'artifacts' in value:
        return {'artifacts': [{key: artifact[key] for key in ARTIFACT_FIELDS if key in artifact}
                              for artifact in value['artifacts']]}
    if 'jobs' in value:
        return {'jobs': [{key: job[key] for key in JOB_FIELDS if key in job} for job in value['jobs']]}
    return run_snapshot(value)


def checker_identity(repository, commit, policy_id='release-v1'):
    require(re.fullmatch(r'[0-9a-f]{40}', commit or ''), 'checker commit must be a full SHA')
    names = ('verify-release-evidence.py', 'verify-distribution-receipts.py', 'evidence-bundle.py',
             'verify-evidence-bundle.py', 'release-evidence-policies.json')
    if policy_id == 'homebrew-platforms-v2':
        names += ('verify-homebrew-features.py', 'verify-homebrew-native.py')
    return {'repository': repository, 'commit': commit,
            'files': {name: sha256((ROOT / name).read_bytes()) for name in names}}


def atomic_write(path, raw):
    require(len(raw) <= MAX_BUNDLE, 'bundle exceeds limit')
    path = Path(path)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.' + path.name + '.', delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def collect(module, repository, candidate, run_ids, distribution_run, tag, checker, collection, policy_id='release-v1'):
    require(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*', tag or ''), 'invalid release tag')
    policy = policy_data(policy_id)
    responses, archives = {}, {}
    globals_ = module['verify'].__globals__
    live_json, live_bytes = globals_['api_json'], globals_['api_bytes']

    def get_json(path):
        value = snapshot(live_json(path))
        reject_bool_ids(value)
        require(path not in responses or responses[path] == value, 'run changed during collection')
        responses[path] = value
        require(len(canonical(responses)) <= MAX_SNAPSHOTS, 'API snapshots exceed limit')
        return value

    def get_bytes(path):
        raw = live_bytes(path)
        # api_json also uses this transport. Only artifact downloads are ZIPs.
        if not re.fullmatch(r'repos/[^/]+/[^/]+/actions/artifacts/[1-9][0-9]*/zip', path):
            return raw
        require(0 < len(raw) <= MAX_ZIP, 'artifact ZIP exceeds limit')
        archives[path] = {'sha256': sha256(raw), 'base64': base64.b64encode(raw).decode()}
        return raw

    globals_ = module['verify'].__globals__
    globals_['api_json'], globals_['api_bytes'] = get_json, get_bytes
    try:
        result = module['verify'](repository, candidate, run_ids, distribution_run, policy_id=policy_id)
        selected = [(repository, result['ci_run_id'])]
        if 'distribution_ci_run_id' in result:
            selected.append((repository, result['distribution_ci_run_id']))
        fixture = module['POLICIES'][repository][1]
        selected.extend((fixture, entry['run_id']) for entry in result['acceptance'])
        if distribution_run:
            selected.append(('releaseway/release-fixture', distribution_run))
        runs = {}
        for repo, run_id in selected:
            path = f'repos/{repo}/actions/runs/{run_id}'
            run = get_json(path)
            require(type(run.get('run_attempt')) is int and run['run_attempt'] > 0, 'run attempt missing')
            runs[path] = run
            list(module['pages'](path + '/attempts/' + str(run['run_attempt']) + '/jobs', 'jobs'))
        # Provider digests bind precisely the bytes downloaded, not extracted/repacked files.
        for path, archive in archives.items():
            artifact_id = int(path.split('/artifacts/')[1].split('/')[0])
            metadata = [a for response in responses.values() for a in response.get('artifacts', [])
                        if a['id'] == artifact_id]
            require(len(metadata) == 1 and metadata[0].get('digest') == 'sha256:' + archive['sha256'],
                    'artifact provider digest mismatch')
        # Freeze only a stable view: a rerun or newer push CI invalidates this collection.
        for path, run in runs.items():
            latest = snapshot(live_json(path))
            require(latest == run, 'run changed during collection')
        for workflow in [module['POLICIES'][repository][0]] + (['distribution.yml'] if repository == 'releaseway/actions' else []):
            latest = module['require_ci'](repository, candidate.lower(), workflow)
            require(latest['id'] in [entry[1] for entry in selected if entry[0] == repository],
                    'latest exact-SHA CI changed during collection')
        bundle = {'schema': 1, 'candidate': {'repository': repository, 'tag': tag, 'sha': candidate.lower()},
                  'policy': {'id': policy_id, 'data': policy, 'sha256': sha256(canonical(policy))},
                  'checker': checker, 'collection': collection, 'result': result,
                  'requests': {'acceptance_runs': run_ids, 'distribution_run': distribution_run},
                  'snapshots': responses, 'archives': archives}
        verify_bundle(bundle, repository, tag, candidate.lower(), module=module)
        return result, bundle
    finally:
        globals_['api_json'], globals_['api_bytes'] = live_json, live_bytes


def verify_bundle(bundle, repository, tag, candidate, module=None):
    require(type(bundle.get('schema')) is int and bundle['schema'] == 1, 'unsupported bundle schema')
    reject_bool_ids(bundle)
    require(re.fullmatch(r'[0-9a-f]{40}', candidate or ''), 'invalid candidate SHA')
    require(bundle['candidate'] == {'repository': repository, 'tag': tag, 'sha': candidate}, 'candidate identity mismatch')
    policy = bundle['policy']
    expected_policy = policy_data(policy['id'])
    require(policy['data'] == expected_policy and policy['sha256'] == sha256(canonical(expected_policy)), 'policy mismatch')
    require(repository in expected_policy['repositories'], 'repository not allowed by policy')
    require(len(canonical(bundle)) <= MAX_BUNDLE, 'bundle exceeds limit')
    responses = bundle['snapshots']
    require(len(canonical(responses)) <= MAX_SNAPSHOTS, 'API snapshots exceed limit')
    archives = bundle['archives']
    decoded = {}
    for path, archive in archives.items():
        try:
            raw = base64.b64decode(archive['base64'], validate=True)
        except (ValueError, binascii.Error) as error:
            raise ValueError('invalid artifact base64') from error
        require(0 < len(raw) <= MAX_ZIP and sha256(raw) == archive['sha256'], 'artifact hash mismatch')
        artifact_id = int(path.split('/artifacts/')[1].split('/')[0])
        matching = [a for response in responses.values() for a in response.get('artifacts', []) if a['id'] == artifact_id]
        require(len(matching) == 1 and matching[0]['digest'] == 'sha256:' + sha256(raw), 'provider digest mismatch')
        require(type(matching[0]['size_in_bytes']) is int and 0 < matching[0]['size_in_bytes'] <= MAX_ZIP, 'invalid provider artifact size')
        for key in ('created_at', 'expires_at'):
            timestamp(matching[0][key])
        name = matching[0]['name']
        kind = 'distribution' if name.startswith('distribution-evidence-') else 'native' if name.startswith('homebrew-validation-') else 'acceptance'
        receipts(raw, kind)
        decoded[path] = raw
    checker = bundle['checker']
    require(checker['repository'] in expected_policy['repositories'] and re.fullmatch(r'[0-9a-f]{40}', checker['commit']), 'invalid checker identity')
    require(set(checker['files']) == set(checker_identity(repository, candidate, policy['id'])['files']), 'checker file inventory mismatch')
    require(all(re.fullmatch(r'[0-9a-f]{64}', value) for value in checker['files'].values()), 'invalid checker digest')
    collection = bundle['collection']
    require(len(canonical(responses)) + len(canonical(collection)) <= MAX_SNAPSHOTS, 'API snapshots exceed limit')
    require(collection['mode'] in ('pre-publication', 'backfill') and collection['collected_at'], 'invalid collection mode/time')
    timestamp(collection['collected_at'])
    require(collection['workflow']['repository'] == repository and type(collection['workflow']['run_id']) is int
            and collection['workflow']['run_id'] > 0 and type(collection['workflow']['run_attempt']) is int
            and collection['workflow']['run_attempt'] > 0, 'invalid collection workflow identity')
    require(re.fullmatch(r'[0-9a-f]{40}', collection['workflow']['head_sha']), 'invalid collection commit')
    require(collection['workflow']['head_sha'] == checker['commit'], 'collector workflow/checker commit mismatch')
    if policy['id'] == 'legacy-20261020':
        expected = expected_policy['releases'][repository]
        require(collection['mode'] == 'backfill' and tag == expected['tag'] and candidate == expected['candidate'],
                'legacy policy applies only to allowlisted original releases')
        require(sorted(bundle['requests']['acceptance_runs']) == sorted(expected['acceptance_runs'])
                and bundle['requests']['distribution_run'] == expected['distribution_run'], 'original approval input mismatch')
        original = collection['original_release']
        require(original['id'] == expected['release_id'] and original['tag_name'] == tag and original['immutable'] is True
                and original['draft'] is False and original['prerelease'] is False and original['published_at'],
                'original release snapshot mismatch')
        publication = collection['publication']
        require(publication['id'] == expected['publication_run'] and publication['head_sha'] == candidate
                and publication['repository']['full_name'] == repository and publication['path'] == '.github/workflows/release.yml'
                and publication['event'] == 'workflow_dispatch' and publication['status'] == 'completed'
                and publication['conclusion'] == 'success' and type(publication['run_attempt']) is int
                and publication['run_attempt'] > 0, 'original publication snapshot mismatch')
        require(collection['publication_jobs'] and all(job['status'] == 'completed'
                for job in collection['publication_jobs']), 'original publication jobs incomplete')
    else:
        require(collection['mode'] == 'pre-publication', 'fresh policy cannot backfill an original release')
    if module is None:
        module = runpy.run_path(str(ROOT / 'verify-release-evidence.py'))
    globals_ = module['verify'].__globals__
    original_json, original_bytes = globals_['api_json'], globals_['api_bytes']
    used_responses, used_archives = set(), set()
    def offline_json(path):
        require(path in responses, 'missing API snapshot: ' + path)
        used_responses.add(path)
        return responses[path]
    def offline_bytes(path):
        require(path in decoded, 'missing original ZIP: ' + path)
        used_archives.add(path)
        return decoded[path]
    globals_['api_json'], globals_['api_bytes'] = offline_json, offline_bytes
    try:
        result = module['verify'](repository, candidate, bundle['requests']['acceptance_runs'], bundle['requests']['distribution_run'], policy_id=policy['id'])
        require(result == bundle['result'], 'approval result mismatch')
        for response in responses.values():
            for run in response.get('workflow_runs', []):
                require(type(run.get('run_attempt')) is int and run['run_attempt'] > 0, 'CI attempt missing')
        for entry in result['acceptance'] + ([result['distribution']] if 'distribution' in result else []):
            repo = module['POLICIES'][repository][1] if entry in result['acceptance'] else 'releaseway/release-fixture'
            run = responses[f'repos/{repo}/actions/runs/{entry["run_id"]}']
            require(run['run_attempt'] == entry['run_attempt'], 'snapshot attempt mismatch')
        ci_ids = [result['ci_run_id']] + ([result['distribution_ci_run_id']] if 'distribution_ci_run_id' in result else [])
        selected = [(repository, run_id) for run_id in ci_ids]
        selected += [(module['POLICIES'][repository][1], item['run_id']) for item in result['acceptance']]
        if 'distribution' in result:
            selected.append(('releaseway/release-fixture', result['distribution']['run_id']))
        for repo, run_id in selected:
            path = f'repos/{repo}/actions/runs/{run_id}'
            run = offline_json(path)
            require(type(run['id']) is int and run['id'] == run_id and type(run['run_attempt']) is int
                    and run['run_attempt'] > 0, 'invalid provider run identity')
            require(type(run['workflow_id']) is int and run['workflow_id'] > 0, 'provider workflow ID missing')
            for key in ('created_at', 'updated_at', 'run_started_at'):
                timestamp(run[key])
            require(run['status'] == 'completed' and run['conclusion'] == 'success', 'provider run incomplete')
            if run_id in ci_ids and repo == repository:
                listed = [item for response in responses.values() for item in response.get('workflow_runs', [])
                          if item['id'] == run_id]
                require(len(listed) == 1 and listed[0] == run, 'CI list/detail snapshot mismatch')
            jobs = list(module['pages'](path + '/attempts/' + str(run['run_attempt']) + '/jobs', 'jobs'))
            require(jobs and all(job['status'] == 'completed' for job in jobs), 'provider jobs incomplete')
            require(all(set(JOB_FIELDS) <= set(job) and type(job['id']) is int and job['id'] > 0
                        and isinstance(job['name'], str) and job['name'] and isinstance(job['labels'], list)
                        and isinstance(job['conclusion'], str) and job['conclusion'] for job in jobs), 'provider job metadata missing')
        require(used_responses == set(responses) and used_archives == set(archives), 'unexpected evidence snapshot or archive')
        return {'state': 'consistency_verified', 'candidate_repository': repository, 'candidate_sha': candidate, 'tag': tag}
    finally:
        globals_['api_json'], globals_['api_bytes'] = original_json, original_bytes


def collection_identity(repository):
    return {'mode': 'pre-publication', 'collected_at': datetime.now(timezone.utc).isoformat(),
            'workflow': {'repository': repository, 'run_id': int(os.environ.get('GITHUB_RUN_ID', '0')),
                         'run_attempt': int(os.environ.get('GITHUB_RUN_ATTEMPT', '0')),
                         'head_sha': os.environ.get('GITHUB_SHA', '').lower(),
                         'path': os.environ.get('GITHUB_WORKFLOW_REF', ''),
                         'status': 'in_progress', 'conclusion': None}}
