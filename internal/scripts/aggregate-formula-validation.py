"""Require exactly one current native receipt for every declared validation target."""
import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import re
import tempfile

TARGETS = ["macos-arm64", "macos-x86_64", "linux-arm64", "linux-x86_64"]


def require(value, message):
    if not value:
        raise ValueError(message)


def unique(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "duplicate JSON key")
        result[key] = value
    return result


def parse(raw):
    return json.loads(raw, object_pairs_hook=unique)


def architecture(value):
    return {"arm64": "arm64", "aarch64": "arm64", "x86_64": "x86_64", "amd64": "x86_64", "x64": "x86_64"}.get(value.lower())


def scope_from_env():
    return dict(invocation_id=os.environ["INVOCATION_ID"], automation_repository=os.environ["AUTOMATION_REPOSITORY"],
                automation_sha=os.environ["AUTOMATION_SHA"], caller_repository=os.environ["GITHUB_REPOSITORY"],
                caller_sha=os.environ["GITHUB_SHA"], run_id=int(os.environ["GITHUB_RUN_ID"]),
                run_attempt=int(os.environ["GITHUB_RUN_ATTEMPT"]))


def aggregate(context, scope, receipts):
    expected = context["required_platforms"]
    require(expected and expected == [target for target in TARGETS if target in expected], "invalid target set/order")
    require(set(context["runners"]) == set(expected), "runner set differs from declaration")
    require(context["declaration_mode"] in ("explicit", "legacy"), "invalid declaration mode")
    require(context["declared_platforms"] == expected if context["declaration_mode"] == "explicit" else context["declared_platforms"] is None,
            "invalid declared platforms")
    for key in ["formula_sha256", "spec_sha256"]:
        require(re.fullmatch(r"[a-f0-9]{64}", context[key]), "invalid content digest")
    for key in ["source_commit", "spec_commit"]:
        require(re.fullmatch(r"[a-f0-9]{40}", context[key]), "invalid source/spec commit")
    for key in ["version_scheme", "revision"]:
        require(type(context[key]) is int and context[key] >= 0, "invalid package tuple")
    require(re.fullmatch(r"[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}", scope["invocation_id"]), "invalid invocation")
    for key in ["automation_sha", "caller_sha"]:
        require(re.fullmatch(r"[a-f0-9]{40}", scope[key]), "invalid workflow commit")
    for key in ["run_id", "run_attempt"]:
        require(type(scope[key]) is int and scope[key] > 0, "invalid run/attempt")
    require(len(receipts) == len(expected), "native receipt set incomplete or duplicated")
    seen = {}
    for receipt in receipts:
        require(set(receipt) == set(context) | set(scope) | {"schema", "kind", "platform", "requested_runner", "runtime", "checks", "completed_at"},
                "unexpected native receipt fields")
        require(type(receipt.get("schema")) is int and receipt["schema"] == 1 and receipt["kind"] == "homebrew-native", "invalid native receipt schema")
        require(all(receipt.get(key) == value and type(receipt.get(key)) is type(value) for key, value in {**context, **scope}.items()),
                "native receipt identity differs from invocation")
        platform = receipt["platform"]
        require(platform in expected and platform not in seen, "unexpected or duplicate native platform")
        os_name, arch = platform.split("-", 1)
        require(receipt["requested_runner"] == context["runners"][platform], "runner label mismatch")
        runtime = receipt["runtime"]
        runtime_keys = {"os", "uname_arch", "hardware_arch", "brew_host_cpu", "ruby_platform", "brew_version", "runner_os", "runner_arch", "image_os", "image_version"}
        require(set(runtime) == runtime_keys and all(isinstance(runtime[key], str) and runtime[key] for key in runtime_keys),
                "invalid native runtime fields")
        require(runtime["os"] == {"macos": "Darwin", "linux": "Linux"}[os_name]
                and runtime["runner_os"].lower() == {"macos": "macos", "linux": "linux"}[os_name], "native OS mismatch")
        require(all(architecture(runtime[key]) == arch for key in ["uname_arch", "hardware_arch", "brew_host_cpu", "runner_arch"]), "native architecture mismatch")
        require(runtime["brew_version"].startswith("Homebrew ") and runtime["ruby_platform"]
                and runtime["image_os"] and runtime["image_version"], "native runtime metadata missing")
        require(receipt["checks"] == dict(audit="success", install="success", test="success"), "native audit/install/test incomplete")
        require(isinstance(receipt["completed_at"], str) and datetime.fromisoformat(receipt["completed_at"].replace("Z", "+00:00")).utcoffset() is not None, "completion timezone missing")
        seen[platform] = receipt
    require(set(seen) == set(expected), "missing required native platform")
    return dict(schema=1, kind="homebrew-validation", **scope, **context,
                validated_platforms=expected, native_receipts=[seen[platform] for platform in expected])


def atomic_json(path, value):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".homebrew-validation-", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write((json.dumps(value, sort_keys=True, indent=2) + "\n").encode())
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--receipts", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    try:
        directory = Path(args.receipts).resolve()
        files = sorted(path for path in directory.rglob("*") if path.is_file())
        require(1 <= len(files) <= 4, "unexpected native receipt file count")
        require(all(path.name == "receipt.json" and not path.is_symlink()
                    and path.resolve().is_relative_to(directory) and path.stat().st_size <= 100 * 1024 for path in files),
                "invalid native receipt path or size")
        context = parse(os.environ["FORMULA_CONTEXT"])
        result = aggregate(context, scope_from_env(), [parse(path.read_bytes()) for path in files])
        atomic_json(Path(args.output), result)
        with open(os.environ["GITHUB_OUTPUT"], "a") as stream:
            stream.write("validated-platforms=" + json.dumps(result["validated_platforms"], separators=(",", ":")) + "\n")
        print("Verified " + str(len(files)) + " native platform receipts")
    except (ValueError, KeyError, TypeError, OSError) as error:
        parser.exit(1, "Native aggregation failed: " + str(error) + "\n")
