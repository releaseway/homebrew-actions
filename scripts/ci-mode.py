"""CI scheduling hints; release verification recomputes all trust decisions."""
import json
import os
from pathlib import Path
import runpy
import subprocess

ROOT = Path(__file__).resolve().parent
R = runpy.run_path(str(ROOT / 'release-reuse.py'))


def choose(repository, candidate, root='.', force_full=False):
    if force_full:
        return dict(mode='full', reason='full verification explicitly requested')
    try:
        head = R['I']['git'](root, 'rev-parse', 'HEAD').decode().strip()
        R['B']['require'](head == candidate, 'candidate checkout differs')
        anchor = R['load_anchor'](repository)
        proof = R['I']['compare'](root, anchor['identity']['sha'], candidate)
        R['B']['require'](R['checker'](repository, candidate)['files'] == anchor['approval']['checker']['files'],
                         'verification implementation changed')
        return dict(mode='docs', reason='authenticated full approval and identical inputs',
                    baseline_tag=anchor['identity']['tag'], fingerprint=proof['fingerprint'])
    except (ValueError, KeyError, TypeError, OSError, subprocess.SubprocessError) as error:
        return dict(mode='full', reason='requires full: ' + str(error))


if __name__ == '__main__':
    result = choose(os.environ['GITHUB_REPOSITORY'], os.environ['GITHUB_SHA'],
                    force_full=os.environ.get('FORCE_FULL') == 'true')
    print(json.dumps(result))
    with open(os.environ['GITHUB_OUTPUT'], 'a') as output:
        output.write('mode=' + result['mode'] + '\n')
