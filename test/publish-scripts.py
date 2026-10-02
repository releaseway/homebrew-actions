#!/usr/bin/env python3
import os
from pathlib import Path
import subprocess
import shutil
import tempfile

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "internal" / "scripts"


def formula(version: str, commit: str = "a" * 40, description: str = "example") -> str:
    return f'# releaseway-version: {version}\n# releaseway-source-commit: {commit}\nclass Example < Formula\n  desc "{description}"\nend\n'


def run(script: str, *, cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    merged = dict(os.environ)
    merged.update(env)
    return subprocess.run(
        ["bash", str(SCRIPTS / script)],
        cwd=cwd,
        env=merged,
        text=True,
        capture_output=True,
        check=False,
    )


def configure(repo: Path) -> None:
    for key, value in [
        ("user.name", "test"),
        ("user.email", "test@example.invalid"),
        ("commit.gpgsign", "false"),
        ("tag.gpgSign", "false"),
        ("core.hooksPath", "/dev/null"),
    ]:
        subprocess.run(["git", "-C", str(repo), "config", key, value], check=True)


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        text=True,
        capture_output=True,
        check=True,
    )
    return result.stdout.strip()


def assert_preflight() -> None:
    with tempfile.TemporaryDirectory() as directory:
        cwd = Path(directory)
        base = {
            "COMMIT": "a" * 40,
            "VERSION": "v1.2.3",
            "TAP_REPOSITORY": "owner/homebrew-tap",
            "TAP_BRANCH": "main",
        }

        ok = run("validate-publish-inputs.sh", cwd=cwd, env=base)
        assert ok.returncode == 0, ok.stderr

        bad_commit = run(
            "validate-publish-inputs.sh",
            cwd=cwd,
            env={**base, "COMMIT": "main"},
        )
        assert bad_commit.returncode != 0
        assert "full 40-character SHA" in bad_commit.stderr

        bad_repo = run(
            "validate-publish-inputs.sh",
            cwd=cwd,
            env={**base, "TAP_REPOSITORY": "../tap"},
        )
        assert bad_repo.returncode != 0
        assert "owner/name" in bad_repo.stderr

        bad_branch = run(
            "validate-publish-inputs.sh",
            cwd=cwd,
            env={**base, "TAP_BRANCH": "main\nother"},
        )
        assert bad_branch.returncode != 0
        assert "single-line" in bad_branch.stderr

        bad_override = run("validate-publish-inputs.sh", cwd=cwd, env={**base, "ALLOW_DOWNGRADE": "yes"})
        assert bad_override.returncode != 0


def assert_credentials() -> None:
    with tempfile.TemporaryDirectory() as directory:
        cwd = Path(directory)

        def choose(token: str = "", deploy_key: str = ""):
            output = cwd / "output"
            output.write_text("")
            result = run(
                "select-tap-credential.sh",
                cwd=cwd,
                env={
                    "GITHUB_OUTPUT": str(output),
                    "TAP_TOKEN": token,
                    "TAP_DEPLOY_KEY": deploy_key,
                },
            )
            return result, output.read_text()

        result, output = choose(token="token-value")
        assert result.returncode == 0
        assert output == "mode=token\n"

        result, output = choose(deploy_key="key-value")
        assert result.returncode == 0
        assert output == "mode=deploy_key\n"

        result, _ = choose(token="token-value", deploy_key="key-value")
        assert result.returncode != 0
        assert "not both" in result.stdout

        result, _ = choose()
        assert result.returncode != 0
        assert "Provide tap_token or tap_deploy_key" in result.stdout


