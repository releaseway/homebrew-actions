"""Required CI roles for new approvals; independent of unrelated workflow runs."""
import re

CONTRACT = 'release-ci-v1'
WORKFLOWS = {'releaseway/actions': 'test.yml', 'releaseway/npm-actions': 'check.yml',
             'releaseway/homebrew-actions': 'test.yml'}
RUNNERS = ['ubuntu-24.04', 'ubuntu-24.04-arm', 'macos-15', 'macos-15-intel', 'windows-2025', 'windows-11-arm']
NODES = ['22', '24', '26']


def required_jobs(repository, mode, workflow=None):
    if workflow == 'distribution.yml' and repository == 'releaseway/actions':
        names = {'docs', 'distribution-validation'}
        if mode == 'full':
            names |= {f'{role} ({runner})' for role in ('local', 'fixture-execution')
                      for runner in ('ubuntu-24.04', 'macos-15', 'windows-2025')}
        elif mode != 'docs':
            raise ValueError('unsupported distribution coverage')
        return names
    if mode == 'docs':
        return {'docs', 'validation'}
    if mode != 'full':
        raise ValueError('unsupported CI coverage')
    names = {'docs', 'validation'}
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


def verify(evidence, repository, candidate, mode, workflow=None):
    expected_workflow = workflow or WORKFLOWS[repository]
    if expected_workflow != WORKFLOWS[repository] and not (repository == 'releaseway/actions' and expected_workflow == 'distribution.yml'):
        raise ValueError('unsupported CI workflow')
    run = evidence['run']
    if (evidence['contract'] != CONTRACT or evidence['mode'] != mode
            or run['repository']['full_name'] != repository or run['head_sha'] != candidate
            or run['path'] != '.github/workflows/' + expected_workflow
            or run['event'] != 'push' or run['status'] != 'completed' or run['conclusion'] != 'success'
            or type(run['id']) is not int or run['id'] <= 0
            or type(run['run_attempt']) is not int or run['run_attempt'] <= 0):
        raise ValueError('required CI identity, role or completion differs')
    jobs = evidence['jobs']
    if mode == 'docs':
        full_names = required_jobs(repository, 'full', workflow) - {'docs', 'validation', 'distribution-validation'}
        if any(job['name'] in full_names and job['conclusion'] != 'skipped' for job in jobs):
            raise ValueError('CI role does not match documentation-only execution')
    for name in required_jobs(repository, mode, workflow):
        selected = [job for job in jobs if job['name'] == name]
        if len(selected) != 1 or selected[0]['status'] != 'completed' or selected[0]['conclusion'] != 'success':
            raise ValueError('required CI job missing or unsuccessful: ' + name)
    return run['id']


def collect(repository, candidate, mode=None):
    from pathlib import Path
    import runpy
    module = runpy.run_path(str(Path(__file__).with_name('verify-release-evidence.py')))
    run = module['require_ci'](repository, candidate, WORKFLOWS[repository])
    # Lightweight tag workflows have their own filename and cannot mask this run.
    jobs = list(module['pages'](f'repos/{repository}/actions/runs/{run["id"]}/attempts/{run["run_attempt"]}/jobs', 'jobs'))
    if mode is None:
        full_role = 'evidence' if repository == 'releaseway/homebrew-actions' else 'static'
        mode = 'full' if any(job['name'] == full_role and job['conclusion'] == 'success' for job in jobs) else 'docs'
    evidence = dict(contract=CONTRACT, mode=mode, run=module['BUNDLE']['run_snapshot'](run),
                    jobs=module['BUNDLE']['snapshot']({'jobs': jobs})['jobs'])
    verify(evidence, repository, candidate, mode)
    current = module['api_json'](f'repos/{repository}/actions/runs/{run["id"]}')
    latest = module['require_ci'](repository, candidate, WORKFLOWS[repository])
    if current['run_attempt'] != run['run_attempt'] or current['status'] != 'completed' or current['conclusion'] != 'success' or latest['id'] != run['id']:
        raise ValueError('CI changed during verification')
    return evidence
