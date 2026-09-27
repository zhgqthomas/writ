import importlib.util
import pathlib
import subprocess
import sys
import tempfile
import unittest

SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "release_policy.py"
spec = importlib.util.spec_from_file_location("release_policy", SCRIPT)
policy = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = policy
spec.loader.exec_module(policy)


class ReleasePolicyTests(unittest.TestCase):
    def test_only_version_tags_can_be_published(self):
        for invalid in ("main", "vbackup", "v01.2.3", "v1.2", "v1.2.3-01", "v1.2.3\nlatest"):
            with self.subTest(tag=invalid), self.assertRaises(ValueError):
                policy.parse_tag(invalid)

    def test_prereleases_never_promote_latest(self):
        self.assertFalse(policy.promotes_latest("v2.0.0-rc.1", ["v1.9.0"]))
        self.assertTrue(policy.promotes_latest("v1.9.0", ["v2.0.0-rc.1"]))

    def test_older_release_cannot_roll_latest_back(self):
        self.assertFalse(policy.promotes_latest("v1.2.3", ["v1.2.4", "v1.2.3"]))
        self.assertTrue(policy.promotes_latest("v1.10.0", ["v1.9.9", "v1.10.0", "unrelated"]))

    def test_existing_registry_version_prevents_rollback_after_tag_deletion(self):
        self.assertFalse(policy.promotes_latest("v1.2.3", ["v1.2.3"], "v1.2.4"))
        self.assertTrue(policy.promotes_latest("v1.2.5", ["v1.2.5"], "v1.2.4"))
        self.assertFalse(policy.promotes_latest("v1.2.5", ["v1.2.5"], "unknown"))

    def test_image_tags_match_release_and_owner(self):
        tags = policy.image_tags("My-Org", "coordinator", "v1.2.3", "a" * 40, ["v1.2.3"])
        self.assertEqual(tags, [
            "ghcr.io/my-org/writ-coordinator:v1.2.3",
            "ghcr.io/my-org/writ-coordinator:1.2.3",
            "ghcr.io/my-org/writ-coordinator:sha-" + "a" * 40,
            "ghcr.io/my-org/writ-coordinator:latest",
        ])

    def test_old_and_prerelease_tags_still_get_versioned_images(self):
        for tag in ("v1.0.0", "v3.0.0-rc.1"):
            refs = policy.image_tags("owner", "doc-extract", tag, "b" * 40, ["v2.0.0"])
            self.assertEqual(len(refs), 3)
            self.assertFalse(any(ref.endswith(":latest") for ref in refs))

    def test_resolve_requires_tag_to_identify_checked_out_commit(self):
        with tempfile.TemporaryDirectory() as directory:
            def git(*args):
                return subprocess.check_output(["git", "-C", directory, *args], text=True).strip()
            git("init", "-q")
            git("-c", "user.name=CI Test", "-c", "user.email=ci@example.invalid",
                "commit", "--allow-empty", "-qm", "first")
            git("tag", "v1.2.3")
            first = git("rev-parse", "HEAD")
            self.assertEqual(policy.resolve_ref(directory, "refs/tags/v1.2.3", True), (first, "v1.2.3"))
            with self.assertRaises(ValueError):
                policy.resolve_ref(directory, "refs/heads/main", True)
            git("-c", "user.name=CI Test", "-c", "user.email=ci@example.invalid",
                "commit", "--allow-empty", "-qm", "second")
            with self.assertRaises(ValueError):
                policy.resolve_ref(directory, "v1.2.3", True)
            self.assertEqual(policy.resolve_ref(directory, "HEAD", False), (git("rev-parse", "HEAD"), ""))
            policy.validate_tag_sha(directory, "v1.2.3", first)
            git("tag", "-f", "v1.2.3")
            with self.assertRaises(ValueError):
                policy.validate_tag_sha(directory, "v1.2.3", first)
            git("tag", "-d", "v1.2.3")
            with self.assertRaises(ValueError):
                policy.validate_tag_sha(directory, "v1.2.3", first)


if __name__ == "__main__":
    unittest.main()
