"""Authenticate a durable release approval asset, then check its offline consistency."""
import argparse
import json
import os
from pathlib import Path
import re
import runpy
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parent
B = runpy.run_path(str(ROOT / 'evidence-bundle.py'))


def gh(args, missing=False):
    result = subprocess.run(['gh'] + args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
    if missing and result.returncode and b'HTTP 404' in result.stderr:
        return None
    B['require'](result.returncode == 0, 'GitHub archive lookup/authentication failed')
    return result.stdout


def release(repository, tag, missing=False):
    raw = gh(['api', f'repos/{repository}/releases/tags/{tag}'], missing)
    return None if raw is None else B['parse_json'](raw)


def tag_commit(repository, tag):
    ref = B['parse_json'](gh(['api', f'repos/{repository}/git/ref/tags/{tag}']))['object']
    for _ in range(5):
        if ref['type'] == 'commit':
            return ref['sha']
        B['require'](ref['type'] == 'tag', 'unsupported tag target')
        ref = B['parse_json'](gh(['api', f'repos/{repository}/git/tags/{ref["sha"]}']))['object']
    raise ValueError('tag nesting exceeds limit')


def authenticate(repository, tag, candidate=None, output=None):
    B['require'](repository in B['policy_data']('release-v1')['repositories'], 'unsupported repository')
    B['require'](re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*', tag or ''), 'invalid tag')
    legacy = B['policy_data']('legacy-20261020')['releases'][repository]
    sidecar = tag == legacy['tag']
    archive_tag = 'evidence-' + tag if sidecar else tag
    observed = release(repository, archive_tag)
    B['require'](observed['immutable'] is True and observed['draft'] is False, 'archive release must be published and immutable')
    if sidecar:
        B['require'](observed['prerelease'] is True, 'sidecar must be a prerelease')
    assets = [asset for asset in observed['assets'] if asset['name'] == 'releaseway-evidence.json']
    B['require'](len(assets) == 1 and (not sidecar or len(observed['assets']) == 1), 'archive asset missing or ambiguous')
    asset = assets[0]
    B['require'](type(asset['id']) is int and 0 < asset['size'] <= B['MAX_BUNDLE'], 'invalid archive asset size/identity')
    expected_candidate = legacy['candidate'] if sidecar else tag_commit(repository, tag)
    B['require'](candidate is None or candidate == expected_candidate, 'candidate tag mismatch')
    with tempfile.TemporaryDirectory(prefix='releaseway-archive-') as directory:
        gh(['release', 'download', archive_tag, '--repo', repository, '--pattern', 'releaseway-evidence.json', '--dir', directory])
        path = Path(directory) / 'releaseway-evidence.json'
        B['require'](path.stat().st_size <= B['MAX_BUNDLE'], 'downloaded archive exceeds limit')
        raw = path.read_bytes()
        B['require'](asset['digest'] == 'sha256:' + B['sha256'](raw), 'release asset digest mismatch')
        gh(['release', 'verify-asset', archive_tag, str(path), '--repo', repository])
        bundle = B['parse_json'](raw)
        B['verify_bundle'](bundle, repository, tag, expected_candidate)
        if sidecar:
            B['require'](bundle['policy']['id'] == 'legacy-20261020', 'sidecar policy mismatch')
            B['require'](bundle['checker']['commit'] == tag_commit(repository, archive_tag), 'collector tag mismatch')
        else:
            B['require'](bundle['policy']['id'] != 'legacy-20261020', 'legacy policy cannot approve a fresh release')
        if output is not None:
            B['atomic_write'](output, raw)
    return {'state': 'archive_authenticated', 'repository': repository, 'tag': tag,
            'archive_tag': archive_tag, 'candidate': expected_candidate, 'sha256': B['sha256'](raw),
            'asset_id': asset['id'], 'release_url': observed['html_url']}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repository', required=True)
    parser.add_argument('--tag', required=True)
    parser.add_argument('--candidate')
    parser.add_argument('--output')
    args = parser.parse_args()
    try:
        print(json.dumps(authenticate(args.repository, args.tag, args.candidate, args.output), indent=2))
    except (ValueError, KeyError, TypeError, OSError, subprocess.TimeoutExpired) as error:
        parser.exit(2 if isinstance(error, OSError) or 'unsupported' in str(error) else 1,
                    'Archive verification failed: ' + str(error) + '\n')
