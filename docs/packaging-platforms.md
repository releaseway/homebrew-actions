# Packaging revisions and supported platforms

Use a homebrew-actions ref whose spec accepts `revision` and `platforms`, and whose
workflows expose `spec-commit` and native validation outputs. This guide describes
its checkout; verify support at your selected release tag or commit. Replace
`<homebrew-actions-sha>` with that full 40-character SHA using
[reference selection](https://github.com/releaseway/actions/blob/main/docs/action-references.md).
When no published release supports the needed inputs, use a reviewed source commit
for candidate evaluation. Reusable workflow refs must be literal.

## Validate source packages on the platforms you support

Add a top-level `platforms` list to `.github/homebrew/formula.yml`:

```yaml
name: example
desc: Example CLI
license: MIT
revision: 0
platforms: [macos-arm64, macos-x86_64, linux-arm64, linux-x86_64]
dependencies:
  build: [go]
install: |
  system "go", "build", *std_go_args, "./cmd/example"
test: |
  system bin/"example", "--version"
```

Adapt the build and test commands to your product. Both source and prebuilt
distributions use the same platform IDs and runner mapping:

| Platform | Native runner |
| --- | --- |
| `macos-arm64` | `macos-latest` |
| `macos-x86_64` | `macos-15-intel` |
| `linux-arm64` | `ubuntu-24.04-arm` |
| `linux-x86_64` | `ubuntu-24.04` |

The list must be nonempty, contain unique supported IDs, and is normalized to the
order above. Publishing requires audit, install and test on every selected native
target. The generated Formula also restricts installation to that OS/architecture
set. Runner labels are fixed; each receipt records the actual OS/CPU, Homebrew,
Ruby and image metadata (`unknown` when the image metadata is unavailable).

Omitting `platforms` preserves the legacy behavior: source publication validates
macOS arm64 and adds no new installation restriction. It therefore provides no
Linux or Intel execution evidence. Legacy prebuilt specs require both architectures
for each selected OS and validate those pairs. An empty list is not an omission.
`check.yml` continues to render and audit the spec on Ubuntu; it does not run this
native installation matrix.

## Publish a single-architecture or asymmetric prebuilt package

For a Linux x86_64-only product, declare exactly that platform and asset:

```yaml
name: example
desc: Example CLI
license: MIT
platforms: [linux-x86_64]
distribution:
  type: github-release
  tag: "v{version}"
  assets:
    linux-x86_64: example_linux_x86_64.tar.gz
install: |
  bin.install "example"
test: |
  system bin/"example", "--version"
```

The immutable product Release must already exist and its tag must resolve to the
source `commit`. Asset mapping keys must exactly match `platforms`: missing and
extra keys fail. Every mapped asset must be uploaded. Two platforms may intentionally
map to the same universal archive; each platform still receives native validation.
For `platforms: [macos-arm64, linux-x86_64]`, provide those two mappings only.

On an unsupported host, normal `brew install` fails the generated OS/architecture
requirement. A default URL allows Homebrew to parse the Formula on that host; it
does not grant installation support. This does not prohibit manual `brew fetch`.

## Publish a packaging fix without changing upstream source

`revision` is an optional nonnegative YAML integer, default `0`; zero emits no
Ruby revision stanza. Strings, booleans, negative values and duplicate `revision` fields
are rejected. For upstream `1.2.3`, change `revision: 0` to `revision: 1` when
installation, dependencies or build options need a package upgrade. Homebrew's
installed package version becomes `1.2.3_1`.

Keep source **A** fixed and commit the new spec at **B** in the same product
repository. B contains the spec at the same `spec-path`; no product rebuild or
replacement of an immutable Release is needed for this packaging-only change.
Pass both full SHAs through outputs of your own preparation job:

```yaml
jobs:
  homebrew:
    needs: packaging
    permissions:
      contents: read
    uses: releaseway/homebrew-actions/.github/workflows/publish.yml@<homebrew-actions-sha>
    with:
      tap-repository: owner/homebrew-tap
      commit: ${{ needs.packaging.outputs.source-commit }}
      spec-commit: ${{ needs.packaging.outputs.spec-commit }}
      version: "1.2.3"
    secrets:
      tap_deploy_key: ${{ secrets.HOMEBREW_TAP_DEPLOY_KEY }}
```

This is a job fragment; `packaging` must provide A and B, and the spec at B must
set `revision: 1`. The source archive and prebuilt Release tag remain bound to A.
Spec bytes and their provenance come from B. Omitting `spec-commit` uses `commit`.
For read-only `check.yml`, optional `commit` defaults to the caller SHA and optional
`spec-commit` defaults to that source commit. Both SHAs must exist in the caller repo.

Updates compare `(version_scheme, version, revision)` in that order. At equal
scheme/version, a higher revision is allowed only with the same source A; lower
revisions are blocked. Equal revision with identical Formula bytes returns
`unchanged`. Same-source metadata edits at an equal tuple can still be published,
but they do not request a Homebrew package upgrade. Increase revision for that.
When upstream version advances, reset revision to zero in the new version's spec;
the renderer does not silently reset an explicitly retained revision.

`version_scheme` remains a separate persistent version-numbering policy, default
zero. A higher scheme takes precedence over version/revision. It is not a substitute
for a packaging revision. `allow-downgrade: true` explicitly permits rollback or
same-version source replacement; it never bypasses source/asset provenance, native
validation or the validated Formula byte check during tap push/rebase.

## Inspect publication evidence

Existing `formula`, `version`, `state` outputs remain. Publishing additionally returns
`spec-commit`, `revision`, `formula-sha256`, `validated-platforms` (a JSON array string)
and `validation-artifact-id`. The aggregate artifact contains
`homebrew-validation.json`, including the original native receipts and exact
source/spec/Formula/run/attempt/invocation identities. Individual receipts and the
aggregate are retained for 30 days. Repeated calls in one run receive distinct UUIDs.
Publication stops if any required receipt is missing, duplicated or inconsistent.

The automation's own release gate uses `homebrew-platforms-v2`: existing
public acceptance plus the new 21-case revision/platform campaign. Its durable
approval also preserves the six original aggregate ZIPs. See the
[fixture campaign](https://github.com/releaseway/release-fixture/tree/main/homebrew-features)
and [archive verification guide](https://github.com/releaseway/actions/blob/main/docs/release-evidence.md).

The automation release and each consumer's dependency update are separate
operations. Candidate acceptance establishes the tested SHA's behavior; it does
not establish that a published release contains those interfaces.
