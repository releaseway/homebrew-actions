"""Git-object evidence for conservative documentation-only release reuse."""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess

POLICY = 'releaseway-inputs-v1'
MAX_PROOF = 2 * 1024 * 1024
MAX_COMMITS = 256


def require(value, message):
    if not value:
        raise ValueError(message)


def canonical(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(',', ':')).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def allowed_document(path, mode):
    return mode == '100644' and (path in ('README.md', 'CONTRIBUTING.md', 'prepare/README.md', 'distribute/README.md')
        or (path.startswith('docs/') and path.endswith('.md')))


def git(root, *args):
    result = subprocess.run(['git', '-C', str(root), *args], capture_output=True, timeout=30)
    require(result.returncode == 0, 'Git objects or ancestry unavailable')
    require(len(result.stdout) <= MAX_PROOF, 'Git object exceeds proof budget')
    return result.stdout


def oid(kind, raw):
    return hashlib.sha1(kind.encode() + b' ' + str(len(raw)).encode() + b'\0' + raw).hexdigest()


def read_object(root, kind, sha):
    require(re.fullmatch('[0-9a-f]{40}', sha or ''), 'full Git SHA required')
    raw = git(root, 'cat-file', kind, sha)
    require(oid(kind, raw) == sha, 'Git object identity differs')
    return base64.b64encode(raw).decode()


def decode_object(kind, sha, encoded):
    require(re.fullmatch('[0-9a-f]{40}', sha or ''), 'invalid object identity')
    raw = base64.b64decode(encoded, validate=True)
    require(oid(kind, raw) == sha, 'Git object hash differs')
    return raw


def commit_tree(raw):
    require(raw.startswith(b'tree ') and re.fullmatch(b'[0-9a-f]{40}', raw[5:45])
            and raw[45:46] == b'\n', 'invalid Git commit tree')
    return raw[5:45].decode()


def snapshot(root, sha):
    commit = read_object(root, 'commit', sha)
    tree = commit_tree(base64.b64decode(commit))
    trees = {}

    def visit(tree_id):
        if tree_id in trees:
            return
        encoded = read_object(root, 'tree', tree_id)
        trees[tree_id] = encoded
        for mode, name, child in tree_entries(base64.b64decode(encoded)):
            if mode == '40000':
                visit(child)
    visit(tree)
    return dict(commit=sha, object=commit, trees=trees)


def tree_entries(raw):
    entries = []
    offset = 0
    names = set()
    while offset < len(raw):
        space = raw.index(b' ', offset)
        end = raw.index(b'\0', space)
        mode = raw[offset:space].decode('ascii')
        name = raw[space + 1:end].decode('utf8')
        require(name and name not in ('.', '..', '.git') and '/' not in name and name not in names,
                'invalid or duplicate tree path')
        require(mode in ('40000', '100644', '100755'), 'unsupported Git entry type requires full validation')
        require(end + 21 <= len(raw), 'incomplete tree entry')
        child = raw[end + 1:end + 21].hex()
        entries.append((mode, name, child))
        names.add(name)
        offset = end + 21
    return entries


def inventory(proof):
    raw = decode_object('commit', proof['commit'], proof['object'])
    tree = commit_tree(raw)
    seen = set()
    entries = []

    def walk(tree_id, prefix, ancestors):
        require(tree_id not in ancestors, 'recursive Git tree')
        require(tree_id in proof['trees'], 'missing Git tree object')
        raw_tree = decode_object('tree', tree_id, proof['trees'][tree_id])
        seen.add(tree_id)
        for mode, name, child in tree_entries(raw_tree):
            path = prefix + name
            if mode == '40000':
                walk(child, path + '/', ancestors | {tree_id})
            else:
                entries.append([path, mode, 'blob', child])
    walk(tree, '', set())
    require(seen == set(proof['trees']), 'unexpected Git tree object')
    return sorted(entries, key=lambda item: item[0].encode())


def manifest(proof):
    return [entry for entry in inventory(proof) if not allowed_document(entry[0], entry[1])]


def verify(proof, baseline, candidate):
    require(proof['schema'] == 1 and type(proof['schema']) is int and proof['policy'] == POLICY, 'input policy differs')
    require(len(canonical(proof)) <= MAX_PROOF, 'input proof exceeds limit')
    require(proof['baseline']['commit'] == baseline and proof['candidate']['commit'] == candidate, 'input commit differs')
    left, right = manifest(proof['baseline']), manifest(proof['candidate'])
    require(left == right, 'execution or verification input changed')
    require(proof['fingerprint'] == digest(dict(policy=POLICY, entries=left)), 'input fingerprint differs')
    # Verify a real parent path; a manifest alone cannot establish ancestry.
    commits = proof['ancestry']
    require(len(commits) <= MAX_COMMITS and candidate in commits, 'ancestry proof incomplete')
    pending, visited = [candidate], set()
    while pending:
        current = pending.pop()
        if current == baseline:
            return proof['fingerprint']
        if current in visited or current not in commits:
            continue
        visited.add(current)
        raw = decode_object('commit', current, commits[current])
        header = raw.split(b'\n\n', 1)[0]
        pending.extend(line[7:].decode() for line in header.splitlines() if re.fullmatch(b'parent [0-9a-f]{40}', line))
    raise ValueError('baseline is not a proven ancestor')


def compare(root, baseline, candidate):
    require(re.fullmatch('[0-9a-f]{40}', baseline or '') and re.fullmatch('[0-9a-f]{40}', candidate or ''), 'full commit SHAs required')
    git(root, 'merge-base', '--is-ancestor', baseline, candidate)
    left, right = snapshot(root, baseline), snapshot(root, candidate)
    entries = manifest(left)
    require(entries == manifest(right), 'execution or verification input changed')
    commits = git(root, 'rev-list', '--ancestry-path', baseline + '..' + candidate).decode().splitlines()
    require(len(commits) < MAX_COMMITS, 'ancestry exceeds proof budget')
    ancestry = {sha: read_object(root, 'commit', sha) for sha in set(commits + [candidate])}
    proof = dict(schema=1, policy=POLICY, baseline=left, candidate=right, ancestry=ancestry,
                 fingerprint=digest(dict(policy=POLICY, entries=entries)))
    verify(proof, baseline, candidate)
    return proof


def classify(root, baseline, candidate):
    try:
        proof = compare(root, baseline, candidate)
        return dict(mode='docs', reason='identical execution and verification inputs', proof=proof)
    except (ValueError, KeyError, TypeError, UnicodeError, OSError, subprocess.SubprocessError) as error:
        return dict(mode='full', reason=str(error))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline')
    parser.add_argument('--candidate', default=os.environ.get('GITHUB_SHA'))
    parser.add_argument('--root', default='.')
    parser.add_argument('--proof')
    args = parser.parse_args()
    result = classify(args.root, args.baseline, args.candidate)
    if args.proof and result['mode'] == 'docs':
        Path(args.proof).write_bytes(canonical(result['proof']))
    summary = {key: value for key, value in result.items() if key != 'proof'}
    print(json.dumps(summary))
