# Develop homebrew-actions

Use Python 3, Bash, Git and Ruby. The [CI workflow](.github/workflows/test.yml)
defines the Ruby toolchain and runner matrix. Consumer repositories call the
[public workflows](README.md) without cloning this repository.

## Validate a change

Run deterministic regressions from the repository root:

```sh
python3 test/formula-generator.py
python3 test/workflow-contracts.py
python3 test/publish-scripts.py
python3 test/formula-validation.py
python3 test/setup-deploy-key.py
python3 test/tap-maintenance.py
python3 test/release-evidence.py
python3 test/evidence-bundle.py
python3 test/release-set-evidence.py
python3 test/homebrew-feature-evidence.py
python3 test/platform-contracts.py
python3 test/validation-evidence.py
python3 test/release-inputs.py
python3 test/release-reuse.py
python3 test/ci-contract.py
python3 test/release-preparation.py
git diff --check
```

`platform-contracts.py` checks generated models without Homebrew by default.
Set `RELEASEWAY_BREW_CONTRACTS=1` on a host with Homebrew to exercise its actual
Formula loader. A passing model check does not establish native installation.
CI runs the regression suite on Linux and macOS, enables loader coverage on macOS,
checks shell/Ruby syntax and lints the reusable workflows.

## Preserve implementation ownership

The Formula action validates inputs in its shell entrypoint and loads
`internal/formula/render.rb` from the pinned automation checkout. Workflow pins
also pin that renderer and the internal scripts. Preserve generated Formula bytes
and public workflow outputs when changing their implementation.

## Approve an automation release

Full automation releases require the newest successful branch CI at the tag's SHA and
matching public Homebrew `acceptance-runs`. Both the legacy public and
revision/platform campaigns are required; the gate preserves a durable approval
asset. See
[archive verification](https://github.com/releaseway/actions/blob/main/docs/release-evidence.md)
and the [candidate guide](https://github.com/releaseway/release-fixture#candidate-release-readiness)
for literal candidate pins, fixture taps, retention and retries. Local tests do not
replace those public native acceptance runs.

Documentation-only releases can reuse an authenticated full approval when all
other Git inputs match. They still check current documentation, source identity
and the new Release asset. Tags perform a lightweight branch-CI identity check.
The archive guide explains the initial full approval, revocation and forced full
checks. Source, Formula and native validation policies remain unchanged.

Read the publication summary to distinguish the reused approval from this candidate:
`original SHA` and `original verification` identify the full campaign, while
`current CI` identifies this candidate's checks. `verification` reports whether the
release used a fresh full approval or an equivalent approval.
