# homebrew-actions

Generate, validate, and publish a Homebrew Formula from a product repository.

`homebrew-actions` owns Formula rendering, Homebrew validation, and publishing to a
destination tap. The product repository owns its build and GitHub Release. The tap
repository owns published `Formula/*.rb` state.

<a id="version"></a>

## Choose a release

Select a release from [Releases](https://github.com/releaseway/homebrew-actions/releases)
and pin all reusable workflows to its full commit SHA. Replace
`<homebrew-actions-sha>` before saving the examples. See
[reference selection](https://github.com/releaseway/actions/blob/main/docs/action-references.md)
for the steps. Reusable workflow refs must be literal; expressions cannot select them.

This README describes its checkout. Read the docs at the chosen tag or commit
for a published release's contract. Packaging `revision`, separate source/spec
commits, explicit platforms and native receipts require a ref implementing those
interfaces; confirm support before following the
[packaging and platform guide](docs/packaging-platforms.md).

## Quick start

### 1. Describe the Formula

Add `.github/homebrew/formula.yml` to the product repository:

```yaml
name: example
desc: Example CLI
license: MIT
dependencies:
  build: [go]

install: |
  system "go", "build", *std_go_args(ldflags: "-s -w"), "./cmd/example"

test: |
  system bin/"example", "--version"
```

The `name` in this file is the Formula identity. Workflows do not repeat it.

### 2. Check pull requests

Add a reusable-workflow job to the product's pull-request CI:

```yaml
jobs:
  homebrew:
    permissions:
      contents: read

    uses: releaseway/homebrew-actions/.github/workflows/check.yml@<homebrew-actions-sha>
    with:
      tap-repository: owner/homebrew-tap
```

The check is read-only. It renders the Formula from the caller revision and runs the
Homebrew spec validation path. The stable result job is `homebrew-check`.

### 3. Publish after the product release

After the product build and GitHub Release are complete, publish the Formula for the
same immutable source commit:

```yaml
jobs:
  homebrew:
    needs: release

    permissions:
      contents: read

    uses: releaseway/homebrew-actions/.github/workflows/publish.yml@<homebrew-actions-sha>
    with:
      tap-repository: owner/homebrew-tap
      commit: ${{ needs.release.outputs.commit }}
      version: ${{ needs.release.outputs.version }}

    secrets:
      tap_deploy_key: ${{ secrets.HOMEBREW_TAP_DEPLOY_KEY }}
```

`publish.yml` renders the Formula, validates it on the required native Homebrew
runners, and updates the tap only after validation succeeds. It returns:

- `formula`: Formula name from the spec;
- `version`: normalized Formula version;
- `state`: `published` when the tap changed, otherwise `unchanged`.

Keep the same full automation SHA in both workflows. `commit` and `version`
identify the product release, independently of that automation ref.

## GitHub Release distributions

When the Formula installs prebuilt GitHub Release assets, declare them in the same spec:

```yaml
name: example
desc: Example CLI
license: MIT

distribution:
  type: github-release
  tag: "v{version}"
  assets:
    macos-arm64: example_macos_arm64.tar.gz
    macos-x86_64: example_macos_x86_64.tar.gz
    linux-arm64: example_linux_arm64.tar.gz
    linux-x86_64: example_linux_x86_64.tar.gz

install: |
  bin.install "example"

test: |
  system bin/"example", "--version"
```

The referenced release must already exist when `publish.yml` runs. Release creation,
asset upload, version selection, and product build ordering remain product
responsibilities.

## Command aliases

Declare additional names for installed commands in the product spec:

```yaml
command_aliases:
  ex: example
  example-cli: example

install: |
  bin.install "example"

test: |
  system bin/"example", "--version"
  system bin/"ex", "--version"
  system bin/"example-cli", "--version"
```

Each key is an alias and each value names an executable already installed in `bin`.
Names start with a letter or number and contain only letters, numbers, `.`, `_`,
`+`, and `-`. The renderer adds relative symlinks after `install`, before
`post_install`, for both source and GitHub Release distributions. Formula identity
and release assets stay independent of these command names.

Omitting the mapping or setting it to `{}` preserves the existing Formula.
Null, duplicate or case-conflicting aliases, self references and alias chains fail
validation. Installation checks every target and destination before creating any
alias; missing or non-executable targets and existing files, directories or symlinks
fail without overwriting them.

Remove manually created alias links from `install`
when switching to this setting. Targets created only by `post_install` are too late.

The product owns command tests and their arguments. Homebrew manages linking,
upgrades and removal; global command conflicts follow its normal policy, and
`keg_only` still limits global command exposure. This setting adds command names
without shell arguments or environment overrides. It does not add Formula aliases
for `brew install` or promise behavior selected by the invocation name.

## Tap write credential

Publishing requires exactly one credential with write access to the destination tap:

- `tap_deploy_key`: SSH deploy key;
- `tap_token`: token with repository contents write access.

A deploy key keeps write authority scoped to the tap repository. The included setup
script can provision one and store the private key directly as a source-repository
Actions secret without printing it:

```sh
TAP_REPO=owner/homebrew-tap SOURCE_REPO=owner/product bash scripts/setup-deploy-key.sh
```

The default secret name is `HOMEBREW_TAP_DEPLOY_KEY`. Use `--force` only for an
intentional key rotation.

## Advanced options

Both workflows require an explicit `tap-repository` in `owner/repo` form.
Defaults for the remaining inputs are:

- tap branch: `main`;
- spec path: `.github/homebrew/formula.yml`.

Configure the destination and optional paths:

```yaml
with:
  tap-repository: owner/homebrew-tap
  tap-branch: main
  spec-path: .github/homebrew/formula.yml
```

The publish workflow additionally requires `commit` and `version`. `commit` must be
the full source SHA used by the product release; the checked-out source is verified
against it before Formula rendering.

### Version protection and rollback

Publishing an existing Formula blocks version downgrades by default. At equal
scheme, version and packaging revision, metadata or install/test edits are allowed
when the source commit is unchanged. Release ordering compares
`(version_scheme, version, revision)`, with the scheme taking precedence and
omitted revision meaning `0`. See
[revision updates](docs/packaging-platforms.md#publish-a-packaging-fix-without-changing-upstream-source)
for packaging-only upgrades. The spec's optional `version_scheme` must be a
non-negative integer; omitting it means `0`.

To restart version numbering, add
it to the product spec:

```yaml
version_scheme: 1
```

This renders `version_scheme 1` in both source and GitHub Release Formulas, allowing
`(0, 11.0.4)` to advance to `(1, 1.2.0)`. Keep `version_scheme: 1` in subsequent
release specs, such as version `1.2.1`; omitting it resets the candidate scheme to
`0` and is blocked as a downgrade. Increase the scheme only when changing the
version numbering system. A scheme-only increase is a publishable change, even
when the version and source commit remain unchanged. At equal scheme and version,
the same-source policy still applies. Existing Formulas without a scheme use `0`.

Generated Formulas retain their version and source commit so updates can be checked
before committing and against the latest remote tap before each push. Different
versions of the same product share a publish concurrency group. Concurrent updates
to another Formula are preserved through bounded fetch/rebase retries.

For an intentional rollback, same-version source replacement, or a version that
cannot be compared numerically, explicitly opt in:

```yaml
with:
  tap-repository: owner/homebrew-tap
  commit: <full-source-sha>
  version: "1.2.3"
  allow-downgrade: true
```

This option permits replacing the existing version, including decreasing its
version scheme. Source SHA, release provenance, asset digest and Formula validation
still apply. A new Formula can use any supported
non-empty version. Older source Formulas can be compared using their explicit version
and archive commit; a same-version update without identifiable source provenance
requires the explicit override. Conflicting edits to the same Formula can still fail
during rebase; rerun against the latest tap state.

### Validated Formula bytes

Publishing freezes the generated Formula's SHA-256 before uploading it.
Validation and publication consume that identity. Every rebase and push checks
both the working file and the Formula blob in `HEAD` against those validated
bytes. An automatically merged change to the same Formula can therefore stop
publication even when Git reports no conflict. Other Formula changes remain
eligible for bounded rebase retries.

If this check fails, rerun the complete publishing workflow against the current
tap so the candidate is generated and validated again. A retry of only the push
step cannot certify different bytes. `allow-downgrade` controls version ordering
and never bypasses this byte check.

## Formula ownership

The spec contains product-specific Homebrew intent such as metadata, dependencies,
install/test behavior, optional stanzas, and release-asset names.

The automation owns generated Formula structure, class naming, source URLs, checksums,
platform blocks, field ordering, native validation, tap commit creation, and bounded
fetch/rebase/retry when the tap advances concurrently. It never force-pushes the tap.

Adding a package requires only its product-side spec and workflow. The same publish
path creates a new Formula or updates an existing one; no tap-side package list,
manual Formula edit, or pull-request merge is required.

## Tap maintenance

Create a new tap with [homebrew-tap-starter](https://github.com/releaseway/homebrew-tap-starter).
Existing public taps can call the same reusable workflows:

- `tap-check.yml`: read-only Ruby syntax and strict audit of all Formulae. Empty taps pass.
- `tap-delete.yml`: deletes one `formula` from the caller's default branch, using
  `contents: write`. Optional `dry-run` defaults to `false`. Outputs `state`
  (`dry-run` or `deleted`). As in the original maintenance script, a missing
  Formula fails rather than reporting a successful deletion.

Tap maintenance uses the caller's token and needs no cross-repository write secret.
Direct pushes must be allowed by the destination branch policy.

## Validation model

`check.yml` uses spec validation and does not require a tap write credential.

`publish.yml` uses release validation. For GitHub Release distributions it verifies the
published immutable release, source commit, declared assets, and SHA-256 digests, then
installs/tests the generated Formula on every required native runner before publishing.

Declare `platforms` to select the supported native targets. Source and prebuilt
distributions validate every selected target; prebuilt asset keys must match that
set. Without `platforms`, source release validation runs on macOS arm64 only and
does not prove Linux or Intel execution. Prebuilt specs without `platforms` require
both arm64 and x86_64 for each declared operating system and validate those pairs.
See [platform configuration](docs/packaging-platforms.md) for single-architecture
and asymmetric sets. Spec audits alone do not prove native installation.

Linux x86_64 validation and spec audits use Ubuntu 24.04, matching the Linux arm64
runner's Ubuntu release. Formula generation, tap publication and deletion also
use Ubuntu 24.04; native installation tests keep their platform-specific runners.
On GitHub-hosted runners, validation temporarily isolates
preinstalled taps unrelated to the Formula or its dependencies, restoring them on
success or failure. Local and self-hosted tap checkouts remain in place. Kernel
sandbox capability warnings and GitHub runner capacity notices remain visible.

Every reusable workflow checks out its implementation from
`job.workflow_repository@job.workflow_sha`, so a full-SHA workflow pin also pins its
internal renderer and scripts.

## Development

See [CONTRIBUTING.md](CONTRIBUTING.md) for local regressions, Homebrew loader
coverage, implementation ownership and automation-release acceptance.

The [same-run release guide](https://github.com/releaseway/actions/blob/main/docs/app-free-release.md)
connects version preparation, GitHub publication and this tap workflow. Homebrew
uses the caller's tap-scoped deploy key or explicit token; no additional GitHub App
is required.

## License

MIT
