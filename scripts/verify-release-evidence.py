"""Read-only release readiness check. Kept identical in the three action repositories."""
import argparse
import base64
import io
import json
import re
import subprocess
import zipfile
from pathlib import Path, PurePosixPath
import runpy

verify_distribution_receipts = runpy.run_path(str(Path(__file__).with_name('verify-distribution-receipts.py')))['verify_receipts']


POLICIES = {
    'releaseway/actions': ('test.yml', 'releaseway/release-fixture', {'actions-all': ('release-notes-acceptance.yml', 'verified')}),
    'releaseway/homebrew-actions': ('test.yml', 'releaseway/release-fixture', {'homebrew-public': ('homebrew-acceptance.yml', 'verified')}),
    'releaseway/npm-actions': ('check.yml', 'releaseway/npm-actions-fixture', {'npm-stage': ('publish.yml', 'staged'), 'npm-direct': ('direct.yml', 'published')}),
}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def api_bytes(path):
    # gh follows the artifact download redirect; never print the signed URL or token.
    result = subprocess.run(['gh', 'api', '-H', 'Accept: application/vnd.github+json', path],
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=60)
    require(result.returncode == 0, 'GitHub evidence lookup failed; check Actions read access and artifact retention')
    require(len(result.stdout) <= 1024 * 1024, 'evidence response too large')
    return result.stdout


def api_json(path):
    return json.loads(api_bytes(path))


def pages(path, key):
    separator = '&' if '?' in path else '?'
    for page in range(1, 101):
        items = api_json(f'{path}{separator}per_page=100&page={page}')[key]
        yield from items
        if len(items) < 100:
            return
    raise ValueError('evidence pagination limit exceeded')


def successful_run(run, repository, sha, workflow):
    require(run['repository']['full_name'] == repository, 'run repository mismatch')
    require(run['head_sha'].lower() == sha, 'run commit mismatch')
    require(run['path'] == '.github/workflows/' + workflow, 'run workflow mismatch')
    require(run['status'] == 'completed' and run['conclusion'] == 'success', 'required run is incomplete or unsuccessful')


def read_evidence(archive):
    with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
        files = bundle.infolist()
        require(len(files) == 1 and files[0].filename == 'acceptance.json', 'artifact must contain only acceptance.json')
        require(files[0].file_size <= 100 * 1024, 'acceptance.json too large')
        return json.loads(bundle.read(files[0]))


def require_ci(repository, candidate, workflow):
    runs = [run for run in pages(f'repos/{repository}/actions/workflows/{workflow}/runs?head_sha={candidate}&event=push', 'workflow_runs') if run.get('event') == 'push']
    require(runs, 'required exact-SHA CI run missing')
    ci = max(runs, key=lambda run: run['id'])
    successful_run(ci, repository, candidate, workflow)
    return ci


def verify_distribution(candidate, run_id):
    require(isinstance(run_id, int) and run_id > 0, 'full distribution/Pages acceptance run is required')
    fixture = 'releaseway/release-fixture'
    run = api_json(f'repos/{fixture}/actions/runs/{run_id}')
    require(run['id'] == run_id and run['event'] == 'workflow_dispatch', 'distribution run identity/event mismatch')
    successful_run(run, fixture, run['head_sha'].lower(), 'distribution-acceptance.yml')
    artifacts = list(pages(f'repos/{fixture}/actions/runs/{run_id}/artifacts', 'artifacts'))
    matching = [a for a in artifacts if a['name'] == f'distribution-evidence-{run_id}-{run["run_attempt"]}']
    require(len(matching) == 1, 'current distribution attempt artifact missing or ambiguous')
    artifact = matching[0]
    require(not artifact['expired'] and 0 < artifact['size_in_bytes'] <= 1024 * 1024, 'distribution artifact expired or too large')
    with zipfile.ZipFile(io.BytesIO(api_bytes(f'repos/{fixture}/actions/artifacts/{artifact["id"]}/zip'))) as bundle:
        files = [f for f in bundle.infolist() if not f.is_dir()]
        require(len(files) == 6 and len({f.filename for f in files}) == 6, 'six unique distribution receipts required')
        receipts = []
        for f in files:
            path = PurePosixPath(f.filename)
            require(not path.is_absolute() and '..' not in path.parts and '\\' not in f.filename and path.name == 'receipt.json' and f.file_size <= 100 * 1024, 'invalid distribution receipt member')
            receipts.append(json.loads(bundle.read(f)))
    verify_distribution_receipts(candidate, run['head_sha'], run_id, run['run_attempt'], receipts)
    return {'run_id': run_id, 'run_attempt': run['run_attempt'], 'artifact_id': artifact['id']}


