"""Build-time, hash-guarded source overlay; never used as a runtime launcher."""
import argparse
import hashlib
import importlib.metadata
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile


def sha(data):
    return hashlib.sha256(data).hexdigest()


def text_bytes(path):
    return path.read_bytes().replace(b"\r\n", b"\n")


def install(preimages, expected_archive, patches, destination):
    manifest = json.loads((patches / "release-manifest.json").read_text())
    assert manifest["passed"] is True
    assert sha(preimages.read_bytes()) == expected_archive, "Upstream archive mismatch"
    expected = manifest["reproduced_files"]
    with tempfile.TemporaryDirectory(prefix="governor-image-build-") as temporary:
        work = Path(temporary)
        with tarfile.open(preimages) as archive:
            for member in archive.getmembers():
                assert member.isfile(), "Only explicit upstream files are allowed"
                assert member.name in expected, "Unexpected upstream file"
            archive.extractall(work, filter="data")
        before = {name: text_bytes(work / name) if (work / name).exists() else None
                  for name in expected}
        for name, data in before.items():
            if data is not None:
                (work / name).write_bytes(data)
        for entry in manifest["patches"]:
            patch = patches / entry["name"]
            assert patch.parent == patches and sha(patch.read_bytes()) == entry["sha256"]
            subprocess.run(["git", "apply", "--check", str(patch)], cwd=work, check=True)
            subprocess.run(["git", "apply", str(patch)], cwd=work, check=True)
        actual = {path.relative_to(work).as_posix(): sha(path.read_bytes())
                  for path in work.rglob("*") if path.is_file()}
        assert actual == expected, "Patched source differs from validated release"
        runtime = {}
        for name in expected:
            if not name.startswith("python/sglang/"):
                continue
            relative = Path(name.removeprefix("python/sglang/"))
            assert not relative.is_absolute() and ".." not in relative.parts
            target = destination / relative
            if before[name] is None:
                assert not target.exists(), "Unexpected existing module: " + name
            else:
                assert target.is_file() and text_bytes(target) == before[name], "Official image preimage mismatch: " + name
            runtime[name] = target
        # Validate every installed preimage before modifying any installed file.
        for name, target in runtime.items():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(work / name, target)
            assert sha(target.read_bytes()) == expected[name]
        return {"upstream": manifest["upstream"], "runtime_files": len(runtime),
                "source_hashes": {name: expected[name] for name in runtime},
                "preimages_sha256": expected_archive,
                "release_manifest_sha256": sha((patches / "release-manifest.json").read_bytes())}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--preimages", type=Path, required=True)
    parser.add_argument("--preimages-sha256", required=True)
    parser.add_argument("--patches", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args()
    assert importlib.metadata.version("sglang") == "0.5.20"
    spec = importlib.util.find_spec("sglang")
    assert spec is not None and spec.origin
    receipt = install(args.preimages, args.preimages_sha256, args.patches.resolve(), Path(spec.origin).parent)
    args.receipt.parent.mkdir(parents=True, exist_ok=True)
    args.receipt.write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps({"installed_runtime_files": receipt["runtime_files"], "upstream": receipt["upstream"]}))
