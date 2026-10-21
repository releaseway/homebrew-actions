"""Prepare one frozen approval asset before publication, or reconcile an immutable archive."""
import argparse
import json
import os
from pathlib import Path
import re
import runpy
import zipfile
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parent
B = runpy.run_path(str(ROOT / 'evidence-bundle.py'))
A = runpy.run_path(str(ROOT / 'verify-archived-release.py'))


def current_policy(repository):
    return 'release-v1'


def bind_inputs(bundle, repository, tag, candidate, run_ids, distribution_run, policy_id=None):
    B['verify_bundle'](bundle, repository, tag, candidate)
    B['require'](sorted(bundle['requests']['acceptance_runs']) == sorted(run_ids)
                 and bundle['requests']['distribution_run'] == distribution_run,
                 'archive_conflict: approval run inputs differ from frozen archive')
    if policy_id is not None:
        B['require'](bundle['policy']['id'] == policy_id, 'archive_conflict: frozen policy differs')


def prepare(repository, tag, candidate, run_ids, distribution_run, output):
    candidate = candidate.lower()
    B['require'](repository in B['policy_data']('release-v1')['repositories']
                 and re.fullmatch(r'[0-9a-f]{40}', candidate)
                 and re.fullmatch(r'v?[0-9]+\.[0-9]+\.[0-9]+', tag), 'invalid official release identity')
    B['require'](all(type(run_id) is int and run_id > 0 for run_id in run_ids), 'invalid acceptance run ID')
    B['require'](A['tag_commit'](repository, tag) == candidate, 'release tag candidate mismatch')
    observed = A['release'](repository, tag, missing=True)
    policy_id = current_policy(repository)
    if observed is not None and observed['draft'] is False:
        with tempfile.TemporaryDirectory(prefix='releaseway-reconcile-') as directory:
            frozen = Path(directory) / 'releaseway-evidence.json'
            authenticated = A['authenticate'](repository, tag, candidate, frozen)
            raw = frozen.read_bytes()
            bundle = B['parse_json'](raw)
            bind_inputs(bundle, repository, tag, candidate, run_ids, distribution_run)
            B['atomic_write'](output, raw)
        return {'state': 'existing', 'release-url': authenticated['release_url']}
    if observed is not None and observed['assets']:
        B['require'](len(observed['assets']) == 1 and observed['assets'][0]['name'] == 'releaseway-evidence.json',
                     'archive_conflict: unexpected draft assets; inspect draft explicitly')
        raw = A['download_bundle'](repository, tag, observed, authenticated=False)
        bundle = B['parse_json'](raw)
        bind_inputs(bundle, repository, tag, candidate, run_ids, distribution_run, policy_id)
        # A draft is still an approval in progress. A rerun of a required run changes its identity.
        module = runpy.run_path(str(ROOT / 'verify-release-evidence.py'))
        entries = list(bundle['result']['acceptance'])
        fixture = module['POLICIES'][repository][1]
        required_runs = [(fixture, item['run_id'], item['run_attempt']) for item in entries]
        if distribution_run:
            item = bundle['result']['distribution']
            required_runs.append(('releaseway/release-fixture', item['run_id'], item['run_attempt']))
        for repo, run_id, attempt in required_runs:
            run = module['api_json'](f'repos/{repo}/actions/runs/{run_id}')
            B['require'](run['run_attempt'] == attempt, 'archive_conflict: required run attempt changed')
        for workflow in [module['POLICIES'][repository][0]] + (['distribution.yml'] if distribution_run else []):
            ci = module['require_ci'](repository, candidate, workflow)
            old_ci = bundle['snapshots'][f'repos/{repository}/actions/runs/{ci["id"]}'] if f'repos/{repository}/actions/runs/{ci["id"]}' in bundle['snapshots'] else None
            B['require'](old_ci is not None and ci['run_attempt'] == old_ci['run_attempt'],
                         'archive_conflict: required CI changed')
        B['atomic_write'](output, raw)
        return {'state': 'prepared', 'release-url': ''}
    module = runpy.run_path(str(ROOT / 'verify-release-evidence.py'))
    checker = B['checker_identity'](repository, os.environ.get('GITHUB_SHA', '').lower())
    _, bundle = B['collect'](module, repository, candidate, run_ids, distribution_run, tag, checker,
                            B['collection_identity'](repository), policy_id)
    B['atomic_write'](output, B['canonical'](bundle))
    return {'state': 'prepared', 'release-url': ''}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repository', required=True)
    parser.add_argument('--tag', required=True)
    parser.add_argument('--candidate', required=True)
    parser.add_argument('--acceptance-runs', required=True)
    parser.add_argument('--distribution-run', type=int)
    parser.add_argument('--output', default='releaseway-evidence.json')
    args = parser.parse_args()
    try:
        result = prepare(args.repository, args.tag, args.candidate,
                         [int(item.strip()) for item in args.acceptance_runs.split(',')],
                         args.distribution_run, args.output)
        with open(os.environ['GITHUB_OUTPUT'], 'a') as stream:
            for key, value in result.items():
                stream.write(key + '=' + value + '\n')
        print(json.dumps(result))
    except (ValueError, KeyError, TypeError, OSError, zipfile.BadZipFile, subprocess.TimeoutExpired) as error:
        parser.exit(2 if isinstance(error, OSError) else 1, 'Release approval failed: ' + str(error) + '\n')