def assert_commit_and_retry() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        remote = root / "tap.git"
        seed = root / "seed"
        publisher = root / "publisher"
        competitor = root / "competitor"
        verify = root / "verify"

        subprocess.run(
            ["git", "init", "--bare", "--initial-branch=main", str(remote)],
            check=True,
            capture_output=True,
        )
        subprocess.run(["git", "clone", str(remote), str(seed)], check=True, capture_output=True)
        configure(seed)
        (seed / "Formula").mkdir()
        (seed / "Formula/example.rb").write_text(formula("0.9.0"))
        git(seed, "add", ".")
        git(seed, "commit", "-m", "seed")
        git(seed, "push", "origin", "main")

        subprocess.run(["git", "clone", str(remote), str(publisher)], check=True, capture_output=True)
        subprocess.run(["git", "clone", str(remote), str(competitor)], check=True, capture_output=True)
        configure(publisher)
        configure(competitor)

        output = root / "commit-output"
        output.write_text("")
        no_change = run(
            "commit-formula.sh",
            cwd=publisher,
            env={
                "GITHUB_OUTPUT": str(output),
                "FORMULA_PATH": "Formula/example.rb",
                "FORMULA": "example",
                "VERSION": "1.0.0",
            },
        )
        assert no_change.returncode == 0, no_change.stderr
        assert output.read_text() == "changed=false\n"

        (publisher / "Formula/example.rb").write_text(formula("1.0.0"))
        output.write_text("")
        changed = run(
            "commit-formula.sh",
            cwd=publisher,
            env={
                "GITHUB_OUTPUT": str(output),
                "FORMULA_PATH": "Formula/example.rb",
                "FORMULA": "example",
                "VERSION": "1.0.0",
                "COMMIT": "a" * 40,
            },
        )
        assert changed.returncode == 0, changed.stderr
        assert output.read_text() == "changed=true\n"
        assert git(publisher, "log", "-1", "--format=%s") == "chore: update example to 1.0.0"

        (competitor / "Formula/other.rb").write_text("other\n")
        git(competitor, "add", ".")
        git(competitor, "commit", "-m", "chore: update other")
        git(competitor, "push", "origin", "main")

        # Reject the first push to exercise the bounded retry, not just an initial rebase.
        wrappers = root / "wrappers"
        wrappers.mkdir()
        real_git = shutil.which("git")
        wrapper = wrappers / "git"
        marker = root / "push-rejected"
        attempts = root / "push-count"
        wrapper.write_text(
            "#!/usr/bin/env python3\nimport os, sys\nfrom pathlib import Path\n"
            + f"marker = Path({str(marker)!r})\nattempts = Path({str(attempts)!r})\n"
            + "if sys.argv[1] == 'push':\n"
            + "    with attempts.open('a') as output: output.write('push\\n')\n"
            + "    if not marker.exists():\n        marker.touch()\n        sys.exit(1)\n"
            + f"os.execv({real_git!r}, [{real_git!r}, *sys.argv[1:]])\n"
        )
        wrapper.chmod(0o755)
        retry_path = str(wrappers) + os.pathsep + os.environ["PATH"]
        pushed = run(
            "push-formula.sh",
            cwd=publisher,
            env={"TAP_BRANCH": "main", "PUSH_ATTEMPTS": "3", "FORMULA_PATH": "Formula/example.rb", "VERSION": "1.0.0", "COMMIT": "a" * 40, "PATH": retry_path},
        )
        assert pushed.returncode == 0, pushed.stderr
        assert attempts.read_text() == "push\npush\n"

        subprocess.run(["git", "clone", str(remote), str(verify)], check=True, capture_output=True)
        assert (verify / "Formula/example.rb").read_text() == formula("1.0.0")
        assert (verify / "Formula/other.rb").read_text() == "other\n"

        subjects = git(verify, "log", "--format=%s", "-3").splitlines()
        assert "chore: update example to 1.0.0" in subjects
        assert "chore: update other" in subjects

        push_script = (SCRIPTS / "push-formula.sh").read_text()
        assert "force" not in push_script.lower()

        # A newer publication on the same Formula must be checked before rebase/push.
        git(competitor, "pull", "--ff-only")
        (competitor / "Formula/example.rb").write_text(formula("2.0.0", "b" * 40))
        git(competitor, "add", ".")
        git(competitor, "commit", "-m", "publish newer formula")
        remote_head = git(competitor, "rev-parse", "HEAD")
        marker.unlink()
        wrapper.write_text(
            "#!/usr/bin/env python3\nimport os, sys, subprocess\nfrom pathlib import Path\n"
            + f"marker = Path({str(marker)!r})\n"
            + "if sys.argv[1] == 'push' and not marker.exists():\n    marker.touch()\n"
            + f"    subprocess.run([{real_git!r}, '-C', {str(competitor)!r}, 'push', 'origin', 'main'], check=True)\n"
            + f"os.execv({real_git!r}, [{real_git!r}, *sys.argv[1:]])\n"
        )
        stale = run("push-formula.sh", cwd=publisher, env={"FORMULA_PATH": "Formula/example.rb", "VERSION": "1.0.0", "COMMIT": "a" * 40, "PATH": retry_path})
        assert stale.returncode != 0
        assert "downgrade" in stale.stderr
        assert git(competitor, "ls-remote", "origin", "refs/heads/main").split()[0] == remote_head


