"""Distribution receipt contract shared with release-fixture/verify-distribution-candidate.py."""
import hashlib
import json
import re
import unicodedata

def inventory(report):
    if report.get('schema') != 1 or not isinstance(report.get('files'), list) or not report['files']:
        raise ValueError('missing file inventory')
    entries = []
    paths = set()
    for f in report['files']:
        path = f['path']
        if not isinstance(path, str) or path.startswith('/') or '\\' in path or any(p in ('', '.', '..') for p in path.split('/')):
            raise ValueError('invalid inventory path')
        key = unicodedata.normalize('NFC', path).upper().lower()
        if key in paths or not isinstance(f['size'], int) or isinstance(f['size'], bool) or f['size'] < 0 or not re.fullmatch(r'[a-f0-9]{64}', f['sha256']):
            raise ValueError('invalid inventory identity')
        paths.add(key)
        entries.append([path, f['size'], f['sha256']])
    entries.sort(key=lambda e: e[0].encode('utf-8'))
    digest = 'sha256:' + hashlib.sha256(json.dumps(entries, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
    if report.get('digest') != digest:
        raise ValueError('content digest mismatch')
    return {e[0] for e in entries}


def verify_receipts(candidate, source, run_id, attempt, receipts, require_pages=True):
    if not re.fullmatch(r'[a-f0-9]{40}', source) or not str(run_id).isdigit() or not str(attempt).isdigit():
        raise ValueError('source/run/attempt identity is required')
    core = set()
    pages = set()
    for r in receipts:
        if r.get('schema') != 1 or (r.get('candidate'), r.get('sourceCommit'), str(r.get('runId')), str(r.get('attempt'))) != (candidate, source, str(run_id), str(attempt)):
            raise ValueError('receipt belongs to a different candidate/source/run/attempt')
        report = r['report']
        paths = inventory(report)
        if r.get('kind') == 'core':
            platform = r.get('platform')
            if platform not in ('Linux', 'macOS', 'Windows') or platform in core:
                raise ValueError('unexpected or duplicate native platform receipt')
            core.add(platform)
            if set(r.get('cases', [])) != {'public-release', 'cross-repo-read', 'negative-tamper', 'rerun', 'native-script'}:
                raise ValueError('missing public source case')
            src = report['source']
            if src.get('kind') != 'github-release' or src.get('commit') != source or src.get('tag') != f'distribution-{run_id}-{attempt}' or not src.get('assets') or not isinstance(src.get('releaseId'), int):
                raise ValueError('wrong public source snapshot')
            if not {'bootstrap.sh', 'diagnose.ps1', 'bootstrap.mjs'} <= paths:
                raise ValueError('missing public fixture files')
            if src.get('repository') != 'releaseway/release-fixture':
                raise ValueError('wrong fixture source repository')
            by_name = {a['name']: a for a in src['assets']}
            for f in report['files']:
                asset = by_name.get(f.get('from'))
                if not asset or asset['size'] != f['size'] or asset['digest'] != 'sha256:' + f['sha256']:
                    raise ValueError('file receipt does not match source asset')
            for a in src['assets']:
                if not isinstance(a.get('id'), int) or a['id'] < 1 or not re.fullmatch(r'sha256:[a-f0-9]{64}', a.get('digest', '')):
                    raise ValueError('invalid public asset identity')
        elif r.get('kind') == 'pages':
            mode = r.get('mode')
            if mode not in ('latest', 'release', 'artifact') or mode in pages:
                raise ValueError('unexpected or duplicate Pages source mode')
            pages.add(mode)
            if not str(r.get('deploymentId', '')).isdigit() or not str(r.get('reportArtifactId', '')).isdigit() or not r.get('pageUrl', '').startswith('https://') or r.get('served') is not True or r.get('deliveryDigest') != report['digest']:
                raise ValueError('missing exact deployment/report/served identity')
            if '.nojekyll' not in paths:
                raise ValueError('hidden delivery file missing')
            if mode != 'latest' and '.well-known/fixture.json' not in paths:
                raise ValueError('hidden fixture not served')
            src = report['source']
            if mode == 'artifact':
                if r.get('negative404') is not True or r.get('negativeDigest') is not True:
                    raise ValueError('public served failure probes missing')
                if src.get('kind') != 'workflow-artifact' or not str(r.get('sourceArtifactId', '')).isdigit() or str(src.get('artifactId')) != r['sourceArtifactId'] or str(src.get('runId')) != str(run_id) or src.get('callerSha') != source or not re.fullmatch(r'sha256:[a-f0-9]{64}', src.get('artifactDigest', '')):
                    raise ValueError('source artifact identity mismatch')
            elif src.get('kind') != 'github-release' or src.get('repository') != 'releaseway/release-fixture':
                raise ValueError('Pages Release source mismatch')
            elif mode == 'release' and (src.get('commit') != source or src.get('tag') != f'distribution-{run_id}-{attempt}'):
                raise ValueError('Pages exact source mismatch')
            elif mode == 'latest' and str(src.get('releaseId')) != report.get('latestId'):
                raise ValueError('latest source selection missing')
        else:
            raise ValueError('unexpected evidence kind')
    if core != {'Linux', 'macOS', 'Windows'} or (require_pages and pages != {'latest', 'release', 'artifact'}):
        raise ValueError('required native/Pages evidence is incomplete')
