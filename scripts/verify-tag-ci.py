"""Validate the exact source tag against already completed branch CI."""
import os
from pathlib import Path
import runpy

if __name__ == '__main__':
    root = Path(__file__).resolve().parent
    inputs = runpy.run_path(str(root / 'release-inputs.py'))
    candidate = os.environ['GITHUB_SHA']
    inputs['require'](inputs['git']('.', 'rev-parse', 'HEAD').decode().strip() == candidate, 'tag checkout differs')
    ci = runpy.run_path(str(root / 'ci-contract.py'))
    evidence = ci['collect'](os.environ['GITHUB_REPOSITORY'], candidate)
    print('Tag identity verified using CI ' + str(evidence['run']['id']) + ' (' + evidence['mode'] + ')')
