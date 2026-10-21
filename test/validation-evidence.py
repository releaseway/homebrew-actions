"""Native evidence boundaries with mocked OS/Brew processes; public acceptance supplies real installs."""
import copy
from hashlib import sha256
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
A = runpy.run_path(str(ROOT / "internal/scripts/aggregate-formula-validation.py"))
SCOPE = dict(invocation_id="11111111-1111-4111-8111-111111111111", automation_repository="releaseway/homebrew-actions",
             automation_sha="a" * 40, caller_repository="owner/example", caller_sha="b" * 40, run_id=7, run_attempt=2)
TARGETS = ["macos-arm64", "macos-x86_64", "linux-arm64", "linux-x86_64"]
RUNNERS = ["macos-latest", "macos-15-intel", "ubuntu-24.04-arm", "ubuntu-24.04"]


def context(targets):
    return dict(formula="example", formula_sha256=sha256(b"formula bytes").hexdigest(), source_commit="c" * 40,
                spec_commit="d" * 40, spec_path=".github/homebrew/formula.yml", spec_sha256="e" * 64,
                version="1.2.3", version_scheme=0, revision=1, declaration_mode="explicit",
                declared_platforms=targets, required_platforms=targets,
                runners={target: RUNNERS[TARGETS.index(target)] for target in targets})


def receipt(ctx, target):
    os_name, arch = target.split("-", 1)
    return dict(**ctx, **SCOPE, schema=1, kind="homebrew-native", platform=target,
                requested_runner=ctx["runners"][target], completed_at="2026-10-21T00:00:00Z",
                checks=dict(audit="success", install="success", test="success"),
                runtime=dict(os="Darwin" if os_name == "macos" else "Linux", uname_arch=arch, hardware_arch=arch,
                             brew_host_cpu=arch, ruby_platform=arch + "-test", brew_version="Homebrew 7.0",
                             runner_os="macOS" if os_name == "macos" else "Linux", runner_arch=arch,
                             image_os="unknown", image_version="unknown"))


