#!/usr/bin/env python3
import os
from pathlib import Path
import subprocess
import shutil
import tempfile
from hashlib import sha256

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "internal" / "scripts"


def formula(version: str, commit: str = "a" * 40, description: str = "example", scheme: int | None = None, revision: int | None = None) -> str:
    scheme_line = f"  version_scheme {scheme}\n" if scheme is not None else ""
    revision_line = f"  revision {revision}\n" if revision is not None else ""
    return f'# releaseway-version: {version}\n# releaseway-source-commit: {commit}\nclass Example < Formula\n  desc "{description}"\n{scheme_line}{revision_line}end\n'


def run(script: str, *, cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    merged = dict(os.environ)
    merged.update(env)
    if script in ("commit-formula.sh", "push-formula.sh") and "FORMULA_SHA256" not in merged:
        path = cwd / merged["FORMULA_PATH"]
        if path.is_file():
            merged["FORMULA_SHA256"] = sha256(path.read_bytes()).hexdigest()
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
        # Detached maintenance can outlive assertions and race temporary cleanup.
        ("gc.auto", "0"),
        ("maintenance.auto", "false"),
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
        for spec_commit in ["", "b" * 40, "B" * 40]:
            result = run("validate-publish-inputs.sh", cwd=cwd, env={**base, "SPEC_COMMIT": spec_commit})
            assert result.returncode == 0, result.stderr
        for spec_commit in ["main", "b" * 39, "b" * 41, "b" * 40 + "\n"]:
            result = run("validate-publish-inputs.sh", cwd=cwd, env={**base, "SPEC_COMMIT": spec_commit})
            assert result.returncode != 0, spec_commit

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


def assert_commit_and_retry(*, scheme: int | None = None, newer_scheme: int | None = None, newer_version: str = "2.0.0", newer_revision: int | None = None) -> None:
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
        configure(remote)
        subprocess.run(["git", "clone", str(remote), str(seed)], check=True, capture_output=True)
        configure(seed)
        (seed / "Formula").mkdir()
        (seed / "Formula/example.rb").write_text(formula("11.0.4" if scheme else "0.9.0"))
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

        (publisher / "Formula/example.rb").write_text(formula("1.0.0", scheme=scheme))
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
        assert (verify / "Formula/example.rb").read_text() == formula("1.0.0", scheme=scheme)
        assert (verify / "Formula/other.rb").read_text() == "other\n"

        subjects = git(verify, "log", "--format=%s", "-3").splitlines()
        assert "chore: update example to 1.0.0" in subjects
        assert "chore: update other" in subjects

        push_script = (SCRIPTS / "push-formula.sh").read_text()
        assert "force" not in push_script.lower()

        # A newer publication on the same Formula must be checked before rebase/push.
        git(competitor, "pull", "--ff-only")
        (competitor / "Formula/example.rb").write_text(formula(newer_version, ("a" if newer_revision else "b") * 40, scheme=newer_scheme, revision=newer_revision))
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


def assert_scheme_policy() -> None:
    cases = [
        # A scheme increase starts a new version sequence, including new sources.
        (None, "11.0.4", 1, "1.2.0", "b", False, True),
        (0, "11.0.4", 1, "1.2.0", "b", False, True),
        (1, "1.2.0", 1, "1.2.1", "b", False, True),
        (1, "1.2.0", 0, "11.0.4", "a", False, False),
        (1, "1.2.0", None, "11.0.4", "a", False, False),
        (1, "1.2.1", 1, "1.2.0", "a", False, False),
        (None, "1.2.0", 1, "1.2.0", "a", False, True),
        (None, "1.2.0", 1, "1.2.0", "b", False, True),
        (1, "1.2.0", 1, "1.2.0", "a", False, True),
        (1, "1.2.0", 1, "1.2.0", "b", False, False),
        (1, "1.2.0", 0, "11.0.4", "b", True, True),
        (1, "1.2.1", 1, "1.2.0", "b", True, True),
        (1, "1.2.0", 1, "1.2.0", "b", True, True),
    ]
    for old_scheme, old_version, new_scheme, new_version, source, override, allowed in cases:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            git(repo, "init", "--initial-branch=main")
            configure(repo)
            (repo / "Formula").mkdir()
            target = repo / "Formula/example.rb"
            target.write_text(formula(old_version, scheme=old_scheme))
            git(repo, "add", ".")
            git(repo, "commit", "-m", "baseline")
            baseline = git(repo, "rev-parse", "HEAD")
            # Metadata edits remain allowed for equal scheme/version/source.
            target.write_text(formula(new_version, source * 40, "updated description", new_scheme))
            output = repo / "output"
            output.write_text("")
            env = {"FORMULA_PATH": "Formula/example.rb", "FORMULA": "example", "VERSION": new_version,
                   "COMMIT": source * 40, "ALLOW_DOWNGRADE": str(override).lower(), "GITHUB_OUTPUT": str(output)}
            result = run("commit-formula.sh", cwd=repo, env=env)
            assert (result.returncode == 0) == allowed, (old_scheme, old_version, new_scheme, new_version, result.stderr)
            if allowed:
                assert output.read_text() == "changed=true\n"
            else:
                assert git(repo, "rev-parse", "HEAD") == baseline
                assert not git(repo, "diff", "--cached")

    # Scheme-only changes must publish even with identical version/source/metadata.
    with tempfile.TemporaryDirectory() as directory:
        repo = Path(directory)
        git(repo, "init", "--initial-branch=main")
        configure(repo)
        target = repo / "example.rb"
        target.write_text(formula("1.2.0"))
        git(repo, "add", ".")
        git(repo, "commit", "-m", "baseline")
        baseline = git(repo, "rev-parse", "HEAD")
        output = repo / "output"
        env = {"FORMULA_PATH": "example.rb", "FORMULA": "example", "VERSION": "1.2.0",
               "COMMIT": "a" * 40, "GITHUB_OUTPUT": str(output)}
        target.write_text(formula("1.2.0", scheme=1))
        result = run("commit-formula.sh", cwd=repo, env=env)
        assert result.returncode == 0, result.stderr
        assert output.read_text() == "changed=true\n"

        # Existing handwritten Formulas expose scheme through their Ruby stanza.
        git(repo, "reset", "--hard", baseline)
        target.write_text('class Example < Formula\n  version "11.0.4"\n  version_scheme 1\nend\n')
        git(repo, "add", "example.rb")
        git(repo, "commit", "-m", "legacy formula")
        target.write_text(formula("12.0.0"))
        result = run("commit-formula.sh", cwd=repo, env={**env, "VERSION": "12.0.0"})
        assert result.returncode != 0
        assert "downgrade" in result.stderr

        target.write_text(formula("1.2.0", scheme=2))
        result = run("commit-formula.sh", cwd=repo, env=env)
        assert result.returncode == 0, result.stderr

        # An explicit malformed candidate is never silently treated as scheme zero.
        for declaration in ['-1', '1.5', '"1"', 'nil', '010', '1\n  version_scheme 2']:
            target.write_text(formula("1.2.0", scheme=1).replace("version_scheme 1", f"version_scheme {declaration}"))
            result = run("commit-formula.sh", cwd=repo, env={**env, "ALLOW_DOWNGRADE": "true"})
            assert result.returncode != 0
            assert "version_scheme must be a non-negative integer literal" in result.stderr


def assert_revision_policy() -> None:
    # version, revision, source, scheme, override, allowed are domain expectations.
    cases = [
        ("1.2.3", None, "1.2.3", 1, "a", None, None, False, True),
        ("1.2.3", 2, "1.2.3", 1, "a", None, None, False, False),
        ("1.2.3", 1, "1.2.3", 1, "a", None, None, False, True),
        ("1.2.3", 4, "1.2.4", None, "b", None, None, False, True),
        ("1.2.3", 4, "1.2.4", 2, "b", None, None, False, True),
        ("1.2.3", 0, "1.2.3", 1, "b", None, None, False, False),
        ("1.2.3", 0, "1.2.3", 1, "b", None, None, True, True),
        ("11.0.0", 4, "1.2.3", 0, "b", None, 1, False, True),
        ("1.2.3", 0, "11.0.0", 9, "a", 1, 0, False, False),
        ("1.2.3", 2, "1.2.3", 1, "a", None, None, True, True),
    ]
    for old_version, old_revision, new_version, new_revision, source, old_scheme, new_scheme, override, allowed in cases:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            git(repo, "init", "--initial-branch=main")
            configure(repo)
            target = repo / "example.rb"
            target.write_text(formula(old_version, revision=old_revision, scheme=old_scheme))
            git(repo, "add", ".")
            git(repo, "commit", "-m", "baseline")
            baseline = git(repo, "rev-parse", "HEAD")
            target.write_text(formula(new_version, source * 40, "packaging change", new_scheme, new_revision))
            output = repo / "output"
            env = dict(FORMULA_PATH="example.rb", FORMULA="example", VERSION=new_version, COMMIT=source * 40,
                       GITHUB_OUTPUT=str(output), ALLOW_DOWNGRADE=str(override).lower())
            result = run("commit-formula.sh", cwd=repo, env=env)
            assert (result.returncode == 0) == allowed, (cases, result.stderr)
            if allowed:
                package_version = new_version + (f"_{new_revision}" if new_revision else "")
                assert git(repo, "log", "-1", "--format=%s") == f"chore: update example to {package_version}"
                output.write_text("")
                rerun = run("commit-formula.sh", cwd=repo, env=env)
                assert rerun.returncode == 0 and output.read_text() == "changed=false\n"
            else:
                assert git(repo, "rev-parse", "HEAD") == baseline and not git(repo, "diff", "--cached")

    with tempfile.TemporaryDirectory() as directory:
        repo = Path(directory)
        git(repo, "init", "--initial-branch=main")
        configure(repo)
        target = repo / "example.rb"
        target.write_text("placeholder")
        git(repo, "add", ".")
        git(repo, "commit", "-m", "seed")
        output = repo / "output"
        env = dict(FORMULA_PATH="example.rb", FORMULA="example", VERSION="1.2.3", COMMIT="a" * 40, GITHUB_OUTPUT=str(output))
        # Initial publication may explicitly choose a nonzero revision.
        target.unlink()
        git(repo, "add", ".")
        git(repo, "commit", "-m", "no formula")
        target.write_text(formula("1.2.3", revision=3))
        assert run("commit-formula.sh", cwd=repo, env=env).returncode == 0
        for malformed in ['-1', '1.5', '"1"', 'nil', '01', '1 + 1', '1\n  revision 2']:
            target.write_text(formula("1.2.3", revision=1).replace("revision 1", "revision " + malformed))
            invalid = run("commit-formula.sh", cwd=repo, env={**env, "ALLOW_DOWNGRADE": "true"})
            assert invalid.returncode != 0 and "revision must be a non-negative integer literal" in invalid.stderr
        # Malformed old handwritten revisions require an explicit override.
        target.write_text(formula("1.2.3", revision=1).replace("revision 1", "revision calculate_revision"))
        git(repo, "add", ".")
        git(repo, "commit", "-m", "legacy invalid revision")
        target.write_text(formula("1.2.4", revision=0))
        env["VERSION"] = "1.2.4"
        assert run("commit-formula.sh", cwd=repo, env=env).returncode != 0
        assert run("commit-formula.sh", cwd=repo, env={**env, "ALLOW_DOWNGRADE": "true"}).returncode == 0


def assert_package_declaration_scope() -> None:
    with tempfile.TemporaryDirectory() as directory:
        repo = Path(directory)
        git(repo, "init", "--initial-branch=main")
        configure(repo)
        target = repo / "example.rb"
        snippets = '''  caveats <<~EOS
    revision 999
    version_scheme 999
  EOS
  def install
    revision = "source commit"
  end
  test do
    revision = "test output"
    version_scheme = "application setting"
  end
'''
        def content(revision):
            return formula("1.2.3", revision=revision).removesuffix("end\n") + snippets + "end\n"
        target.write_text(content(None))
        git(repo, "add", ".")
        git(repo, "commit", "-m", "nested product Ruby")
        env = dict(os.environ, FORMULA_PATH="example.rb", VERSION="1.2.3", COMMIT="a" * 40)
        validator = str(SCRIPTS / "validate-formula-update.rb")
        for revision, package in [(None, "1.2.3"), (1, "1.2.3_1")]:
            target.write_text(content(revision))
            result = subprocess.run(["ruby", validator, "--package-version"], cwd=repo, env=env, capture_output=True, text=True)
            assert result.returncode == 0 and result.stdout.strip() == package, result.stderr
            result = subprocess.run(["ruby", validator, "HEAD"], cwd=repo, env=env, capture_output=True, text=True)
            assert result.returncode == 0, result.stderr
        git(repo, "add", ".")
        git(repo, "commit", "-m", "packaging revision one")
        target.write_text(content(None))
        result = subprocess.run(["ruby", validator, "HEAD"], cwd=repo, env=env, capture_output=True, text=True)
        assert result.returncode != 0 and "revision downgrade" in result.stderr
        target.write_text(formula("1.2.3", revision=1).replace("  revision 1", "  if true\n    revision 1\n  end"))
        result = subprocess.run(["ruby", validator, "--package-version"], cwd=repo, env=env, capture_output=True, text=True)
        assert result.returncode != 0 and "integer literal" in result.stderr


def main() -> None:
    assert_package_declaration_scope()
    assert_revision_policy()
    assert_validated_bytes()
    assert_preflight()
    assert_credentials()
    assert_commit_and_retry()
    assert_commit_and_retry(scheme=1, newer_scheme=2, newer_version="0.5.0")
    assert_commit_and_retry(scheme=1, newer_scheme=1, newer_version="1.0.1")
    assert_commit_and_retry(newer_version="1.0.0", newer_revision=2)
    assert_update_policy()
    assert_scheme_policy()
    print("Homebrew publish scripts passed")


def assert_validated_bytes() -> None:
    for same_formula in [True, False]:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            remote, publisher, competitor = [root / name for name in ["remote.git", "publisher", "competitor"]]
            git(root, "init", "--bare", "--initial-branch=main", str(remote))
            git(root, "clone", str(remote), str(publisher))
            configure(publisher)
            target = publisher / "Formula/example.rb"
            target.parent.mkdir()
            text = formula("1.0.0") + '\n' * 12 + 'def install\n  bin.install "example"\nend\n'
            target.write_text(text)
            git(publisher, "add", "."); git(publisher, "commit", "-m", "baseline"); git(publisher, "push", "origin", "main")
            git(root, "clone", str(remote), str(competitor)); configure(competitor)
            target.write_text(text.replace("1.0.0", "1.2.3"))
            expected = sha256(target.read_bytes()).hexdigest()
            git(publisher, "add", "."); git(publisher, "commit", "-m", "candidate")
            env = dict(os.environ, FORMULA_PATH="Formula/example.rb")
            env.pop("FORMULA_SHA256", None)
            missing = subprocess.run(["ruby", str(SCRIPTS / "verify-formula-bytes.rb")], cwd=publisher, env=env, capture_output=True, text=True)
            assert missing.returncode != 0 and "required from the validated artifact" in missing.stderr
            for invalid_digest in ["", "0" * 64]:
                invalid = run("push-formula.sh", cwd=publisher, env={"FORMULA_PATH": "Formula/example.rb", "FORMULA_SHA256": invalid_digest, "VERSION": "1.2.3", "COMMIT": "a" * 40})
                assert invalid.returncode != 0
            original_candidate = target.read_bytes()
            target.write_bytes(original_candidate + b"# unvalidated local edit\n")
            invalid = run("push-formula.sh", cwd=publisher, env={"FORMULA_PATH": "Formula/example.rb", "FORMULA_SHA256": expected, "VERSION": "1.2.3", "COMMIT": "a" * 40})
            assert invalid.returncode != 0 and "validated artifact" in invalid.stderr
            target.write_bytes(original_candidate)
            if same_formula:
                (competitor / "Formula/example.rb").write_text(text.replace('bin.install "example"', 'bin.install "unvalidated"'))
            else:
                (competitor / "Formula/other.rb").write_text("other\n")
            git(competitor, "add", "."); git(competitor, "commit", "-m", "concurrent"); git(competitor, "push", "origin", "main")
            before = git(competitor, "rev-parse", "HEAD")
            result = run("push-formula.sh", cwd=publisher, env={"FORMULA_PATH": "Formula/example.rb", "FORMULA_SHA256": expected, "VERSION": "1.2.3", "COMMIT": "a" * 40})
            if same_formula:
                assert result.returncode != 0
                assert "validated artifact" in result.stderr, result.stderr
                assert git(competitor, "ls-remote", "origin", "refs/heads/main").split()[0] == before
            else:
                assert result.returncode == 0, result.stderr
                git(competitor, "pull", "--ff-only")
                assert sha256((competitor / "Formula/example.rb").read_bytes()).hexdigest() == expected


if __name__ == "__main__":
    main()
