"""Read-only release readiness check. Kept identical in the three action repositories."""
import argparse
import base64
import io
import json
import re
import subprocess
import zipfile


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


def verify(repository, candidate, run_ids):
    require(repository in POLICIES, 'unsupported release repository')
    require(re.fullmatch(r'[0-9a-fA-F]{40}', candidate), 'candidate must be a full 40-character SHA')
    candidate = candidate.lower()
    ci_workflow, fixture, scenarios = POLICIES[repository]
    runs = [run for run in pages(f'repos/{repository}/actions/workflows/{ci_workflow}/runs?head_sha={candidate}&event=push', 'workflow_runs') if run.get('event') == 'push']
    require(runs, 'required exact-SHA CI run missing')
    # A newer failure or in-progress rerun must not be hidden by an older success.
    ci = max(runs, key=lambda run: run['id'])
    successful_run(ci, repository, candidate, ci_workflow)
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
    return {'candidate_repository': repository, 'candidate_sha': candidate, 'ci_run_id': ci['id'], 'acceptance': accepted}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repository', required=True, choices=POLICIES)
    parser.add_argument('--candidate', required=True)
    parser.add_argument('--acceptance-runs', required=True, help='Comma-separated successful fixture workflow run IDs')
    args = parser.parse_args()
    try:
        result = verify(args.repository, args.candidate, [int(value.strip()) for value in args.acceptance_runs.split(',')])
        print(json.dumps(result, indent=2))
    except (ValueError, KeyError, TypeError, zipfile.BadZipFile, subprocess.TimeoutExpired) as error:
        parser.exit(1, f'Release readiness failed: {error}\n')
