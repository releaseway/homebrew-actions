"""Aggregate the selected CI path without skipped required checks."""
import json
import os


def verify(mode, needs):
    if mode not in ('docs', 'full'):
        raise ValueError('CI mode missing or unsupported')
    required = {'inputs', 'docs'} if mode == 'docs' else set(needs)
    if not required <= set(needs) or any(needs[name]['result'] != 'success' for name in required):
        raise ValueError('Required CI work is incomplete or unsuccessful')
    if any(value['result'] in ('failure', 'cancelled') for value in needs.values()):
        raise ValueError('A CI job failed or was cancelled')


if __name__ == '__main__':
    verify(os.environ['CI_MODE'], json.loads(os.environ['CI_NEEDS']))
    print('Selected CI path completed')
