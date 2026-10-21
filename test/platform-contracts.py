"""All nonempty platform sets: canonical assets, matrix, requirements and real Homebrew parsing."""
from hashlib import sha256
from pathlib import Path
import itertools
import json
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
TARGETS = ["macos-arm64", "macos-x86_64", "linux-arm64", "linux-x86_64"]
RUNNERS = ["macos-latest", "macos-15-intel", "ubuntu-24.04-arm", "ubuntu-24.04"]
SHA = "a" * 40

with tempfile.TemporaryDirectory(prefix="releaseway-platforms-") as directory:
    root = Path(directory)
    tap, source, fake_bin = [root / name for name in ["tap", "source", "bin"]]
    for path in [tap, source, fake_bin]:
        path.mkdir()
    archive = root / "source.tar.gz"
    archive.write_bytes(b"source archive for platform contract")
    assets = {target: "pkg-" + target + ".tar.gz" for target in TARGETS}
    release = dict(immutable=True, draft=False, published_at="2026-10-21T00:00:00Z",
                   assets=[dict(name=assets[target], state="uploaded", digest="sha256:" + sha256(target.encode()).hexdigest()) for target in TARGETS])
    release_json = root / "release.json"
    release_json.write_text(json.dumps(release))
    gh = fake_bin / "gh"
    gh.write_text("#!" + sys.executable + "\nimport os,sys,json\nfrom pathlib import Path\n"
                  "print(json.dumps({'sha':os.environ['SOURCE_COMMIT']}) if '/commits/' in sys.argv[-1] else Path(os.environ['FAKE_RELEASE']).read_text())\n")
    gh.chmod(0o755)
    curl = fake_bin / "curl"
    curl.write_text("#!" + sys.executable + "\nimport os,sys,shutil\n"
                    "shutil.copyfile(os.environ['FAKE_ARCHIVE'],sys.argv[sys.argv.index('-o')+1])\n")
    curl.chmod(0o755)
    env = dict(os.environ, TAP_PATH=str(tap), SOURCE_PATH=str(source), SOURCE_COMMIT=SHA, SPEC_COMMIT=SHA,
               SPEC_PATH="formula.yml", SPEC=str(source / "formula.yml"), VERSION="1.2.3", REPOSITORY="owner/example",
               VALIDATION_MODE="release", FAKE_RELEASE=str(release_json), FAKE_ARCHIVE=str(archive),
               PATH=str(fake_bin) + os.pathsep + os.environ["PATH"])
    def render(spec, error=None, raw=None):
        (source / "formula.yml").write_text(json.dumps(spec) if raw is None else raw)
        output = root / "output"
        output.write_text("")
        result = subprocess.run(["ruby", str(ROOT / "internal/formula/render.rb")],
                                env={**env, "GITHUB_OUTPUT": str(output)}, capture_output=True, text=True)
        if error:
            assert result.returncode != 0 and error in result.stderr, result.stderr
            assert output.read_text() == ""
            return None
        assert result.returncode == 0, result.stderr
        return dict(line.split("=", 1) for line in output.read_text().splitlines())

    base = dict(name="platform-probe", desc="Platform contract fixture", license="MIT",
                install='bin.install "example"', test='system "#{bin}/example", "--version"')
    manifest = []
    for size in range(1, 5):
        for subset in itertools.combinations(TARGETS, size):
            declared = list(subset)
            for distribution in ["source", "github-release"]:
                name = "platform-" + str(len(manifest)) + "-" + distribution
                spec = {**base, "name": name, "platforms": declared}
                if distribution == "github-release":
                    spec["distribution"] = dict(type=distribution, tag="v{version}", assets={key: assets[key] for key in declared})
                output = render(spec)
                model = json.loads(output["platform-model"])
                assert model["mode"] == "explicit" and model["declared"] == model["targets"] == declared
                expected_sources = ({"source": dict(url=f"https://github.com/owner/example/archive/{SHA}.tar.gz",
                                                     sha256=sha256(archive.read_bytes()).hexdigest())} if distribution == "source" else
                                    {target: dict(url="https://github.com/owner/example/releases/download/v1.2.3/" + assets[target],
                                                  sha256=sha256(target.encode()).hexdigest()) for target in declared})
                assert model["sources"] == expected_sources
                expected_matrix = [dict(platform=target, runner=RUNNERS[TARGETS.index(target)]) for target in declared]
                assert json.loads(output["runner-matrix"]) == model["runner-matrix"] == expected_matrix
                path = tap / "Formula" / (name + ".rb")
                original = path.read_bytes()
                reversed_output = render({**spec, "platforms": list(reversed(declared))})
                assert path.read_bytes() == original
                assert json.loads(reversed_output["runner-matrix"]) == expected_matrix
                manifest.append(dict(name=name, path=str(path), targets=declared, distribution=distribution, sources=expected_sources))

    for value in [None, [], "linux-x86_64", ["unknown"], ["linux-x86_64", "linux-x86_64"], [True]]:
        render({**base, "platforms": value}, "platforms")
    render(base, "duplicate platforms", raw=json.dumps(base)[:-1] + ', "platforms":["linux-x86_64"], "platforms":["macos-arm64"]}')
    spec = {**base, "platforms": ["linux-x86_64"], "distribution": dict(type="github-release", tag="v1.2.3", assets={})}
    render(spec, "missing declared platform mappings")
    spec["distribution"]["assets"] = {key: assets[key] for key in ["linux-x86_64", "macos-arm64"]}
    render(spec, "undeclared platform mappings")
    # Mapping exists, but the uploaded release asset does not.
    spec["distribution"]["assets"] = {"linux-x86_64": "absent.tar.gz"}
    render(spec, "missing asset absent.tar.gz")
    # A universal archive may map to multiple targets; every target remains required.
    spec["platforms"] = ["macos-arm64", "linux-x86_64"]
    spec["distribution"]["assets"] = {key: assets["macos-arm64"] for key in spec["platforms"]}
    universal = render(spec)
    assert len(json.loads(universal["runner-matrix"])) == 2
    legacy = render(base)
    assert json.loads(legacy["platform-model"])["declared"] is None
    assert json.loads(legacy["runner-matrix"]) == [dict(platform="macos-arm64", runner="macos-latest")]
    assert "depends_on :" not in (tap / "Formula/platform-probe.rb").read_text()

    manifest_path = root / "manifest.json"
    manifest_path.write_text(json.dumps(manifest))
    if os.environ.get("RELEASEWAY_BREW_AUDIT") == "1":
        for distribution in ["source", "github-release"]:
            native = next(entry for entry in manifest if entry["distribution"] == distribution and entry["targets"] == ["linux-x86_64"])
            audit_tap = root / ("audit-tap-" + distribution)
            (audit_tap / "Formula").mkdir(parents=True)
            shutil.copyfile(native["path"], audit_tap / "Formula" / (native["name"] + ".rb"))
            result = subprocess.run(["bash", str(ROOT / "internal/scripts/validate-formula.sh")],
                                    env={**os.environ, "HOMEBREW_NO_AUTO_UPDATE": "1", "TAP_PATH": str(audit_tap),
                                         "FORMULA": native["name"], "VALIDATION_MODE": "spec"}, capture_output=True, text=True)
            assert result.returncode == 0, result.stdout + result.stderr
        print("Native Linux x86_64 source/prebuilt Formula audit passed in isolated temporary taps")
    if os.environ.get("RELEASEWAY_BREW_CONTRACTS") == "1":
        assert shutil.which("brew"), "Homebrew required for parsing contracts"
        result = subprocess.run(["brew", "ruby", "-rformulary", str(ROOT / "test/platform-homebrew.rb"), str(manifest_path)],
                                env={**os.environ, "HOMEBREW_NO_AUTO_UPDATE": "1"}, capture_output=True, text=True)
        assert result.returncode == 0, result.stdout + result.stderr
        print(result.stdout.strip())
    else:
        print("Homebrew parsing not requested; run RELEASEWAY_BREW_CONTRACTS=1 for actual loader coverage")
print("30 platform models, canonical order, asset boundaries and legacy defaults passed")
