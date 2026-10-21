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
    return B['current_policy'](repository)


def bind_inputs(bundle, repository, tag, candidate, run_ids, distribution_run, policy_id=None):
    B['verify_bundle'](bundle, repository, tag, candidate)
    B['require'](sorted(bundle['requests']['acceptance_runs']) == sorted(run_ids)
                 and bundle['requests']['distribution_run'] == distribution_run,
                 'archive_conflict: approval run inputs differ from frozen archive')
    if policy_id is not None:
        B['require'](bundle['policy']['id'] == policy_id, 'archive_conflict: frozen policy differs')


def _prepare_legacy(repository, tag, candidate, run_ids, distribution_run, output):
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
    checker = B['checker_identity'](repository, os.environ.get('GITHUB_SHA', '').lower(), policy_id)
    _, bundle = B['collect'](module, repository, candidate, run_ids, distribution_run, tag, checker,
                            B['collection_identity'](repository), policy_id)
    B['atomic_write'](output, B['canonical'](bundle))
    return {'state': 'prepared', 'release-url': ''}




def approval_result(bundle, state, url=''):
    mode = bundle.get('verification', 'legacy')
    original = bundle if bundle['schema'] == 1 else bundle['full'] if mode == 'full' else bundle['baseline']['approval']['full']
    ci_id = bundle['result']['ci_run_id'] if bundle['schema'] == 1 else bundle['ci']['run']['id']
    return dict(state=state, **{'release-url': url, 'verification': mode,
                'original-sha': original['candidate']['sha'],
                'verified-at': original['collection']['collected_at'], 'ci-run': ci_id})


def bind_v2(bundle, repository, tag, candidate, run_ids, distribution_run, baseline_tag=None):
    B['verify_bundle'](bundle, repository, tag, candidate)
    if bundle['verification'] == 'full':
        expected = bundle['full']['requests']
        B['require']((not run_ids or sorted(run_ids) == sorted(expected['acceptance_runs']))
                     and (distribution_run is None or distribution_run == expected['distribution_run'])
                     and baseline_tag is None, 'archive_conflict: full approval inputs differ')
    else:
        B['require'](not run_ids and distribution_run is None
                     and (baseline_tag is None or baseline_tag == bundle['baseline']['identity']['tag']),
                     'archive_conflict: equivalent approval inputs differ')


def check_pending_full(bundle):
    module = runpy.run_path(str(ROOT / 'verify-release-evidence.py'))
    repository = bundle['candidate']['repository']
    candidate = bundle['candidate']['sha']
    fixture = module['POLICIES'][repository][1]
    selected = [(fixture, item['run_id']) for item in bundle['result']['acceptance']]
    if 'distribution' in bundle['result']:
        selected.append(('releaseway/release-fixture', bundle['result']['distribution']['run_id']))
    for repo, run_id in selected:
        path = f'repos/{repo}/actions/runs/{run_id}'
        old = bundle['snapshots'][path]
        current = module['api_json'](path)
        B['require'](current['run_attempt'] == old['run_attempt']
                     and current['head_sha'] == old['head_sha']
                     and current['status'] == 'completed' and current['conclusion'] == 'success',
                     'archive_conflict: acceptance identity or attempt changed')
    for workflow in [module['POLICIES'][repository][0]] + (['distribution.yml'] if 'distribution' in bundle['result'] else []):
        ci = module['require_ci'](repository, candidate, workflow)
        old = bundle['snapshots'].get(f'repos/{repository}/actions/runs/{ci["id"]}')
        B['require'](old is not None and ci['run_attempt'] == old['run_attempt'], 'archive_conflict: current CI changed')


