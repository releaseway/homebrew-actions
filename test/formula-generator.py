"""Exercise GitHub Release formula generation without external downloads."""

from hashlib import sha256
from pathlib import Path
import json
import os
import shutil
import subprocess
import tempfile


root = Path(__file__).resolve().parents[1]
generator = root / "internal/formula/generate.sh"
baseline_generator = os.environ.get("RELEASEWAY_BASELINE_GENERATOR")
platform_assets = {
    "macos-arm64": "example_1.2.3_darwin_arm64.tar.gz",
    "macos-x86_64": "example_1.2.3_darwin_amd64.tar.gz",
    "linux-arm64": "example_1.2.3_linux_arm64.tar.gz",
    "linux-x86_64": "example_1.2.3_linux_amd64.tar.gz",
}


def git(path, *args):
    result = subprocess.run(
        ["git", "-C", str(path), *args],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def spec(distribution):
    lines = [
        "name: example",
        "desc: Release fixture",
        "license: MIT",
        *distribution,
        "install: |",
        '  bin.install "example"',
        "test: |",
        '  system "#{bin}/example", "--version"',
    ]
    return "\n".join(lines) + "\n"


with tempfile.TemporaryDirectory() as directory:
    workspace = Path(directory)
    source = workspace / "source"
    tap = workspace / "tap"
    assets = workspace / "assets"
    fake_bin = workspace / "bin"
    for path in [source, tap, assets, fake_bin]:
        path.mkdir()

    git(source, "init", "--initial-branch=main")
    git(source, "config", "user.name", "test")
    git(source, "config", "user.email", "test@example.invalid")
    git(source, "config", "commit.gpgsign", "false")
    git(source, "config", "tag.gpgsign", "false")
    git(source, "config", "core.hooksPath", "/dev/null")
    spec_path = source / ".github/homebrew/formula.yml"
    spec_path.parent.mkdir(parents=True)
    spec_path.write_text("name: example\ndesc: placeholder\nlicense: MIT\ninstall: x\ntest: x\n")
    git(source, "add", ".")
    git(source, "commit", "-m", "fixture")
    resolved_ref = subprocess.run(
        ["git", "-C", str(source), "rev-parse", "HEAD"],
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
    release_tag = "formula-fixture-v1.2.3"
    git(source, "tag", release_tag)

    for index, asset in enumerate(platform_assets.values()):
        (assets / asset).write_bytes(f"asset-{index}\n".encode())
    (assets / f"{resolved_ref}.tar.gz").write_bytes(b"source-archive\n")

    curl = fake_bin / "curl"
    curl.write_text(
        """#!/bin/sh
set -eu
[ "${FAKE_NETWORK_FAIL:-false}" != "true" ] || exit 99
while [ "$#" -gt 0 ]; do
  case "$1" in
    -o) output="$2"; shift 2 ;;
    -*) shift ;;
    *) url="$1"; shift ;;
  esac
done
cp "$FAKE_RELEASE_DIR/${url##*/}" "$output"
"""
    )
    curl.chmod(0o755)

    release_json = workspace / "release.json"
    base_release = {
        "tag_name": release_tag,
        "draft": False,
        "immutable": True,
        "published_at": "2026-09-14T00:00:00Z",
        "assets": [
            {
                "name": asset,
                "state": "uploaded",
                "digest": f"sha256:{sha256((assets / asset).read_bytes()).hexdigest()}",
            }
            for asset in platform_assets.values()
        ],
    }
    release_json.write_text(json.dumps(base_release))
    gh = fake_bin / "gh"
    gh.write_text(
        """#!/bin/sh
set -eu
[ "${FAKE_NETWORK_FAIL:-false}" != "true" ] || exit 99
case "$4" in
  repos/*/releases/tags/*) cat "$FAKE_RELEASE_JSON" ;;
  repos/*/commits/*) printf '{"sha":"%s"}\\n' "$FAKE_TAG_COMMIT" ;;
  *) echo "unexpected GitHub API path: $4" >&2; exit 1 ;;
esac
"""
    )
    gh.chmod(0o755)

    base_env = dict(
        os.environ,
        TAP_PATH=str(tap),
        SOURCE_PATH=str(source),
        REPOSITORY="owner/example",
        COMMIT=resolved_ref,
        VERSION="v1.2.3",
        VALIDATION_MODE="release",
        SPEC_PATH=".github/homebrew/formula.yml",
        FAKE_RELEASE_DIR=str(assets),
        FAKE_RELEASE_JSON=str(release_json),
        FAKE_TAG_COMMIT=resolved_ref,
        GH_TOKEN="test-token",
        PATH=f"{fake_bin}:{os.environ['PATH']}",
    )

    def generate(distribution, expected_error=None, env_overrides=None, install=None):
        text = spec(distribution)
        if install is not None:
            text = text.replace('  bin.install "example"', install)
        spec_path.write_text(text)
        output = workspace / "github-output"
        output.write_text("")
        env = dict(base_env, GITHUB_OUTPUT=str(output))
        env.update(env_overrides or {})
        result = subprocess.run(
            ["bash", str(generator)],
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        if baseline_generator and not any(line.startswith("command_aliases:") for line in distribution):
            current_formula = (tap / "Formula/example.rb").read_bytes() if result.returncode == 0 else None
            baseline_output = workspace / "baseline-output"
            baseline_output.write_text("")
            baseline = subprocess.run(["bash", baseline_generator], env={**env, "GITHUB_OUTPUT": str(baseline_output)}, text=True, capture_output=True)
            assert baseline.returncode == result.returncode, (baseline.stderr, result.stderr)
            if result.returncode == 0:
                assert current_formula == (tap / "Formula/example.rb").read_bytes()
                assert output.read_bytes() == baseline_output.read_bytes()
        if expected_error:
            assert result.returncode != 0, "invalid release distribution should fail"
            assert expected_error in result.stderr, result.stderr
            return None
        assert result.returncode == 0, result.stderr
        return output.read_text()

    output = generate([])
    formula = (tap / "Formula/example.rb").read_text()
    assert "distribution=source" in output
    assert 'runner-matrix=[{"platform":"macos-arm64","runner":"macos-latest"}]' in output
    assert "  depends_on :macos\n" not in formula
    assert "  depends_on :linux\n" not in formula
    assert "version_scheme" not in formula

    # Omission and an empty declaration preserve the existing artifact/output bytes.
    original_formula = (tap / "Formula/example.rb").read_bytes()
    original_output = output
    assert generate(["command_aliases: {}"]) == original_output
    assert (tap / "Formula/example.rb").read_bytes() == original_formula
    invalid_aliases = [
        (["command_aliases: null"], "command_aliases must be a mapping"),
        (["command_aliases: []"], "command_aliases must be a mapping"),
        (["command_aliases: example"], "command_aliases must be a mapping"),
        (["command_aliases:", "  ex: null"], "command_aliases.ex:"),
        (["command_aliases:", "  1: example"], "command_aliases.1:"),
        (["command_aliases:", "  ex: [example]"], "command_aliases.ex:"),
        (["command_aliases:", "  ex: example", "  ex: other"], "duplicate command_aliases.ex"),
        (["command_aliases: {}", "command_aliases: {}"], "duplicate command_aliases field"),
        (["command_aliases:", "  ex: example", "  EX: other"], "conflicts with alias"),
        (["command_aliases:", "  ex: EX"], "alias chains are not supported"),
        (["command_aliases:", "  ex: short", "  short: example"], "alias chains are not supported"),
        (["command_aliases:", "  ex: short", "  short: ex"], "alias chains are not supported"),
    ]
    for name in ["../ex", "/ex", "ex/other", "ex\\other", ".ex", "-ex", "ex other", "ex;echo", "ex#{1}"]:
        for entry in [f"  {json.dumps(name)}: example", f"  ex: {json.dumps(name)}"]:
            invalid_aliases.append((["command_aliases:", entry], "command_aliases."))
    for declaration, error in invalid_aliases:
        before = (tap / "Formula/example.rb").read_bytes()
        generate(declaration, error, {"FAKE_NETWORK_FAIL": "true"})
        assert (tap / "Formula/example.rb").read_bytes() == before
        assert not (workspace / "github-output").read_text()

    declarations = ["command_aliases:", "  zz: second", "  ex: example", "  example-cli: example"]
    generate(declarations, install="  nil")
    alias_formula = (tap / "Formula/example.rb").read_text()
    assert alias_formula.index('=> "ex"') < alias_formula.index('=> "example-cli"') < alias_formula.index('=> "zz"')
    assert alias_formula.rindex("odie ") < alias_formula.index("bin.install_symlink")
    assert alias_formula.index("    nil") < alias_formula.index("    odie")

    # Execute the generated install method, with only the small Homebrew DSL
    # boundary supplied by the harness. Native Homebrew acceptance tests the API.
    harness = workspace / "install.rb"
    harness.write_text('''require "pathname"
require "fileutils"
class Formula
  attr_accessor :bin
  def self.method_missing(*) = nil
  def self.test = nil
  def odie(message) = raise(message)
end
class Pathname
  def install_symlink(pairs)
    pairs.each { |source, name| File.symlink(source.relative_path_from(self), self/name) }
  end
end
load ARGV.fetch(0)
formula = Example.new
formula.bin = Pathname(ARGV.fetch(1))
formula.install
''')

    def install_aliases(failure=None):
        installed = workspace / "installed"
        shutil.rmtree(installed, ignore_errors=True)
        installed.mkdir()
        for command in ["example", "second"]:
            path = installed / command
            path.write_text('#!/bin/sh\nprintf "%s\\n" "$*"\nexit 17\n')
            path.chmod(0o755)
        if failure == "missing":
            (installed / "second").unlink()
        elif failure == "non-executable":
            (installed / "second").chmod(0o644)
        elif failure == "directory-target":
            (installed / "second").unlink()
            (installed / "second").mkdir()
        elif failure in ["file", "directory", "symlink", "broken-symlink"]:
            destination = installed / "zz"
            if failure == "file": destination.write_text("preserved")
            elif failure == "directory": destination.mkdir()
            else: destination.symlink_to("second" if failure == "symlink" else "missing")
        result = subprocess.run(["ruby", str(harness), str(tap / "Formula/example.rb"), str(installed)], text=True, capture_output=True)
        if failure:
            assert result.returncode != 0 and "command_aliases.zz:" in result.stderr, result.stderr
            assert not (installed / "ex").is_symlink(), "preflight must precede all alias creation"
            if failure == "file": assert (installed / "zz").read_text() == "preserved"
        else:
            assert result.returncode == 0, result.stderr
            for command in ["ex", "example-cli", "zz"]:
                link = installed / command
                assert link.is_symlink() and not link.readlink().is_absolute()
                run = subprocess.run([str(link), "--probe", "two words"], text=True, capture_output=True)
                assert run.returncode == 17 and run.stdout == "--probe two words\n"
            moved = workspace / "moved"
            shutil.rmtree(moved, ignore_errors=True)
            installed.rename(moved)
            assert (moved / "ex").resolve() == (moved / "example").resolve()
            run = subprocess.run([str(moved / "ex"), "--probe"], text=True, capture_output=True)
            assert run.returncode == 17 and run.stdout == "--probe\n"

    install_aliases()
    for failure in ["missing", "non-executable", "directory-target", "file", "directory", "symlink", "broken-symlink"]:
        install_aliases(failure)

    generate(["version_scheme: 0"])
    assert "version_scheme" not in (tap / "Formula/example.rb").read_text()
    for scheme in [1, 2]:
        generate([f"version_scheme: {scheme}"])
        assert f"  version_scheme {scheme}\n" in (tap / "Formula/example.rb").read_text()
    for invalid in ["-1", "1.5", '"1"', "true", "null", "[]", "{}"]:
        generate([f"version_scheme: {invalid}"], "version_scheme must be a non-negative integer")

    output = generate([], env_overrides={
        "VALIDATION_MODE": "spec", "FAKE_NETWORK_FAIL": "true",
    })
    formula = (tap / "Formula/example.rb").read_text()
    assert f'sha256 "{"0" * 64}"' in formula
    assert "validation-mode=spec" in output
    assert 'runner-matrix=[{"platform":"spec","runner":"ubuntu-24.04"}]' in output

    output = generate([
        "version_scheme: 1",
        *declarations,
        "distribution:",
        "  type: github-release",
        f'  tag: "{release_tag}"',
        "  assets:",
        *[f"    {platform}: {asset.replace('1.2.3', '{version}')}" for platform, asset in platform_assets.items()],
    ])
    formula = (tap / "Formula/example.rb").read_text()
    assert 'version "1.2.3"' in formula
    assert formula.count("bin.install_symlink") == 3
    assert "  version_scheme 1\n" in formula
    assert "  depends_on :macos\n" not in formula
    assert "  depends_on :linux\n" not in formula
    assert formula.count("on_macos do") == 1
    assert formula.count("on_linux do") == 1
    def source_lines(platform):
        asset = platform_assets[platform]
        url = f"https://github.com/owner/example/releases/download/{release_tag}/{asset}"
        checksum = sha256((assets / asset).read_bytes()).hexdigest()
        return f'url "{url}"', f'sha256 "{checksum}"'

    for os_name, arm_platform, intel_platform in [
        ("macos", "macos-arm64", "macos-x86_64"),
        ("linux", "linux-arm64", "linux-x86_64"),
    ]:
        arm_url, arm_checksum = source_lines(arm_platform)
        intel_url, intel_checksum = source_lines(intel_platform)
        expected_block = f"""  on_{os_name} do
    on_arm do
      {arm_url}
      {arm_checksum}
    end
    on_intel do
      {intel_url}
      {intel_checksum}
    end
  end
"""
        assert expected_block in formula
    assert "distribution=github-release" in output
    assert f"release-tag={release_tag}" in output
    assert 'runner-matrix=[{"platform":"macos-arm64","runner":"macos-latest"},{"platform":"macos-x86_64","runner":"macos-15-intel"},{"platform":"linux-arm64","runner":"ubuntu-24.04-arm"},{"platform":"linux-x86_64","runner":"ubuntu-24.04"}]' in output
    assert "archive-url=\n" in output
    assert "sha256=\n" in output

    macos_assets = {key: value for key, value in platform_assets.items() if key.startswith("macos-")}
    output = generate([
        "distribution:",
        "  type: github-release",
        f'  tag: "{release_tag}"',
        "  assets:",
        *[f"    {platform}: {asset}" for platform, asset in macos_assets.items()],
    ])
    formula = (tap / "Formula/example.rb").read_text()
    assert "  depends_on :macos\n" in formula
    assert "  on_macos do\n" in formula
    assert "  on_linux do\n" not in formula
    assert 'runner-matrix=[{"platform":"macos-arm64","runner":"macos-latest"},{"platform":"macos-x86_64","runner":"macos-15-intel"}]' in output

    linux_assets = {key: value for key, value in platform_assets.items() if key.startswith("linux-")}
    output = generate([
        "distribution:",
        "  type: github-release",
        f'  tag: "{release_tag}"',
        "  assets:",
        *[f"    {platform}: {asset}" for platform, asset in linux_assets.items()],
    ])
    formula = (tap / "Formula/example.rb").read_text()
    assert "  depends_on :linux\n" in formula
    assert "  on_macos do\n" not in formula
    assert "  on_linux do\n" in formula
    assert 'runner-matrix=[{"platform":"linux-arm64","runner":"ubuntu-24.04-arm"},{"platform":"linux-x86_64","runner":"ubuntu-24.04"}]' in output

    future_tag = "formula-fixture-v9.9.9"
    output = generate([
        "distribution:",
        "  type: github-release",
        f'  tag: "{future_tag}"',
        "  assets:",
        *[f"    {platform}: {asset}" for platform, asset in platform_assets.items()],
    ], env_overrides={"VALIDATION_MODE": "spec", "FAKE_NETWORK_FAIL": "true"})
    formula = (tap / "Formula/example.rb").read_text()
    assert f"/releases/download/{future_tag}/" in formula
    assert formula.count(f'sha256 "{"0" * 64}"') == 4
    assert "validation-mode=spec" in output
    assert 'runner-matrix=[{"platform":"spec","runner":"ubuntu-24.04"}]' in output

    release_json.write_text(json.dumps({**base_release, "draft": True}))
    generate([
        "distribution:",
        "  type: github-release",
        f'  tag: "{release_tag}"',
        "  assets:",
        *[f"    {platform}: {asset}" for platform, asset in platform_assets.items()],
    ], f"GitHub Release {release_tag} is a draft")

    release_json.write_text(json.dumps({**base_release, "published_at": None}))
    generate([
        "distribution:",
        "  type: github-release",
        f'  tag: "{release_tag}"',
        "  assets:",
        *[f"    {platform}: {asset}" for platform, asset in platform_assets.items()],
    ], f"GitHub Release {release_tag} is not published")

    release_json.write_text(json.dumps({**base_release, "immutable": False}))
    generate([
        "distribution:",
        "  type: github-release",
        f'  tag: "{release_tag}"',
        "  assets:",
        *[f"    {platform}: {asset}" for platform, asset in platform_assets.items()],
    ], f"GitHub Release {release_tag} must be immutable")

    missing_asset_release = json.loads(json.dumps(base_release))
    missing_asset_release["assets"] = missing_asset_release["assets"][1:]
    release_json.write_text(json.dumps(missing_asset_release))
    generate([
        "distribution:",
        "  type: github-release",
        f'  tag: "{release_tag}"',
        "  assets:",
        *[f"    {platform}: {asset}" for platform, asset in platform_assets.items()],
    ], f"GitHub Release {release_tag} is missing asset")

    pending_asset_release = json.loads(json.dumps(base_release))
    pending_asset_release["assets"][0]["state"] = "new"
    release_json.write_text(json.dumps(pending_asset_release))
    generate([
        "distribution:",
        "  type: github-release",
        f'  tag: "{release_tag}"',
        "  assets:",
        *[f"    {platform}: {asset}" for platform, asset in platform_assets.items()],
    ], "GitHub Release asset " + list(platform_assets.values())[0] + " is not uploaded")

    duplicate_asset_release = json.loads(json.dumps(base_release))
    duplicate_asset_release["assets"].append(duplicate_asset_release["assets"][0])
    release_json.write_text(json.dumps(duplicate_asset_release))
    generate([
        "distribution:",
        "  type: github-release",
        f'  tag: "{release_tag}"',
        "  assets:",
        *[f"    {platform}: {asset}" for platform, asset in platform_assets.items()],
    ], f"GitHub Release {release_tag} contains duplicate asset")

    release_json.write_text(json.dumps(base_release))
    generate([
        "distribution:",
        "  type: github-release",
        f'  tag: "{release_tag}"',
        "  assets:",
        *[f"    {platform}: {asset}" for platform, asset in platform_assets.items()],
    ], f"GitHub Release tag {release_tag} does not resolve to source commit", {
        "FAKE_TAG_COMMIT": "f" * 40,
    })

    fallback_release = json.loads(json.dumps(base_release))
    fallback_release["assets"][0]["digest"] = None
    release_json.write_text(json.dumps(fallback_release))
    generate([
        "distribution:",
        "  type: github-release",
        f'  tag: "{release_tag}"',
        "  assets:",
        *[f"    {platform}: {asset}" for platform, asset in platform_assets.items()],
    ])
    release_json.write_text(json.dumps(base_release))

    generate([
        "distribution:",
        "  type: github-release",
        '  tag: "v{version}"',
        "  assets:",
        "    macos-arm64: example_{version}_darwin_arm64.tar.gz",
    ], "distribution.assets is missing macos platforms: macos-x86_64")

    generate([
        "distribution:",
        "  type: github-release",
        '  tag: "v{version}"',
        "  assets: {}",
    ], "distribution.assets must declare at least one supported operating system")

    generate([
        "distribution:",
        "  type: github-release",
        '  tag: "v{version}"',
        "  assets:",
        *[f"    {platform}: {asset}" for platform, asset in macos_assets.items()],
        "    windows-x86_64: example_1.2.3_windows_amd64.zip",
    ], "distribution.assets has unsupported platforms: windows-x86_64")

    generate([
        "distribution:",
        "  type: github-release",
        '  tag: "v{channel}"',
        "  assets:",
        *[f"    {platform}: {asset}" for platform, asset in platform_assets.items()],
    ], "distribution.tag contains an unsupported template placeholder")

    generate([
        "distribution:",
        "  type: github-release",
        '  tag: "v{version}"',
        "  assets:",
        *[
            f"    {platform}: {'../escape.tar.gz' if platform == 'linux-arm64' else asset}"
            for platform, asset in platform_assets.items()
        ],
    ], "distribution.assets.linux-arm64 may contain only")

    # Architecture digits in versionless assets must never replace caller VERSION.
    generate([
        "distribution:",
        "  type: github-release",
        '  tag: "app-free-v{version}"',
        "  assets:",
        *[f"    {platform}: example_{platform.replace('-', '_')}.tar.gz" for platform in platform_assets],
    ], env_overrides={"VALIDATION_MODE": "spec", "FAKE_NETWORK_FAIL": "true"})
    formula = (tap / "Formula/example.rb").read_text()
    assert '  version "1.2.3"\n' in formula
    assert '/app-free-v1.2.3/example_linux_arm64.tar.gz' in formula

    print("GitHub Release formula generation passed")