def assert_update_policy() -> None:
    with tempfile.TemporaryDirectory() as directory:
        repo = Path(directory)
        git(repo, "init", "--initial-branch=main")
        configure(repo)
        (repo / "Formula").mkdir()
        target = repo / "Formula/example.rb"
        target.write_text(formula("1.2.3"))
        git(repo, "add", ".")
        git(repo, "commit", "-m", "baseline")
        baseline = git(repo, "rev-parse", "HEAD")
        for version, commit, override, allowed in [
            ("1.2.4", "b" * 40, "false", True),
            ("1.2.3", "a" * 40, "false", True),
            ("1.2.3", "b" * 40, "false", False),
            ("1.2.2", "a" * 40, "false", False),
            ("1.2.3-rc.1", "a" * 40, "false", False),
            ("1.3.0-rc.1", "b" * 40, "false", True),
            ("nightly", "a" * 40, "false", False),
            ("1.2.2", "b" * 40, "true", True),
            ("1.2.3", "b" * 40, "true", True),
            ("nightly", "b" * 40, "true", True),
        ]:
            git(repo, "reset", "--hard", baseline)
            target.write_text(formula(version, commit, "updated description"))
            output = repo / "output"
            output.write_text("")
            result = run("commit-formula.sh", cwd=repo, env={"FORMULA_PATH": "Formula/example.rb", "FORMULA": "example", "VERSION": version, "COMMIT": commit, "ALLOW_DOWNGRADE": override, "GITHUB_OUTPUT": str(output)})
            assert (result.returncode == 0) == allowed, (version, commit, override, result.stderr)
            if not allowed:
                assert git(repo, "rev-parse", "HEAD") == baseline
                assert not git(repo, "diff", "--cached")
                assert "allow-downgrade=true" in result.stderr

        # Bootstrap an existing source Formula, then a new Formula.
        git(repo, "reset", "--hard", baseline)
        target.write_text('class Example < Formula\n  version "1.2.3"\n  url "https://github.com/owner/repo/archive/' + "a" * 40 + '.tar.gz"\nend\n')
        git(repo, "add", ".")
        git(repo, "commit", "-m", "legacy source")
        target.write_text(formula("1.2.3", description="metadata edit"))
        result = run("commit-formula.sh", cwd=repo, env={"FORMULA_PATH": "Formula/example.rb", "FORMULA": "example", "VERSION": "1.2.3", "COMMIT": "a" * 40, "GITHUB_OUTPUT": str(repo / "output")})
        assert result.returncode == 0, result.stderr
        target.unlink()
        git(repo, "add", ".")
        git(repo, "commit", "-m", "remove formula")
        target.write_text(formula("nightly"))
        result = run("commit-formula.sh", cwd=repo, env={"FORMULA_PATH": "Formula/example.rb", "FORMULA": "example", "VERSION": "nightly", "COMMIT": "a" * 40, "GITHUB_OUTPUT": str(repo / "output")})
        assert result.returncode == 0, result.stderr


def main() -> None:
    assert_preflight()
    assert_credentials()
    assert_commit_and_retry()
    assert_update_policy()
    print("Homebrew publish scripts passed")


if __name__ == "__main__":
    main()
