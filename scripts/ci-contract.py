"""Required CI roles for new approvals; independent of unrelated workflow runs."""
import re

CONTRACT = 'release-ci-v1'
WORKFLOWS = {'releaseway/actions': 'test.yml', 'releaseway/npm-actions': 'check.yml',
             'releaseway/homebrew-actions': 'test.yml'}
RUNNERS = ['ubuntu-24.04', 'ubuntu-24.04-arm', 'macos-15', 'macos-15-intel', 'windows-2025', 'windows-11-arm']
NODES = ['22', '24', '26']


def required_jobs(repository, mode):
    if mode == 'docs':
        return {'docs', 'validation (docs)'}
    if mode != 'full':
        raise ValueError('unsupported CI coverage')
    names = {'docs', 'validation (full)'}
    if repository == 'releaseway/npm-actions':
        names |= {'static', 'unit', 'pack-integration', 'coverage (full)'}
        names |= {f'native ({runner}, {node})' for runner in RUNNERS for node in NODES}
        names |= {f'musl ({node})' for node in NODES}
    elif repository == 'releaseway/actions':
        names |= {'static', 'test (ubuntu-24.04)', 'test (macos-15)'}
    elif repository == 'releaseway/homebrew-actions':
        names |= {'evidence', 'actionlint', 'regression (ubuntu-24.04)', 'regression (macos-15)'}
    else:
        raise ValueError('unsupported CI repository')
    return names


def verify(evidence, repository, candidate, mode):
    run = evidence['run']
    if (evidence['contract'] != CONTRACT or evidence['mode'] != mode
            or run['repository']['full_name'] != repository or run['head_sha'] != candidate
            or run['path'] != '.github/workflows/' + WORKFLOWS[repository]
            or run['event'] != 'push' or run['status'] != 'completed' or run['conclusion'] != 'success'
            or type(run['id']) is not int or run['id'] <= 0
            or type(run['run_attempt']) is not int or run['run_attempt'] <= 0):
        raise ValueError('required CI identity, role or completion differs')
    jobs = evidence['jobs']
    for name in required_jobs(repository, mode):
        selected = [job for job in jobs if job['name'] == name]
        if len(selected) != 1 or selected[0]['status'] != 'completed' or selected[0]['conclusion'] != 'success':
            raise ValueError('required CI job missing or unsuccessful: ' + name)
    return run['id']
