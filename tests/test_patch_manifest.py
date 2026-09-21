import hashlib
import json
import re
import unittest
from pathlib import Path


class PatchManifestTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parents[1]
        self.patch_dir = root / "patches" / "sglang" / "v0.5.20"
        self.manifest = json.loads((self.patch_dir / "manifest.json").read_text())

    def test_governor_patch_hash_and_files_match_manifest(self):
        patch = self.manifest["patches"][0]
        patch_bytes = (self.patch_dir / patch["path"]).read_bytes()
        digest = hashlib.sha256(patch_bytes).hexdigest()
        self.assertEqual(digest, patch["sha256"])
        changed_files = [
            left.decode("utf-8")
            for left, right in re.findall(
                rb"^diff --git a/(.+?) b/(.+?)$", patch_bytes, re.MULTILINE
            )
            if left == right
        ]
        self.assertEqual(changed_files, patch["changed_files"])
        self.assertEqual(self.manifest["schema"], "phala.governor-hooks.v2")
        self.assertEqual(self.manifest["component"]["version"], "0.2.0")
        self.assertEqual(self.manifest["component"]["abi_version"], 4)

    def test_v3_frozen_hook_bytes_remain_retrievable(self):
        historical = self.manifest["historical"]["v3"]
        patch_bytes = (self.patch_dir / historical["patch"]).read_bytes()
        self.assertEqual(hashlib.sha256(patch_bytes).hexdigest(), historical["hook_sha256"])
        frozen_manifest = json.loads(
            (self.patch_dir / historical["manifest"]).read_text()
        )
        self.assertEqual(
            frozen_manifest["patches"][0]["sha256"], historical["hook_sha256"]
        )


if __name__ == "__main__":
    unittest.main()