def prepare(repository, tag, candidate, run_ids, distribution_run, output, force_full=False, baseline_tag=None):
    reuse = runpy.run_path(str(ROOT / 'release-reuse.py'))
    candidate = candidate.lower()
    B['require'](repository in B['policy_data']('release-v1')['repositories']
                 and re.fullmatch(r'[0-9a-f]{40}', candidate)
                 and re.fullmatch(r'v?[0-9]+\.[0-9]+\.[0-9]+', tag), 'invalid official release identity')
    B['require'](all(type(value) is int and value > 0 for value in run_ids)
                 and (distribution_run is None or (type(distribution_run) is int and distribution_run > 0)),
                 'invalid acceptance run ID')
    B['require'](A['tag_commit'](repository, tag) == candidate, 'release tag candidate mismatch')
    observed = A['release'](repository, tag, missing=True)
    if observed is not None and observed['draft'] is False:
        with tempfile.TemporaryDirectory(prefix='releaseway-existing-') as directory:
            frozen = Path(directory) / 'approval.json'
            authenticated = A['authenticate'](repository, tag, candidate, frozen)
            raw = frozen.read_bytes()
        bundle = B['parse_json'](raw)
        if bundle['schema'] == 1:
            bind_inputs(bundle, repository, tag, candidate, run_ids or bundle['requests']['acceptance_runs'],
                        distribution_run if distribution_run is not None else bundle['requests']['distribution_run'])
        else:
            bind_v2(bundle, repository, tag, candidate, run_ids, distribution_run, baseline_tag)
        B['atomic_write'](output, raw)
        return approval_result(bundle, 'existing', authenticated['release_url'])
    if observed is not None and observed['assets']:
        B['require'](len(observed['assets']) == 1 and observed['assets'][0]['name'] == 'releaseway-evidence.json',
                     'archive_conflict: unexpected draft assets')
        raw = A['download_bundle'](repository, tag, observed, authenticated=False)
        bundle = B['parse_json'](raw)
        if bundle['schema'] == 1:
            # Preserve retries of approvals made before the new policy was introduced.
            B['verify_bundle'](bundle, repository, tag, candidate)
            return _prepare_legacy(repository, tag, candidate, run_ids or bundle['requests']['acceptance_runs'],
                                   distribution_run if distribution_run is not None else bundle['requests']['distribution_run'], output)
        bind_v2(bundle, repository, tag, candidate, run_ids, distribution_run, baseline_tag)
        B['require'](not force_full or bundle['verification'] == 'full', 'archive_conflict: frozen verification mode differs')
        current = reuse['C']['collect'](repository, candidate, bundle['ci']['mode'])
        B['require'](current == bundle['ci'], 'archive_conflict: current CI changed')
        if bundle['verification'] == 'full':
            B['require'](bundle['checker']['files'] == reuse['checker'](repository, candidate)['files'],
                         'archive_conflict: pending full verifier changed')
            check_pending_full(bundle['full'])
        else:
            anchor = reuse['load_anchor'](repository, bundle['baseline']['identity']['tag'])
            proposed = reuse['equivalent'](repository, tag, candidate, '.', anchor, current)
            B['require'](B['canonical'](proposed) == raw, 'archive_conflict: baseline or equivalent approval changed')
        B['atomic_write'](output, raw)
        return approval_result(bundle, 'prepared')
    if run_ids or distribution_run is not None:
        B['require'](baseline_tag is None, 'choose fresh acceptance or a baseline, not both')
        # Collect into a temporary path so failed wrapping cannot replace a prior output.
        with tempfile.TemporaryDirectory(prefix='releaseway-full-') as directory:
            inner_path = Path(directory) / 'full.json'
            _prepare_legacy(repository, tag, candidate, run_ids, distribution_run, inner_path)
            inner = B['parse_json'](inner_path.read_bytes())
        bundle = reuse['full'](inner, '.', reuse['ci_from_full'](inner))
    else:
        B['require'](not force_full, 'requires-full: supply exact-candidate acceptance run IDs')
        try:
            anchor = reuse['load_anchor'](repository, baseline_tag)
            current = reuse['C']['collect'](repository, candidate)
            bundle = reuse['equivalent'](repository, tag, candidate, '.', anchor, current)
        except (ValueError, KeyError, TypeError, OSError, subprocess.SubprocessError) as error:
            raise ValueError('requires-full: ' + str(error) + '; supply exact-candidate acceptance run IDs') from error
    B['verify_bundle'](bundle, repository, tag, candidate)
    B['atomic_write'](output, B['canonical'](bundle))
    return approval_result(bundle, 'prepared')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repository', required=True)
    parser.add_argument('--tag', required=True)
    parser.add_argument('--candidate', required=True)
    parser.add_argument('--acceptance-runs', default='')
    parser.add_argument('--baseline-tag')
    parser.add_argument('--force-full', action='store_true')
    parser.add_argument('--distribution-run', type=int)
    parser.add_argument('--output', default='releaseway-evidence.json')
    args = parser.parse_args()
    try:
        result = prepare(args.repository, args.tag, args.candidate,
                         [int(item.strip()) for item in args.acceptance_runs.split(',') if item.strip()],
                         args.distribution_run, args.output, args.force_full, args.baseline_tag)
        with open(os.environ['GITHUB_OUTPUT'], 'a') as stream:
            for key, value in result.items():
                stream.write(key + '=' + value + '\n')
        print(json.dumps(result))
    except (ValueError, KeyError, TypeError, OSError, zipfile.BadZipFile, subprocess.TimeoutExpired) as error:
        parser.exit(2 if isinstance(error, OSError) else 1, 'Release approval failed: ' + str(error) + '\n')
