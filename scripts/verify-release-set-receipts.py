"""Semantic checks for the isolated release-set acceptance contract (stdlib only)."""
import hashlib
import json
import re
import io
import zipfile

CASES = {'three-os-core', 'docs-merge', 'version-url-retained', 'alias-advanced',
         'empty-skipped', 'partial-rejected', 'digest-tamper-rejected',
         'rerun-same-digest', 'schema1-regression', 'mixed-provenance', 'source-change-rejected'}
CORE_CASES = {'partial-rejected', 'digest-tamper-rejected', 'rerun-same-digest',
              'schema1-regression', 'source-change-rejected'}
REPOSITORY = 'releaseway/release-fixture'
PAGE_URL = 'https://releaseway.github.io/release-fixture/'


def require(condition, message):
    if not condition:
        raise ValueError(message)


def digest(raw):
    return 'sha256:' + hashlib.sha256(raw).hexdigest()


def compact(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode()


def identity(value):
    require(not isinstance(value, bool) and re.fullmatch(r'[1-9]\d*', str(value)) is not None,
            'invalid provider identity')
    return str(value)


def sha(value):
    require(isinstance(value, str) and re.fullmatch(r'sha256:[a-f0-9]{64}', value), 'invalid SHA-256')


def exact_cases(actual, expected):
    require(isinstance(actual, list) and len(actual) == len(expected) and set(actual) == expected,
            'missing, duplicate or unknown acceptance case')


def payload(version):
    return {'bootstrap.sh': f"#!/bin/sh\nprintf '%s\\n' 'release-set:{version}'\n",
            'diagnose.ps1': f"Write-Output 'release-set:{version}'\n",
            'metadata.json': json.dumps({'version': version}, separators=(',', ':')) + '\n'}


def documentation(phase):
    return {'index.html': f'<!doctype html><title>Release set {phase}</title>\n',
            '.well-known/release-set.json': json.dumps({'phase': phase}, separators=(',', ':')) + '\n'}


def inventory(files):
    require(isinstance(files, list) and 0 < len(files) < 100, 'invalid fixture inventory')
    result = {}
    for f in files:
        path = f.get('path')
        require(isinstance(path, str) and not path.startswith('/') and '\\' not in path
                and all(p not in ('', '.', '..') for p in path.split('/')) and path not in result,
                'invalid or duplicate path')
        require(type(f.get('size')) is int and f['size'] >= 0, 'invalid file size')
        sha('sha256:' + f.get('sha256', ''))
        result[path] = f
    return result


def content_digest(files):
    return digest(compact(sorted([[f['path'], f['size'], f['sha256']] for f in files],
                                 key=lambda e: e[0].encode())))


def expected_policy(prefix, phase):
    return dict(schema=2, releases={'tag-prefix': prefix,
                'range': '>=0.9.0 <1.2.0 || =1.3.0-beta.1' if phase == 'initial' else '>=0.9.0 <2.0.0',
                'include-prereleases': True, 'on-empty': 'skip'},
                files=[{'from': 'bootstrap.sh', 'to': 'install.sh', 'required': True},
                       {'from': 'diagnose.ps1', 'to': 'install.ps1', 'required': True},
                       {'from': 'metadata.json', 'to': 'metadata.json', 'required': False}],
                aliases=dict(tag=prefix + ('1.1.0' if phase == 'initial' else '1.2.0'),
                             files=[{'from': 'install.sh', 'to': 'install.sh'},
                                    {'from': 'install.ps1', 'to': 'install.ps1'}]))


def verify_report(report, source_commit, run_id, prefix, phase, pages=False):
    require(type(report.get('schema')) is int and report['schema'] == 2 and report.get('base') is True, 'missing mixed release-set report')
    files = inventory(report.get('files'))
    require(report.get('digest') == content_digest(report['files']), 'inventory digest mismatch')
    src = report.get('source', {})
    expected = expected_policy(prefix, phase)
    require(src.get('kind') == 'github-release-set' and src.get('repository') == REPOSITORY
            and src.get('verification') == 'digest' and src.get('destination') == '', 'wrong release source')
    require(src.get('policy') == expected and src.get('policyDigest') == digest(compact(expected)),
            'wrong policy identity')
    require(src.get('aliasTag') == expected['aliases']['tag'], 'wrong alias selection')
    versions = {'0.9.0', '1.0.0', '1.1.0', '1.3.0-beta.1'} | ({'1.2.0'} if phase == 'final' else set())
    releases = src.get('releases')
    require(isinstance(releases, list) and len(releases) == len(versions)
            and {r.get('tag') for r in releases} == {prefix + v for v in versions}, 'wrong selected release membership')
    release_ids, asset_ids, expected_files = set(), set(), {}
    for r in releases:
        require(type(r.get('releaseId')) is int, 'release ID must be an integer')
        rid = identity(r.get('releaseId'))
        require(rid not in release_ids, 'duplicate release identity'); release_ids.add(rid)
        require(r.get('commit') == source_commit and r.get('prerelease') is True
                and type(r.get('immutable')) is bool, 'wrong release snapshot')
        version = r['tag'][len(prefix):]
        if version == '0.9.0':
            require(r.get('status') == 'skipped-empty' and r.get('assets') == [], 'empty release not skipped')
            continue
        require(r.get('status') == 'selected', 'complete release was skipped')
        data = payload(version)
        require(isinstance(r.get('assets'), list) and len(r['assets']) == 3
                and {a.get('name') for a in r['assets']} == set(data), 'missing fixture assets')
        for a in r['assets']:
            require(type(a.get('id')) is int, 'asset ID must be an integer')
            aid = identity(a.get('id'))
            require(aid not in asset_ids, 'duplicate asset identity'); asset_ids.add(aid)
            raw = data[a['name']].encode()
            require(type(a.get('size')) is int and a['size'] == len(raw)
                    and a.get('digest') == digest(raw) and a.get('state') == 'uploaded', 'asset bytes mismatch')
            to = next(f['to'] for f in expected['files'] if f['from'] == a['name'])
            path = r['tag'] + '/' + to
            f = dict(path=path, size=len(raw), sha256=digest(raw)[7:], origin='selected',
                     releaseId=r['releaseId'], assetId=a['id'], **{'from': a['name']})
            expected_files[path] = f
            if r['tag'] == src['aliasTag'] and to != 'metadata.json':
                expected_files[to] = dict(f, path=to, aliasOf=path)
    for path, text in documentation(phase).items():
        raw = text.encode(); expected_files[path] = dict(path=path, size=len(raw), sha256=digest(raw)[7:], origin='base')
    baseline = report.get('baseline', {})
    if pages:
        require(baseline.get('kind') == 'workflow-artifact' and baseline.get('repository') == REPOSITORY
                and identity(baseline.get('runId')) == str(run_id)
                and baseline.get('headSha') == source_commit and baseline.get('callerSha') == source_commit,
                'wrong documentation provenance')
        identity(baseline.get('artifactId')); sha(baseline.get('artifactDigest'))
        require(report.get('contentDigest') == content_digest(list(expected_files.values())), 'wrong pre-delivery digest')
        expected_files['.nojekyll'] = dict(path='.nojekyll', size=0, sha256=digest(b'')[7:], origin='selected', generatedBy='pages')
    else:
        require(baseline == {'kind': 'local-site'}, 'wrong local baseline')
    require(files == expected_files, 'file bytes or provenance differ from fixture contract')
    return files


def verify_receipt(candidate, source, run_id, attempt, receipt):
    require(re.fullmatch(r'[a-f0-9]{40}', candidate or '') and re.fullmatch(r'[a-f0-9]{40}', source or ''), 'invalid commit identity')
    identity(run_id); identity(attempt)
    require(type(receipt.get('schema')) is int and receipt['schema'] == 1 and receipt.get('kind') == 'release-set', 'wrong acceptance type')
    def same(r):
        require((r.get('candidate'), r.get('sourceCommit'), identity(r.get('runId')), identity(r.get('attempt')))
                == (candidate, source, str(run_id), str(attempt)), 'different candidate/source/run/attempt')
    same(receipt); exact_cases(receipt.get('cases'), CASES)
    prefix = receipt.get('prefix', '')
    require(re.fullmatch(r'distribution-set-\d{8}-[a-f0-9]{6,16}-v', prefix), 'wrong campaign prefix')
    core, pages = receipt.get('core'), receipt.get('pages')
    require(isinstance(core, list) and len(core) == 3 and {r.get('platform') for r in core} == {'Linux', 'macOS', 'Windows'}, 'incomplete native platforms')
    for r in core:
        same(r); require(type(r.get('schema')) is int and r['schema'] == 1 and r.get('kind') == 'release-set-core', 'wrong native receipt type')
        exact_cases(r.get('cases'), CORE_CASES)
        verify_report(r['report'], source, run_id, prefix, 'initial')
        require(r.get('rerunDigest') == r['report']['digest'] and r.get('nativeMarker') == 'release-set:1.1.0', 'rerun or native execution proof missing')
    require(isinstance(pages, list) and len(pages) == 2 and {r.get('phase') for r in pages} == {'initial', 'final'}, 'missing two Pages deployments')
    by_phase = {}
    for r in pages:
        same(r); require(type(r.get('schema')) is int and r['schema'] == 1 and r.get('kind') == 'release-set-pages', 'wrong Pages receipt type')
        verify_report(r['report'], source, run_id, prefix, r['phase'], pages=True)
        identity(r.get('deploymentId')); identity(r.get('reportArtifactId')); sha(r.get('reportArtifactDigest')); sha(r.get('reportSha'))
        require(r.get('served') is True and r.get('pageUrl') == PAGE_URL and r.get('deliveryDigest') == r['report']['digest'], 'missing exact served delivery identity')
        require(str(r.get('siteArtifactId')) == r['report']['baseline']['artifactId']
                and r.get('siteArtifactDigest') == r['report']['baseline']['artifactDigest'], 'base artifact identity mismatch')
        by_phase[r['phase']] = r
    a, b = by_phase['initial'], by_phase['final']
    for key in ('deploymentId', 'reportArtifactId', 'siteArtifactId'):
        require(str(a[key]) != str(b[key]), 'deployment or fresh artifact reused across phases')
    require(a['siteArtifactDigest'] != b['siteArtifactDigest'], 'fresh documentation archive missing')
    require(all(r['report']['digest'] == a['report']['contentDigest'] for r in core), 'native/Pages preparation differs')
    old = inventory(a['report']['files']); new = inventory(b['report']['files'])
    require(all(new.get(p) == f for p, f in old.items() if p.startswith(prefix)), 'old version URL or provenance changed')
    require(old['install.sh']['sha256'] != new['install.sh']['sha256'], 'alias did not advance')


def read_archive(raw, expected):
    require(0 < len(raw) <= 1024**2, 'release-set ZIP exceeds limit')
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        members = z.infolist()
        require(len(members) == len(expected) and {m.filename for m in members} == set(expected),
                'unexpected or duplicate release-set ZIP members')
        require(all(not m.is_dir() and m.file_size <= 100 * 1024
                    and (m.external_attr >> 16 & 0o170000) != 0o120000 for m in members), 'invalid release-set ZIP member')
        return {m.filename: z.read(m) for m in members}


def verify_provider(receipt, candidate, run, artifacts, api_json, api_bytes, pages, bundle):
    """Bind aggregate claims to original report/docs ZIPs and exact deployment jobs."""
    verify_receipt(candidate, run['head_sha'], run['id'], run['run_attempt'], receipt)
    repo, run_id, attempt = REPOSITORY, run['id'], run['run_attempt']
    jobs = list(pages(f'repos/{repo}/actions/runs/{run_id}/attempts/{attempt}/jobs', 'jobs'))
    for runner in ('ubuntu-24.04', 'macos-15', 'windows-2025'):
        matching = [j for j in jobs if j.get('name') == f'public-source ({runner})']
        require(len(matching) == 1 and matching[0].get('conclusion') == 'success'
                and runner in matching[0].get('labels', []), 'native job/provider platform mismatch')
    def original(artifact_id, digest_value, name_check, members):
        matching = [a for a in artifacts if str(a.get('id')) == str(artifact_id)]
        require(len(matching) == 1, 'original artifact identity missing or ambiguous')
        a = matching[0]
        require(type(a.get('id')) is int and a['id'] > 0 and a.get('expired') is False
                and type(a.get('size_in_bytes')) is int and 0 < a['size_in_bytes'] <= 1024**2
                and name_check(a.get('name', '')) and a.get('digest') == digest_value,
                'original artifact metadata mismatch')
        workflow = a.get('workflow_run', {})
        require(workflow.get('id') == run_id and workflow.get('head_sha') == run['head_sha'],
                'original artifact belongs to another run/source')
        raw = api_bytes(f'repos/{repo}/actions/artifacts/{a["id"]}/zip')
        require(digest(raw) == digest_value, 'original artifact provider digest mismatch')
        return read_archive(raw, members)
    for r in receipt['pages']:
        report = original(r['reportArtifactId'], r['reportArtifactDigest'],
                          lambda n: re.fullmatch(f'releaseway-pages-{run_id}-{attempt}-[a-f0-9-]{{36}}-report', n), {'report.json'})['report.json']
        require(digest(report) == r['reportSha'] and bundle['parse_json'](report) == r['report'],
                'aggregate differs from original report bytes')
        docs = original(r['siteArtifactId'], r['siteArtifactDigest'],
                        lambda n: n == f'release-set-docs-{r["phase"]}-{run_id}-{attempt}', documentation(r['phase']))
        require(docs == {p: text.encode() for p, text in documentation(r['phase']).items()}, 'documentation bytes mismatch')
        deployment = api_json(f'repos/{repo}/deployments/{r["deploymentId"]}')
        require(identity(deployment.get('id')) == r['deploymentId'] and deployment.get('sha') == run['head_sha']
                and deployment.get('environment') == 'github-pages' and deployment.get('task') == 'deploy',
                'deployment source/environment mismatch')
        matching = [j for j in jobs if j.get('name') == f'release-set-{r["phase"]} / deploy']
        require(len(matching) == 1 and matching[0].get('conclusion') == 'success', 'exact Pages deployment job missing')
        log_url = f'https://github.com/{repo}/actions/runs/{run_id}/job/{matching[0]["id"]}'
        statuses = api_json(f'repos/{repo}/deployments/{r["deploymentId"]}/statuses?per_page=100')
        require(isinstance(statuses, list) and any(s.get('state') == 'success' and s.get('log_url') == log_url
                and s.get('environment_url') == PAGE_URL for s in statuses), 'deployment not successful in the exact run/job')