class AggregateTests(unittest.TestCase):
    def setUp(self):
        self.ctx = context(TARGETS)
        self.receipts = [receipt(self.ctx, target) for target in TARGETS]

    def test_exact_set_and_canonical_native_receipts(self):
        result = A["aggregate"](self.ctx, SCOPE, list(reversed(self.receipts)))
        self.assertEqual(result["validated_platforms"], TARGETS)
        self.assertEqual([r["platform"] for r in result["native_receipts"]], TARGETS)

    def test_missing_duplicate_unexpected_attempt_or_invocation(self):
        for records in [self.receipts[:-1], self.receipts + [self.receipts[0]], [self.receipts[0]] * 4]:
            with self.assertRaises(ValueError):
                A["aggregate"](self.ctx, SCOPE, records)
        changes = dict(invocation_id="22222222-2222-4222-8222-222222222222", run_attempt=1, run_id=True,
                       source_commit="f" * 40, spec_commit="f" * 40, spec_sha256="f" * 64,
                       formula_sha256="f" * 64, caller_sha="f" * 40, automation_sha="f" * 40, revision=0)
        for key, value in changes.items():
            bad = copy.deepcopy(self.receipts)
            bad[0][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                A["aggregate"](self.ctx, SCOPE, bad)

    def test_runtime_mismatch_incomplete_checks_and_extra_fields(self):
        for key, value in [("os", "Windows"), ("uname_arch", "x86_64"), ("hardware_arch", "x86_64"),
                           ("brew_host_cpu", "x86_64"), ("runner_arch", "X64"), ("brew_version", ""), ("image_os", True)]:
            bad = copy.deepcopy(self.receipts)
            bad[0]["runtime"][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                A["aggregate"](self.ctx, SCOPE, bad)
        for mutate in [lambda r: r["checks"].update(test="failure"), lambda r: r.update(GH_TOKEN="must-not-archive")]:
            bad = copy.deepcopy(self.receipts)
            mutate(bad[0])
            with self.assertRaises(ValueError):
                A["aggregate"](self.ctx, SCOPE, bad)

    def test_atomic_output_and_duplicate_json_rejected(self):
        with self.assertRaisesRegex(ValueError, "duplicate"):
            A["parse"]('{"run_id":7,"run_id":8}')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "out.json"
            A["atomic_json"](path, dict(state="good"))
            self.assertEqual(json.loads(path.read_text()), dict(state="good"))

    def test_workflow_scopes_artifacts_and_gates_native_evidence(self):
        workflow = (ROOT / ".github/workflows/publish.yml").read_text()
        self.assertIn("SecureRandom.uuid", workflow)
        self.assertIn("formula-artifact-name: ${{ steps.invocation.outputs.formula-artifact-name }}", workflow)
        self.assertIn("          name: ${{ steps.invocation.outputs.formula-artifact-name }}", workflow)
        self.assertEqual(workflow.count("artifact-ids: ${{ needs.generate.outputs.formula-artifact-id }}"), 2)
        self.assertIn("pattern: homebrew-native-${{ needs.generate.outputs.invocation-id }}-*", workflow)
        self.assertLess(workflow.index("Verify requested native runtime"), workflow.index("      - name: Validate Formula"))
        self.assertLess(workflow.index("Require complete current native receipt set"), workflow.index("\n  publish:"))
        self.assertIn("retention-days: 30", workflow)
        public = workflow.split("permissions:", 1)[0]
        for output in ["spec-commit", "revision", "formula-sha256", "validated-platforms", "validation-artifact-id"]:
            self.assertIn("      " + output + ":", public)

    def test_failed_skipped_cancelled_generation_or_native_job_blocks_publish(self):
        for generated in ["success", "failure", "skipped", "cancelled"]:
            for validated in ["success", "failure", "skipped", "cancelled"]:
                result = subprocess.run(["bash", str(ROOT / "internal/scripts/require-formula-validation.sh")],
                                        env={**os.environ, "GENERATE_RESULT": generated, "VALIDATE_RESULT": validated}, capture_output=True)
                self.assertEqual(result.returncode == 0, generated == validated == "success")

    def test_three_invocations_of_workflow_emit_distinct_formula_artifact_names(self):
        workflow = (ROOT / ".github/workflows/publish.yml").read_text()
        block = workflow.split("      - name: Identify this reusable workflow invocation\n", 1)[1].split("      - name: Freeze generated Formula bytes", 1)[0]
        command = "\n".join(line[10:] for line in block.split("        run: |\n", 1)[1].splitlines() if line.startswith("          "))
        names, ids = set(), set()
        with tempfile.TemporaryDirectory() as directory:
            for index in range(3):
                output = Path(directory) / str(index)
                result = subprocess.run(["bash", "-ec", command], env={**os.environ, "FORMULA": "example", "GITHUB_OUTPUT": str(output)}, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                values = dict(line.split("=", 1) for line in output.read_text().splitlines())
                self.assertEqual(values["formula-artifact-name"], "homebrew-publish-formula-example-" + values["id"])
                names.add(values["formula-artifact-name"])
                ids.add(values["id"])
        self.assertEqual(len(names), 3)
        self.assertEqual(len(ids), 3)

    def test_cli_reads_download_layout_and_preserves_output_on_missing_receipts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            receipts = root / "receipts"
            for index, item in enumerate(self.receipts):
                destination = receipts / ("artifact-" + str(index)) / "receipt.json"
                destination.parent.mkdir(parents=True)
                destination.write_text(json.dumps(item))
            output, github_output = root / "aggregate.json", root / "outputs"
            env = dict(os.environ, FORMULA_CONTEXT=json.dumps(self.ctx), INVOCATION_ID=SCOPE["invocation_id"],
                       AUTOMATION_REPOSITORY=SCOPE["automation_repository"], AUTOMATION_SHA=SCOPE["automation_sha"],
                       GITHUB_REPOSITORY=SCOPE["caller_repository"], GITHUB_SHA=SCOPE["caller_sha"],
                       GITHUB_RUN_ID="7", GITHUB_RUN_ATTEMPT="2", GITHUB_OUTPUT=str(github_output))
            command = [sys.executable, str(ROOT / "internal/scripts/aggregate-formula-validation.py"), "--receipts", str(receipts), "--output", str(output)]
            result = subprocess.run(command, env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            frozen = output.read_bytes()
            self.assertEqual(json.loads(output.read_text())["validated_platforms"], TARGETS)
            (receipts / "artifact-0/receipt.json").unlink()
            failed = subprocess.run(command, env=env, capture_output=True)
            self.assertNotEqual(failed.returncode, 0)
            self.assertEqual(output.read_bytes(), frozen)


class RecorderTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        for name, code in {
            "uname": "print(os.environ['FAKE_OS'] if sys.argv[1]=='-s' else os.environ['FAKE_ARCH'])",
            "sysctl": "assert sys.argv[1:]==['-in','sysctl.proc_translated']; print(os.environ['FAKE_TRANSLATED']); sys.exit(int(os.environ.get('FAKE_SYSCTL_EXIT','0')))",
            "brew": "print('Homebrew 7.0' if sys.argv[1]=='--version' else json.dumps(dict(host_cpu=os.environ['FAKE_BREW_ARCH'],ruby_platform='test-ruby-platform')))",
        }.items():
            path = self.bin / name
            path.write_text("#!" + sys.executable + "\nimport os,sys,json\n" + code + "\n")
            path.chmod(0o755)
        (self.root / "formula.rb").write_bytes(b"formula bytes")

    def run_recorder(self, target, phase, overrides=None):
        os_name, arch = target.split("-", 1)
        ctx = context([target])
        env = dict(os.environ, PATH=str(self.bin) + os.pathsep + os.environ["PATH"], FORMULA_CONTEXT=json.dumps(ctx),
                   INVOCATION_ID=SCOPE["invocation_id"], AUTOMATION_REPOSITORY=SCOPE["automation_repository"],
                   AUTOMATION_SHA=SCOPE["automation_sha"], GITHUB_REPOSITORY=SCOPE["caller_repository"],
                   GITHUB_SHA=SCOPE["caller_sha"], GITHUB_RUN_ID="7", GITHUB_RUN_ATTEMPT="2",
                   PLATFORM=target, REQUESTED_RUNNER=ctx["runners"][target],
                   FORMULA_PATH=str(self.root / "formula.rb"), NATIVE_RUNTIME=str(self.root / "runtime.json"),
                   NATIVE_RECEIPT=str(self.root / "receipt.json"), VALIDATION_SUCCEEDED="true",
                   RUNNER_OS="macOS" if os_name == "macos" else "Linux", RUNNER_ARCH=arch,
                   FAKE_OS="Darwin" if os_name == "macos" else "Linux", FAKE_ARCH=arch,
                   FAKE_TRANSLATED="" if target == "macos-x86_64" else "0", FAKE_BREW_ARCH=arch, GH_TOKEN="never-record-this")
        env.update(overrides or {})
        return subprocess.run(["ruby", str(ROOT / "internal/scripts/record-formula-validation.rb"), phase],
                              env=env, capture_output=True, text=True)

    def test_all_requested_platforms_and_process_identity(self):
        for target in TARGETS:
            preflight = self.run_recorder(target, "--preflight")
            self.assertEqual(preflight.returncode, 0, preflight.stderr)
            result = self.run_recorder(target, "--record")
            self.assertEqual(result.returncode, 0, result.stderr)
            raw = (self.root / "receipt.json").read_text()
            self.assertNotIn("never-record-this", raw)
            receipt_ = json.loads(raw)
            self.assertEqual(A["aggregate"](context([target]), SCOPE, [receipt_])["validated_platforms"], [target])
        if os.environ.get("RELEASEWAY_NATIVE_PREFLIGHT") == "1":
            result = self.run_recorder("linux-x86_64", "--preflight", dict(PATH=os.environ["PATH"]))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(json.loads((self.root / "runtime.json").read_text())["runtime"]["brew_version"].startswith("Homebrew "))

    def test_wrong_os_arch_rosetta_brew_process_and_failed_checks_block(self):
        for overrides in [dict(FAKE_OS="Linux"), dict(FAKE_ARCH="x86_64"), dict(FAKE_TRANSLATED="1"), dict(FAKE_TRANSLATED="unexpected"), dict(FAKE_SYSCTL_EXIT="2"),
                          dict(FAKE_BREW_ARCH="x86_64"), dict(RUNNER_ARCH="X64")]:
            result = self.run_recorder("macos-arm64", "--preflight", overrides)
            self.assertNotEqual(result.returncode, 0, overrides)
        self.assertFalse((self.root / "receipt.json").exists())
        self.assertEqual(self.run_recorder("linux-x86_64", "--preflight").returncode, 0)
        self.assertNotEqual(self.run_recorder("linux-x86_64", "--record", dict(VALIDATION_SUCCEEDED="false")).returncode, 0)
        self.assertFalse((self.root / "receipt.json").exists())


if __name__ == "__main__":
    unittest.main()
