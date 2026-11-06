"""Release paths cannot block the app on a protected Downloads permission prompt."""
from __future__ import annotations

from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from tests._isolation import sandbox_env

_SANDBOX = sandbox_env()

from gateway import release_publisher as rp, release_rollout, story_releases  # noqa: E402


class ReleaseStaging(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="gateway-staging-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.home = self.root / "home"
        self.home.mkdir()
        self.data = self.root / "custom-profile"
        self.data.mkdir()
        self.data_patch = patch.object(rp.config, "DATA_DIR", self.data)
        self.home_patch = patch.object(Path, "home", return_value=self.home)
        self.data_patch.start()
        self.home_patch.start()
        self.addCleanup(self.data_patch.stop)
        self.addCleanup(self.home_patch.stop)
        self.download = self.home / "Downloads" / "Hub.dmg"

    def test_owned_directory_follows_active_profile_and_resolves_filename(self) -> None:
        directory = rp.artifact_directory()
        self.assertEqual(directory, self.data / "releases")
        self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
        artifact = directory / "Hub.dmg"
        artifact.write_bytes(b"release")
        self.assertEqual(rp.resolve_artifact("Hub.dmg"), artifact.resolve())
        self.assertEqual(rp.resolve_artifact(artifact), artifact.resolve())

    def test_existing_absolute_staging_path_remains_supported(self) -> None:
        artifact = self.root / "existing-deployment" / "Hub.dmg"
        artifact.parent.mkdir()
        artifact.write_bytes(b"release")
        self.assertEqual(rp.resolve_artifact(artifact), artifact.resolve())

    def test_downloads_refused_before_metadata_lock_hash_sign_or_rollout(self) -> None:
        calls = (
            lambda: rp.publish(artifact_path=self.download, version="0.6.222", build="build"),
            lambda: rp._publish(artifact_path=self.download, version="0.6.222", build="build"),
            lambda: release_rollout.start(artifact_path=self.download, version="0.6.222", build="build"),
        )
        with patch.object(Path, "resolve", side_effect=AssertionError("protected metadata read")), \
             patch.object(rp, "_hash_file") as hash_file, \
             patch.object(rp, "_node_request") as signer, \
             patch.object(rp, "_push_lock") as lock, \
             patch.object(release_rollout, "_write") as write_rollout:
            for call in calls:
                with self.subTest(call=call), self.assertRaises(rp.ReleaseError) as raised:
                    call()
                self.assertEqual(raised.exception.status, 400)
                self.assertEqual(raised.exception.code, "RELEASE_ARTIFACT_NEEDS_STAGING")
                self.assertIn(str(self.data / "releases"), raised.exception.message)
            hash_file.assert_not_called()
            signer.assert_not_called()
            lock.__enter__.assert_not_called()
            write_rollout.assert_not_called()

    def test_differently_cased_downloads_refuses_before_metadata(self) -> None:
        with patch.object(Path, "resolve", side_effect=AssertionError("protected metadata read")):
            with self.assertRaises(rp.ReleaseError) as raised:
                rp.resolve_artifact(self.home / "downloads" / "Hub.dmg")
        self.assertEqual(raised.exception.code, "RELEASE_ARTIFACT_NEEDS_STAGING")

    def test_alias_into_downloads_is_refused(self) -> None:
        self.download.parent.mkdir()
        self.download.write_bytes(b"release")
        alias = self.root / "alias.dmg"
        alias.symlink_to(self.download)
        with self.assertRaises(rp.ReleaseError) as raised:
            rp.resolve_artifact(alias)
        self.assertEqual(raised.exception.code, "RELEASE_ARTIFACT_NEEDS_STAGING")

    def test_story_refuses_downloads_before_reading_or_copying(self) -> None:
        with patch.object(story_releases, "read_versions") as versions, \
             patch.object(story_releases.shutil, "copyfile") as copy_file:
            with self.assertRaises(story_releases.StoryReleaseError) as raised:
                story_releases.publish(self.download)
            self.assertEqual(raised.exception.code, "RELEASE_ARTIFACT_NEEDS_STAGING")
            versions.assert_not_called()
            copy_file.assert_not_called()

    def test_old_catchup_state_in_downloads_fails_closed_without_opening(self) -> None:
        state = {"release": {"version": "0.6.222"}, "artifact_path": str(self.download)}
        with patch.object(rp, "_hash_file") as hash_file:
            path, reason = rp._catchup_artifact(state, "0.6.222")
        self.assertIsNone(path)
        self.assertIn("Downloads", reason)
        hash_file.assert_not_called()

    def test_paced_rollout_persists_the_owned_filename_as_an_absolute_path(self) -> None:
        artifact = rp.artifact_directory() / "Hub.dmg"
        artifact.write_bytes(b"release")
        with patch.object(release_rollout, "_path", return_value=self.root / "rollout.json"), \
             patch.object(release_rollout, "plan", return_value=[("controller", "Location")]), \
             patch.object(release_rollout, "_ensure_thread"):
            state = release_rollout.start(artifact_path="Hub.dmg", version="0.6.222", build="build")
        self.assertEqual(state["artifact_path"], str(artifact.resolve()))

    def test_story_reads_a_filename_from_the_owned_folder(self) -> None:
        artifact = rp.artifact_directory() / "Story.dmg"
        artifact.write_bytes(b"release")
        with patch.object(story_releases, "read_versions", return_value=("1.4.382", "build")) as versions, \
             patch.object(rp, "identity", return_value={"fingerprint": "test-publisher"}), \
             patch.object(rp, "sign_descriptor", return_value="test-signature"):
            answer = story_releases.publish("Story.dmg")
        versions.assert_called_once_with(artifact.resolve())
        self.assertEqual(answer["descriptor"]["bytes"], len(b"release"))
        self.assertTrue(artifact.is_file())

    def test_identity_reports_the_owned_folder_without_opening_a_release(self) -> None:
        from gateway import admin
        with patch.object(rp, "identity", return_value={}), \
             patch.object(rp, "last_push", return_value={}), \
             patch.object(rp, "_hash_file") as hash_file:
            answer = admin.release_identity()
        self.assertEqual(answer["artifact_directory"], str(self.data / "releases"))
        self.assertTrue((self.data / "releases").is_dir())
        hash_file.assert_not_called()


if __name__ == "__main__":
    unittest.main()
