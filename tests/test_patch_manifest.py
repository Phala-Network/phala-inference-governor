import hashlib
import json
import unittest
from pathlib import Path


class PatchManifestTests(unittest.TestCase):
    def test_governor_patch_hash_matches_manifest(self):
        root = Path(__file__).resolve().parents[1]
        patch_dir = root / "patches" / "sglang" / "v0.5.20"
        manifest = json.loads((patch_dir / "manifest.json").read_text())
        patch = manifest["patches"][0]
        digest = hashlib.sha256((patch_dir / patch["path"]).read_bytes()).hexdigest()
        self.assertEqual(digest, patch["sha256"])


if __name__ == "__main__":
    unittest.main()
