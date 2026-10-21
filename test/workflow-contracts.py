#!/usr/bin/env python3
"""Public workflow wiring, independent of YAML layout and incidental steps."""
import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def load(name):
    result = subprocess.run(["ruby", "-ryaml", "-rjson", "-e",
        "puts JSON.generate(YAML.load_file(ARGV.fetch(0), aliases: true))", str(ROOT / name)],
        capture_output=True, text=True, check=True)
    return json.loads(result.stdout)


def contract(model):
    return model.get("on", model.get("true"))["workflow_call"]


def all_steps(model):
    return [step for job in model["jobs"].values() for step in job.get("steps", [])]


def check(model):
    public = contract(model)
    inputs = public["inputs"]
    assert model["permissions"] == {"contents": "read"}
    assert {"commit", "spec-commit", "tap-repository", "tap-branch", "spec-path"} <= inputs.keys()
    assert not {"formula", "ref"} & inputs.keys()
    assert inputs["tap-repository"]["required"] is True
    assert "default" not in inputs["tap-repository"]
    assert "formula" in public["outputs"]
    formula = next(step for step in all_steps(model) if step.get("uses") == "./automation/internal/formula")
    assert formula["with"]["repository"] == "${{ github.repository }}"
    gate = model["jobs"]["homebrew-check"]
    assert gate["if"] == "always()"
    assert {"generate", "validate"} <= set(gate["needs"])
    return formula


def main():
    read = load(".github/workflows/check.yml")
    formula = check(read)
    assert not contract(read).get("secrets")
    assert formula["with"]["validation-mode"] == "spec"
    assert formula["with"]["commit"] == "${{ inputs.commit || github.sha }}"
    assert formula["with"]["spec-commit"] == "${{ inputs.spec-commit || inputs.commit || github.sha }}"
    publish = load(".github/workflows/publish.yml")
    formula = check(publish)
    public = contract(publish)
    assert {"commit", "version", "allow-downgrade"} <= public["inputs"].keys()
    assert public["inputs"]["commit"]["required"] and public["inputs"]["version"]["required"]
    assert {"tap_token", "tap_deploy_key"} <= public["secrets"].keys()
    assert {"version", "state"} <= public["outputs"].keys()
    assert formula["with"]["commit"] == "${{ inputs.commit }}"
    assert publish["jobs"]["generate"]["outputs"]["formula-sha256"] == "${{ steps.digest.outputs.sha256 }}"
    assert {"generate", "homebrew-check"} <= set(publish["jobs"]["publish"]["needs"])
    for role in ["validate", "publish"]:
        job = publish["jobs"][role]
        environments = [job.get("env", {}), *(step.get("env", {}) for step in job["steps"])]
        assert any(env.get("FORMULA_SHA256") == "${{ needs.generate.outputs.formula-sha256 }}" for env in environments)
    action = load("internal/formula/action.yml")
    assert {"commit", "spec-commit"} <= action["inputs"].keys()
    assert "formula" not in action["inputs"]
    assert "formula" in action["outputs"]
    release = load(".github/workflows/release.yml")
    assert any(step.get("uses", "").startswith("releaseway/actions@") for step in all_steps(release))
    # A formatting change is harmless; removing the write gate is not.
    import copy
    reordered = copy.deepcopy(read)
    reordered["jobs"] = dict(reversed(list(reordered["jobs"].items())))
    check(reordered)
    broken = copy.deepcopy(read)
    broken["jobs"]["homebrew-check"]["needs"] = ["generate"]
    try:
        check(broken)
    except AssertionError:
        pass
    else:
        raise AssertionError("Missing native validation dependency was accepted")
    print("Homebrew workflow contracts passed")


if __name__ == "__main__":
    main()
