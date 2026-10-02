#!/usr/bin/env python3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def assert_check_contract() -> None:
    text = (ROOT / ".github/workflows/check.yml").read_text()
    public = text.split("permissions:", 1)[0]
    inputs = public.split("outputs:", 1)[0]

    assert "permissions:\n  contents: read" in text
    assert "secrets:" not in inputs
    assert "      formula:" not in inputs
    assert "      ref:" not in inputs
    assert "      commit:" not in inputs
    assert "      version:" not in inputs
    for optional_input in ["tap-repository", "tap-branch", "spec-path"]:
        assert f"      {optional_input}:" in inputs

    assert "outputs:\n      formula:" in public
    assert "repository: ${{ github.repository }}" in text
    assert "ref: ${{ github.sha }}" in text
    assert "commit: ${{ github.sha }}" in text
    assert "uses: ./automation/internal/formula" in text
    assert "formula: ${{ steps.formula.outputs.formula }}" in text
    assert "path: tap/${{ steps.formula.outputs.formula-path }}" in text
    assert "FORMULA: ${{ needs.generate.outputs.formula }}" in text
    assert "validation-mode: spec" in text
    assert "group: homebrew-check-${{ github.repository }}-${{ inputs.spec-path }}-${{ github.sha }}" in text
    assert "\n  homebrew-check:" in text


def assert_publish_contract() -> None:
    text = (ROOT / ".github/workflows/publish.yml").read_text()
    public = text.split("permissions:", 1)[0]
    inputs = public.split("secrets:", 1)[0]

    assert "permissions:\n  contents: read" in text
    for required in ["commit", "version"]:
        assert f"      {required}:" in inputs
    for optional_input in ["tap-repository", "tap-branch", "spec-path"]:
        assert f"      {optional_input}:" in inputs

    assert "      formula:" not in inputs
    assert "      ref:" not in inputs
    assert "      tap_token:" in public
    assert "      tap_deploy_key:" in public
    for output in ["formula", "version", "state"]:
        assert f"      {output}:" in public

    assert "COMMIT: ${{ inputs.commit }}" in text
    assert "ref: ${{ inputs.commit }}" in text
    assert "commit: ${{ inputs.commit }}" in text
    assert "formula: ${{ steps.formula.outputs.formula }}" in text
    assert "path: tap/${{ steps.formula.outputs.formula-path }}" in text
    assert "FORMULA: ${{ needs.generate.outputs.formula }}" in text
    assert "state=published" in text
    assert "state=unchanged" in text
    assert "group: homebrew-publish-${{ inputs.tap-repository }}-${{ github.repository }}-${{ inputs.spec-path }}\n" in text
    assert "      allow-downgrade:" in inputs
    assert text.count("ALLOW_DOWNGRADE: ${{ inputs.allow-downgrade }}") == 3
    assert "\n  homebrew-check:" in text
    assert "\n  publish:" in text


def assert_internal_formula_contract() -> None:
    action = (ROOT / "internal/formula/action.yml").read_text()
    generator = (ROOT / "internal/formula/generate.sh").read_text() + (ROOT / "internal/formula/render.rb").read_text()
    assert 'ruby "$script_dir/render.rb"' in generator
    inputs = action.split("outputs:", 1)[0]

    assert "  formula:" not in inputs
    assert "  commit:" in inputs
    assert "FORMULA:" not in action
    assert "COMMIT: ${{ inputs.commit }}" in action
    assert 'required_string(spec, "name")' in generator
    assert '"formula=#{formula}"' in generator
    assert '"commit=#{ENV.fetch(\"SOURCE_COMMIT\")}"' in generator
    assert "source checkout does not match commit" in generator


def assert_repository_contract() -> None:
    release = (ROOT / ".github/workflows/release.yml").read_text()
    ci = (ROOT / ".github/workflows/test.yml").read_text()

    assert not (ROOT / ".github/workflows/update-policy.yml").exists()
    assert not (ROOT / "internal/policy").exists()
    assert not (ROOT / "test/update-policy.py").exists()
    assert not (ROOT / ".github/workflows/release-fixture-e2e.yml").exists()
    assert not (ROOT / "test/fixtures").exists()

    assert "releaseway/actions@" in release
    assert "gh release create" not in release
    for workflow in ["check.yml", "publish.yml"]:
        assert f".github/workflows/{workflow}" in release
    assert "update-policy.yml" not in release

    for test in [
        "test/formula-generator.py",
        "test/workflow-contracts.py",
        "test/publish-scripts.py",
        "test/setup-deploy-key.py",
    ]:
        assert test in ci
    assert "test/update-policy.py" not in ci
    assert "internal/policy" not in ci


def main() -> None:
    assert_check_contract()
    assert_publish_contract()
    for workflow in ["check.yml", "publish.yml"]:
        text = (ROOT / ".github/workflows" / workflow).read_text()
        block = text.split("      tap-repository:", 1)[1].split("      tap-branch:", 1)[0]
        assert "required: true" in block
        assert "default:" not in block
    assert_internal_formula_contract()
    assert_repository_contract()
    print("Homebrew workflow contracts passed")


if __name__ == "__main__":
    main()
