"""Verify hosted tap isolation and cleanup without changing the host's Homebrew."""
import json
import os
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "internal/scripts/validate-formula.sh"

BREW = r'''#!/usr/bin/env python3
import json, os, shutil, sys
from pathlib import Path
root = Path(os.environ["FAKE_BREW_ROOT"])
args = sys.argv[1:]
with (root / "calls.jsonl").open("a") as log:
    log.write(json.dumps(args) + "\n")
def tap_path(name):
    owner, repo = name.split("/")
    return root / "Taps" / owner / ("homebrew-" + repo)
if args[0] == "--repository":
    print(tap_path(args[1]))
elif args[0] == "tap":
    if len(args) == 1:
        for path in sorted((root / "Taps").glob("*/*")):
            print(path.parent.name + "/" + path.name.removeprefix("homebrew-"))
    else:
        shutil.copytree(args[2], tap_path(args[1]))
elif args[0] == "deps":
    print("vendor/tools/build-tool\nvendor/tools/test-tool\nopenssl@3")
    sys.exit(int(os.environ.get("FAIL_DEPS", "0")))
elif args[0] in ("audit", "install", "test"):
    assert tap_path("vendor/tools").is_dir(), "dependency tap was hidden"
    assert tap_path("homebrew/core").is_dir(), "core tap was hidden"
    assert tap_path("homebrew/cask").is_dir(), "cask tap was hidden"
    hosted = os.environ.get("RUNNER_ENVIRONMENT") == "github-hosted"
    if hosted:
        assert not tap_path("aws/tap").exists(), "unrelated AWS tap was evaluated"
        assert not tap_path("hashicorp/tap").exists(), "broken Vagrant tap was evaluated"
        assert os.environ.get("HOMEBREW_NO_AUTO_UPDATE") == "1"
        assert os.environ.get("HOMEBREW_NO_INSTALL_CLEANUP") == "1"
    else:
        assert tap_path("aws/tap").is_dir(), "local tap was moved"
    sys.exit(int(os.environ.get("FAIL_" + args[0].upper(), "0")))
elif args[0] == "untap":
    shutil.rmtree(tap_path(args[-1]))
elif args[0] not in ("trust", "untrust"):
    raise AssertionError(args)
'''


def check(*, hosted=True, mode="release", failure=None):
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        brew_root = root / "brew"
        bin_dir = root / "bin"
        bin_dir.mkdir()
        (bin_dir / "brew").write_text(BREW)
        (bin_dir / "brew").chmod(0o755)
        originals = {}
        for name in ["aws/tap", "hashicorp/tap", "vendor/tools", "homebrew/core", "homebrew/cask"]:
            owner, repo = name.split("/")
            path = brew_root / "Taps" / owner / ("homebrew-" + repo) / "marker"
            path.parent.mkdir(parents=True)
            path.write_text(name)
            originals[path] = path.read_bytes()
        tap = root / "tap"
        (tap / "Formula").mkdir(parents=True)
        (tap / "Formula/example.rb").write_text("class Example < Formula\nend\n")
        temp = root / "temp"
        temp.mkdir()
        env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}", FAKE_BREW_ROOT=str(brew_root),
                   TAP_PATH=str(tap), FORMULA="example", VALIDATION_MODE=mode,
                   GITHUB_ACTIONS="true", RUNNER_ENVIRONMENT="github-hosted" if hosted else "self-hosted",
                   TMPDIR=str(temp), GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_NOSYSTEM="1")
        if failure:
            env["FAIL_" + failure.upper()] = "7"
        result = subprocess.run(["bash", str(SCRIPT)], env=env, text=True, capture_output=True)
        assert result.returncode == (7 if failure else 0), result.stderr
        for path, content in originals.items():
            assert path.read_bytes() == content, f"original tap not restored: {path}"
        assert not list(temp.iterdir()), "temporary validation checkout was leaked"
        assert not list((brew_root / "Taps/releaseway").glob("*"))
        calls = [json.loads(line) for line in (brew_root / "calls.jsonl").read_text().splitlines()]
        commands = [args[0] for args in calls]
        assert commands[-2:] == ["untap", "untrust"]
        if not hosted:
            assert "deps" not in commands
            assert "--repository" not in commands
        if mode == "spec":
            assert "install" not in commands and "test" not in commands


check()
check(mode="spec")
check(hosted=False)
for command in ["deps", "audit", "install", "test"]:
    check(failure=command)
print("Formula validation isolation passed")
