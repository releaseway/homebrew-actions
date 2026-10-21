"""Preserve the allowlisted original approval, or reuse an authenticated frozen sidecar."""
import argparse
import json
import os
from pathlib import Path
import runpy

ROOT = Path(__file__).resolve().parent
B = runpy.run_path(str(ROOT / 'evidence-bundle.py'))
A = runpy.run_path(str(ROOT / 'verify-archived-release.py'))


def prepare(repository, tag, publication_run, run_ids, distribution_run, output):
    policy = B['policy_data']('legacy-20261020')
    expected = policy['releases'].get(repository)
    B['require'](expected is not None and tag == expected['tag'] and publication_run == expected['publication_run']
                 and sorted(run_ids) == sorted(expected['acceptance_runs']) and distribution_run == expected['distribution_run'],
                 'legacy backfill inputs are not allowlisted')
    sidecar = A['release'](repository, 'evidence-' + tag, missing=True)
    if sidecar is not None:
        if sidecar['draft'] is False:
            A['authenticate'](repository, tag, expected['candidate'], output)
            return {'state': 'existing', 'collector': A['tag_commit'](repository, 'evidence-' + tag)}
        raise ValueError('archive_conflict: unpublished sidecar draft requires explicit inspection before retry')
    original = A['release'](repository, tag)
    B['require'](original['id'] == expected['release_id'] and original['immutable'] is True
                 and original['draft'] is False and original['prerelease'] is False, 'original release identity mismatch')
    B['require'](A['tag_commit'](repository, tag) == expected['candidate'], 'original tag changed')
    latest = B['parse_json'](A['gh'](['api', f'repos/{repository}/releases/latest']))
    B['require'](latest['id'] == expected['release_id'], 'original stable latest changed')
    module = runpy.run_path(str(ROOT / 'verify-release-evidence.py'))
    publication = B['run_snapshot'](module['api_json'](f'repos/{repository}/actions/runs/{publication_run}'))
    B['require'](publication['id'] == publication_run and publication['event'] == 'workflow_dispatch', 'publication identity mismatch')
    module['successful_run'](publication, repository, expected['candidate'], 'release.yml')
    jobs = list(module['pages'](f'repos/{repository}/actions/runs/{publication_run}/attempts/{publication["run_attempt"]}/jobs', 'jobs'))
    collection = B['collection_identity'](repository)
    collection.update(mode='backfill', original_release={key: original[key] for key in
                      ['id', 'tag_name', 'immutable', 'published_at', 'draft', 'prerelease']},
                      publication=publication,
                      publication_jobs=B['snapshot']({'jobs': jobs})['jobs'])
    checker = B['checker_identity'](repository, os.environ.get('GITHUB_SHA', '').lower())
    try:
        _, bundle = B['collect'](module, repository, expected['candidate'], run_ids, distribution_run,
                                tag, checker, collection, 'legacy-20261020')
    except ValueError as error:
        raise ValueError('source_unavailable or invalid original evidence: ' + str(error)) from error
    B['atomic_write'](output, B['canonical'](bundle))
    return {'state': 'prepared', 'collector': checker['commit']}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repository', required=True)
    parser.add_argument('--tag', required=True)
    parser.add_argument('--publication-run', required=True, type=int)
    parser.add_argument('--acceptance-runs', required=True)
    parser.add_argument('--distribution-run', type=int)
    parser.add_argument('--output', default='releaseway-evidence.json')
    args = parser.parse_args()
    try:
        result = prepare(args.repository, args.tag, args.publication_run,
                        [int(item.strip()) for item in args.acceptance_runs.split(',')],
                        args.distribution_run, args.output)
        with open(os.environ['GITHUB_OUTPUT'], 'a') as stream:
            for key, value in result.items():
                stream.write(key + '=' + value + '\n')
        print(json.dumps(dict(result, tag=args.tag)))
    except (ValueError, KeyError, TypeError, OSError) as error:
        parser.exit(1, 'Archive preparation failed: ' + str(error) + '\n')