def verify(repository, candidate, run_ids, distribution_run_id=None):
    require(repository in POLICIES, 'unsupported release repository')
    require(re.fullmatch(r'[0-9a-fA-F]{40}', candidate), 'candidate must be a full 40-character SHA')
    candidate = candidate.lower()
    ci_workflow, fixture, scenarios = POLICIES[repository]
    # A newer failure or in-progress rerun must not be hidden by an older success.
    ci = require_ci(repository, candidate, ci_workflow)
    require(ci['event'] == 'push', 'CI evidence must be a repository push run')
    require(len(run_ids) == len(scenarios) and len(set(run_ids)) == len(run_ids), 'supply one run per required acceptance scenario')
    seen = {}
    accepted = []
    for run_id in run_ids:
        require(isinstance(run_id, int) and run_id > 0, 'invalid acceptance run ID')
        run = api_json(f'repos/{fixture}/actions/runs/{run_id}')
        require(run['id'] == run_id and run['event'] == 'workflow_dispatch', 'acceptance run identity/event mismatch')
        artifacts = list(pages(f'repos/{fixture}/actions/runs/{run_id}/artifacts', 'artifacts'))
        matching = [artifact for artifact in artifacts if artifact['name'] == f'releaseway-acceptance-{run["run_attempt"]}']
        require(len(matching) == 1, 'current-attempt acceptance artifact missing or ambiguous')
        artifact = matching[0]
        require(not artifact['expired'] and artifact['size_in_bytes'] <= 1024 * 1024, 'acceptance artifact expired or too large')
        evidence = read_evidence(api_bytes(f'repos/{fixture}/actions/artifacts/{artifact["id"]}/zip'))
        scenario = evidence.get('scenario')
        require(scenario in scenarios and scenario not in seen, 'missing, unexpected or duplicate scenario')
        workflow, state = scenarios[scenario]
        successful_run(run, fixture, run['head_sha'].lower(), workflow)
        require(evidence.get('schema') == 1, 'unsupported evidence schema')
        require(evidence.get('candidate_repository') == repository and evidence.get('candidate_sha') == candidate, 'acceptance candidate mismatch')
        require(evidence.get('fixture_repository') == fixture and evidence.get('fixture_commit') == run['head_sha'], 'fixture commit mismatch')
        require(evidence.get('run_id') == run_id and evidence.get('run_attempt') == run['run_attempt'] and evidence.get('workflow') == workflow, 'acceptance attempt/workflow mismatch')
        require(evidence.get('state') == state, 'acceptance must verify fresh candidate execution')
        if repository == 'releaseway/npm-actions':
            integrity = evidence.get('integrity', '')
            require(re.fullmatch(r'sha512-[A-Za-z0-9+/]{86}==', integrity) and len(base64.b64decode(integrity[7:], validate=True)) == 64, 'planned npm integrity missing')
            require(evidence.get('version_source') in ('package-json', 'git-tag'), 'invalid version source')
            suffix = 'fixture' if scenario == 'npm-stage' else 'native'
            require(re.fullmatch(r'0\.0\.[1-9][0-9]*-' + suffix + r'\.[0-9]+', evidence.get('version', '')), 'invalid fixture version')
            if scenario == 'npm-direct':
                require(evidence.get('installed_node_versions') == ['22', '24', '26'], 'Node 22/24/26 consumer evidence missing')
        seen[scenario] = evidence
        accepted.append({'scenario': scenario, 'run_id': run_id, 'run_attempt': run['run_attempt'], 'artifact_id': artifact['id']})
    require(set(seen) == set(scenarios), 'required acceptance missing')
    if repository == 'releaseway/npm-actions':
        require(any(item['version_source'] == 'git-tag' for item in seen.values()), 'tag-derived prerelease acceptance missing')
    result = {'candidate_repository': repository, 'candidate_sha': candidate, 'ci_run_id': ci['id'], 'acceptance': accepted}
    if repository == 'releaseway/actions':
        result['distribution_ci_run_id'] = require_ci(repository, candidate, 'distribution.yml')['id']
        result['distribution'] = verify_distribution(candidate, distribution_run_id)
    else:
        require(distribution_run_id is None, 'distribution evidence applies only to actions releases')
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repository', required=True, choices=POLICIES)
    parser.add_argument('--candidate', required=True)
    parser.add_argument('--acceptance-runs', required=True, help='Comma-separated successful fixture workflow run IDs')
    parser.add_argument('--distribution-run', type=int, help='Full exact-candidate distribution/Pages fixture run; required for actions')
    args = parser.parse_args()
    try:
        result = verify(args.repository, args.candidate, [int(value.strip()) for value in args.acceptance_runs.split(',')], args.distribution_run)
        print(json.dumps(result, indent=2))
    except (ValueError, KeyError, TypeError, zipfile.BadZipFile, subprocess.TimeoutExpired) as error:
        parser.exit(1, f'Release readiness failed: {error}\n')
