#!/usr/bin/env python3
"""Offline regression tests for wow-addon-updater (stdlib, no dependencies).

The suite covers package/path safety, transactional installation, persistence,
multi-root and multi-flavor behavior, update failure reporting, and the CLI
state-management regressions fixed during development.

Run:  python3 -m unittest test_update_addons.py -v
"""

import io
import json
import os
import tempfile
import unittest
import urllib.error
import zipfile
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import update_addons as u


def make_flavor(tmp, name="_retail_", installed=(), state=None, version="11.0.5"):
    """Build a fake flavor on disk and return a real Flavor object.

    `installed` is the set of top-level AddOn folder names to create; `state`
    (if given) is written as the .addon-updater.json state file. A one-product
    .build.info gives the flavor a resolvable client version.
    """
    wow_root = tmp / "World of Warcraft"
    wow_root.mkdir(parents=True, exist_ok=True)
    (wow_root / ".build.info").write_text(
        "Version!STRING:0|Product!STRING:0\n" + f"{version}.12345|wow\n")
    addons = wow_root / name / "Interface" / "AddOns"
    addons.mkdir(parents=True, exist_ok=True)
    for folder in installed:
        (addons / folder).mkdir(exist_ok=True)
    if state is not None:
        u.save_state(addons, state)
    return u.Flavor(wow_root, wow_root / name)


def make_package(tmp, folders=("Addon",), wrapper=None):
    """Create a fetched-package-shaped directory with valid addon folders."""
    source = tmp / "package"
    base = source / wrapper if wrapper else source
    base.mkdir(parents=True)
    for folder in folders:
        addon = base / folder
        addon.mkdir()
        (addon / f"{folder}.toc").write_text("## Interface: 110005\n")
    return source


class TempDirTestCase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.tmp = Path(self.temp_dir.name)


class ClassifyInstalledTest(TempDirTestCase):
    def setUp(self):
        super().setUp()
        self.registry = {
            "AdiBags": {"strategy": "github-release", "repo": "AdiAddons/AdiBags",
                        "asset": "^x$"},
        }

    def test_multi_folder_addon_not_mislabeled(self):
        """AdiBags ships AdiBags + AdiBags_Config; the second folder must be
        attributed to AdiBags, not reported as a separate unmanaged addon."""
        state = {"AdiBags": {"version": "v1.10.29",
                             "folders": ["AdiBags", "AdiBags_Config"]}}
        flavor = make_flavor(self.tmp, installed=["AdiBags", "AdiBags_Config"],
                             state=state)
        rows = u.gather_status([flavor], self.registry)
        names = {r["name"]: r for r in rows}
        self.assertIn("AdiBags", names)
        self.assertTrue(names["AdiBags"]["in_registry"])
        # The secondary folder must NOT appear as its own row.
        self.assertNotIn("AdiBags_Config", names)

    def test_genuinely_unmanaged_folder_is_flagged(self):
        """A folder with no registry entry is correctly "not in registry"."""
        flavor = make_flavor(self.tmp, installed=["AdiBags", "RandoAddon"],
                             state={"AdiBags": {"version": "v1", "folders": ["AdiBags"]}})
        rows = {r["name"]: r for r in u.gather_status([flavor], self.registry)}
        self.assertTrue(rows["AdiBags"]["in_registry"])
        self.assertFalse(rows["RandoAddon"]["in_registry"])

    def test_classify_matches_status_split(self):
        """classify_installed and gather_status agree on the managed/unmanaged
        split — the invariant that keeps update and list consistent."""
        state = {"AdiBags": {"version": "v1", "folders": ["AdiBags", "AdiBags_Config"]}}
        flavor = make_flavor(self.tmp, installed=["AdiBags", "AdiBags_Config", "RandoAddon"],
                             state=state)
        managed, unmanaged, covered = u.classify_installed(flavor, self.registry, state)
        self.assertEqual(managed, ["AdiBags"])
        self.assertEqual(unmanaged, ["RandoAddon"])
        self.assertEqual(covered, {"AdiBags", "AdiBags_Config"})
        rows = {r["name"]: r["in_registry"] for r in u.gather_status([flavor], self.registry)}
        self.assertEqual(rows, {"AdiBags": True, "RandoAddon": False})

    def test_state_folders_manage_addon_when_registry_name_differs(self):
        state = {"RepoName": {"version": "v1", "folders": ["ActualAddon"]}}
        flavor = make_flavor(self.tmp, installed=["ActualAddon"], state=state)
        registry = {"RepoName": {"strategy": "github-release", "repo": "a/b",
                                 "asset": "^x$"}}
        managed, unmanaged, covered = u.classify_installed(flavor, registry, state)
        self.assertEqual(managed, ["RepoName"])
        self.assertEqual(unmanaged, [])
        self.assertEqual(covered, {"ActualAddon"})


class RemoveAddonTest(TempDirTestCase):
    def setUp(self):
        super().setUp()
        self.registry_path = self.tmp / "addons.json"
        self.registry = {
            "AdiBags": {"strategy": "github-release", "repo": "AdiAddons/AdiBags",
                        "asset": "^x$"},
            "Questie": {"strategy": "github-release", "repo": "Questie/Questie",
                        "asset": "^y$"},
        }
        u.save_registry(self.registry, self.registry_path)

    def test_remove_persists_registry_drop(self):
        """The real Bug #2: removing an addon must drop it from the registry
        file on disk, not just the in-memory dict."""
        state = {"AdiBags": {"version": "v1", "folders": ["AdiBags", "AdiBags_Config"]}}
        flavor = make_flavor(self.tmp, installed=["AdiBags", "AdiBags_Config"], state=state)
        u.remove_addon("AdiBags", [flavor], self.registry,
                       drop_registry=True, registry_path=self.registry_path)
        on_disk = json.loads(self.registry_path.read_text())
        self.assertNotIn("AdiBags", on_disk)
        self.assertIn("Questie", on_disk)  # unrelated entry untouched

    def test_remove_only_deletes_recorded_folders(self):
        """Safety invariant: only recorded folders are deleted; an unrelated
        sibling directory survives."""
        state = {"AdiBags": {"version": "v1", "folders": ["AdiBags", "AdiBags_Config"]}}
        flavor = make_flavor(self.tmp,
                             installed=["AdiBags", "AdiBags_Config", "RandoAddon"],
                             state=state)
        u.remove_addon("AdiBags", [flavor], self.registry,
                       drop_registry=False, registry_path=self.registry_path)
        remaining = {p.name for p in flavor.addons_dir.iterdir() if p.is_dir()}
        self.assertEqual(remaining, {"RandoAddon"})

    def test_remove_dry_run_changes_nothing(self):
        state = {"AdiBags": {"version": "v1", "folders": ["AdiBags"]}}
        flavor = make_flavor(self.tmp, installed=["AdiBags"], state=state)
        u.remove_addon("AdiBags", [flavor], self.registry, drop_registry=True,
                       dry_run=True, registry_path=self.registry_path)
        self.assertTrue((flavor.addons_dir / "AdiBags").is_dir())
        self.assertIn("AdiBags", json.loads(self.registry_path.read_text()))

    def test_keep_registry_removes_files_only(self):
        state = {"AdiBags": {"version": "v1", "folders": ["AdiBags"]}}
        flavor = make_flavor(self.tmp, installed=["AdiBags"], state=state)
        u.remove_addon("AdiBags", [flavor], self.registry, drop_registry=False,
                       registry_path=self.registry_path)
        self.assertFalse((flavor.addons_dir / "AdiBags").exists())
        self.assertIn("AdiBags", json.loads(self.registry_path.read_text()))

    def test_flavor_scoped_remove_drops_registry_only_with_last_copy(self):
        first = make_flavor(self.tmp / "first", installed=["AdiBags"],
                            state={"AdiBags": {"version": "v1", "folders": ["AdiBags"]}})
        second = make_flavor(self.tmp / "second", installed=["AdiBags"],
                             state={"AdiBags": {"version": "v1", "folders": ["AdiBags"]}})
        _, dropped = u.remove_addon(
            "AdiBags", [first], self.registry, registry_path=self.registry_path,
            all_known_flavors=[first, second])
        self.assertFalse(dropped)
        self.assertIn("AdiBags", self.registry)
        self.assertTrue((second.addons_dir / "AdiBags").is_dir())

        _, dropped = u.remove_addon(
            "AdiBags", [second], self.registry, registry_path=self.registry_path,
            all_known_flavors=[first, second])
        self.assertTrue(dropped)
        self.assertNotIn("AdiBags", json.loads(self.registry_path.read_text()))

    def test_unsafe_state_folder_is_rejected_before_deletion(self):
        sentinel = self.tmp / "sentinel"
        sentinel.mkdir()
        flavor = make_flavor(
            self.tmp / "unsafe", installed=[],
            state={"AdiBags": {"version": "v1", "folders": [str(sentinel)]}})
        with self.assertRaisesRegex(ValueError, "unsafe path"):
            u.remove_addon("AdiBags", [flavor], self.registry,
                           registry_path=self.registry_path)
        self.assertTrue(sentinel.is_dir())


class ResolveInstallTargetTest(TempDirTestCase):
    def test_existing_registry_name_is_pure(self):
        registry = {"AdiBags": {"strategy": "github-release", "repo": "a/b", "asset": "^x$"}}
        before = json.dumps(registry, sort_keys=True)
        name, spec, is_new = u.resolve_install_target("AdiBags", registry)
        self.assertEqual(name, "AdiBags")
        self.assertFalse(is_new)
        self.assertEqual(json.dumps(registry, sort_keys=True), before)

    def test_new_repo_slug_does_not_mutate_registry(self):
        """Bug #2 root cause: resolving a fresh owner/repo must not add it to
        the registry — the caller owns add + persist after a successful fetch."""
        registry = {}
        # Stub the network + spec derivation so the test stays offline.
        orig_http, orig_suggest = u.http_json, u.suggest_registry_spec
        u.http_json = lambda url: {"full_name": "AdiAddons/AdiBags",
                                   "default_branch": "master"}
        u.suggest_registry_spec = lambda repo: {"strategy": "github-branch",
                                                "repo": repo["full_name"],
                                                "branch": "master",
                                                "package_as": "AdiBags"}
        try:
            name, spec, is_new = u.resolve_install_target("AdiAddons/AdiBags", registry)
        finally:
            u.http_json, u.suggest_registry_spec = orig_http, orig_suggest
        self.assertEqual(name, "AdiBags")
        self.assertTrue(is_new)
        self.assertEqual(registry, {})  # untouched


class PinnedUpdateTest(TempDirTestCase):
    def test_pinned_addon_skipped_by_update(self):
        """A pinned addon must be reported pinned and never fetched."""
        registry = {"AdiBags": {"strategy": "github-release", "repo": "a/b",
                                "asset": "^x$", "pin": True}}
        state = {"AdiBags": {"version": "v1.10.29", "folders": ["AdiBags"]}}
        flavor = make_flavor(self.tmp, installed=["AdiBags"], state=state)

        # If update tried to resolve/fetch, this stub would flip the flag.
        touched = []
        orig = u.resolve_latest_version
        u.resolve_latest_version = lambda spec: touched.append(1) or "v2"
        try:
            cache = {}
            with redirect_stdout(io.StringIO()):  # update_flavor prints a tree
                u.update_flavor(flavor, registry, cache, self.tmp, dry_run=True)
        finally:
            u.resolve_latest_version = orig
        self.assertEqual(touched, [])  # never consulted the network for a pin


class PackageSafetyTest(TempDirTestCase):
    def test_safe_child_rejects_absolute_and_parent_paths(self):
        root = self.tmp / "addon"
        root.mkdir()
        for unsafe in (str(self.tmp / "outside"), "../outside", "."):
            with self.subTest(unsafe=unsafe):
                with self.assertRaisesRegex(ValueError, "unsafe path"):
                    u.safe_child(root, unsafe)

    def test_malicious_pkgmeta_cannot_delete_outside_package(self):
        sentinel = self.tmp / "sentinel"
        sentinel.mkdir()
        archive = self.tmp / "fixture.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("repo-root/Addon.toc", "## Interface: 110005\n")
            zf.writestr("repo-root/.pkgmeta", f"ignore:\n  - {sentinel}\n")
        workdir = self.tmp / "work"
        workdir.mkdir()
        spec = {"strategy": "github-branch", "repo": "a/b",
                "branch": "main", "package_as": "Addon"}

        def copy_archive(url, destination):
            destination.write_bytes(archive.read_bytes())

        with mock.patch.object(u, "resolve_github_branch", return_value=("v1", "url")), \
                mock.patch.object(u, "http_download", side_effect=copy_archive):
            with self.assertRaisesRegex(ValueError, "unsafe path"):
                u.fetch_addon("Addon", spec, workdir)
        self.assertTrue(sentinel.is_dir())

    def test_toc_folders_filters_extras(self):
        source = make_package(self.tmp, folders=("Addon", "Addon_Config"))
        (source / "docs").mkdir()
        root, folders = u.toc_folders(source, allow_wrapper=True)
        self.assertEqual(root, source)
        self.assertEqual(folders, ["Addon", "Addon_Config"])

    def test_toc_folders_accepts_single_wrapper(self):
        source = make_package(self.tmp, folders=("Addon",), wrapper="release-v1")
        root, folders = u.toc_folders(source, allow_wrapper=True)
        self.assertEqual(root, source / "release-v1")
        self.assertEqual(folders, ["Addon"])

    def test_toc_folders_rejects_non_addons(self):
        source = self.tmp / "package"
        (source / "docs").mkdir(parents=True)
        with self.assertRaisesRegex(ValueError, "no top-level addon folder"):
            u.toc_folders(source, allow_wrapper=True)


class InstallTransactionTest(TempDirTestCase):
    def test_install_removes_obsolete_tracked_folders(self):
        source = make_package(self.tmp, folders=("Addon",))
        flavor = make_flavor(self.tmp / "wow", installed=["Addon", "OldModule"])
        (flavor.addons_dir / "Addon" / "old.txt").write_text("old")
        folders = u.install_addon(
            {"strategy": "github-release"}, source, flavor, "v2",
            previous_folders=["Addon", "OldModule"])
        self.assertEqual(folders, ["Addon"])
        self.assertFalse((flavor.addons_dir / "OldModule").exists())
        self.assertFalse((flavor.addons_dir / "Addon" / "old.txt").exists())

    def test_install_rolls_back_new_and_obsolete_folders_on_copy_failure(self):
        source = self.tmp / "source"
        (source / "Addon").mkdir(parents=True)
        (source / "Addon" / "new.txt").write_text("new")
        addons = self.tmp / "AddOns"
        (addons / "Addon").mkdir(parents=True)
        (addons / "Addon" / "old.txt").write_text("old")
        (addons / "OldModule").mkdir()
        with self.assertRaises(OSError):
            u.install(source, addons, ["Addon", "Missing"], ["OldModule"])
        self.assertTrue((addons / "Addon" / "old.txt").is_file())
        self.assertTrue((addons / "OldModule").is_dir())
        self.assertFalse((addons / "Addon" / "new.txt").exists())


class VersionResolutionTest(TempDirTestCase):
    def test_dotted_versions_are_sorted_numerically(self):
        tags = [
            {"name": "v1.9.0", "zipball_url": "old"},
            {"name": "v1.10.0", "zipball_url": "new"},
        ]
        with mock.patch.object(u, "http_json", return_value=tags):
            self.assertEqual(
                u.resolve_github_source(
                    {"repo": "a/b", "tag_pattern": r"v([\d.]+)"}),
                ("1.10.0", "new"))

    def test_nonnumeric_capture_is_rejected(self):
        tags = [{"name": "release-beta", "zipball_url": "x"}]
        with mock.patch.object(u, "http_json", return_value=tags):
            with self.assertRaisesRegex(ValueError, "numeric dotted version"):
                u.resolve_github_source(
                    {"repo": "a/b", "tag_pattern": r"release-(.+)"})


class FailureReportingTest(TempDirTestCase):
    def setUp(self):
        super().setUp()
        self.registry = {"Addon": {"strategy": "github-release", "repo": "a/b",
                                   "asset": "^x$"}}
        self.flavor = make_flavor(self.tmp, installed=["Addon"],
                                  state={"Addon": {"version": "v1",
                                                   "folders": ["Addon"]}})

    def test_update_flavor_returns_failure_count(self):
        with mock.patch.object(u, "fetch_addon",
                               side_effect=urllib.error.URLError("offline")):
            with redirect_stdout(io.StringIO()):
                counts = u.update_flavor(
                    self.flavor, self.registry, {}, self.tmp, dry_run=False)
        self.assertEqual(counts["failed"], 1)

    def test_main_returns_nonzero_when_update_fails(self):
        registry_path = self.tmp / "addons.json"
        u.save_registry(self.registry, registry_path)
        argv = ["update_addons.py", "--registry", str(registry_path),
                "--wow-dir", str(self.flavor.wow_root)]
        with mock.patch.object(u.sys, "argv", argv), \
                mock.patch.object(u, "fetch_addon",
                                  side_effect=urllib.error.URLError("offline")), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(u.main(), 1)

    def test_list_check_shows_failure_and_returns_nonzero(self):
        output = io.StringIO()
        with mock.patch.object(u, "resolve_latest_version",
                               side_effect=urllib.error.URLError("offline")), \
                redirect_stdout(output):
            result = u.cmd_list([self.flavor.wow_root], self.registry, check=True)
        self.assertEqual(result, 1)
        self.assertIn("check failed", output.getvalue())


class MultiRootStatusTest(TempDirTestCase):
    def test_same_named_flavors_retain_distinct_identity(self):
        registry = {"Addon": {"strategy": "github-release", "repo": "a/b",
                              "asset": "^x$"}}
        first = make_flavor(self.tmp / "first", installed=["Addon"],
                            state={"Addon": {"version": "v1", "folders": ["Addon"]}})
        second = make_flavor(self.tmp / "second", installed=["Addon"],
                             state={"Addon": {"version": "v1", "folders": ["Addon"]}})
        rows = u.gather_status([first, second], registry)
        self.assertEqual(len({row["flavor_key"] for row in rows}), 2)
        self.assertIs(rows[0]["flavor_obj"], first)
        self.assertIs(rows[1]["flavor_obj"], second)
        self.assertNotEqual(rows[0]["flavor_display"], rows[1]["flavor_display"])


class AtomicPersistenceTest(TempDirTestCase):
    def test_atomic_write_preserves_existing_mode(self):
        path = self.tmp / "state.json"
        path.write_text("{}\n")
        os.chmod(path, 0o640)
        u.atomic_write_json(path, {"ok": True})
        self.assertEqual(path.stat().st_mode & 0o777, 0o640)
        self.assertEqual(json.loads(path.read_text()), {"ok": True})

    def test_replace_failure_leaves_original_and_cleans_temp(self):
        path = self.tmp / "state.json"
        path.write_text('{"old": true}\n')
        with mock.patch.object(u.os, "replace", side_effect=OSError("disk error")):
            with self.assertRaises(OSError):
                u.atomic_write_json(path, {"new": True})
        self.assertEqual(json.loads(path.read_text()), {"old": True})
        self.assertEqual(list(self.tmp.glob(".state.json.*")), [])


class InstallRegistryTest(TempDirTestCase):
    def test_new_registry_entry_is_not_written_when_every_install_fails(self):
        registry_path = self.tmp / "addons.json"
        u.save_registry({}, registry_path)
        flavor = make_flavor(self.tmp / "wow")
        spec = {"strategy": "github-release", "repo": "a/b", "asset": "^x$"}
        with mock.patch.object(u, "resolve_install_target",
                               return_value=("Addon", spec, True)), \
                mock.patch.object(u, "fetch_addon", return_value=("v1", self.tmp)), \
                mock.patch.object(u, "install_addon", side_effect=OSError("full")), \
                redirect_stdout(io.StringIO()):
            result = u.cmd_install(
                "a/b", {}, registry_path, [flavor.wow_root], None, False)
        self.assertEqual(result, 1)
        self.assertEqual(json.loads(registry_path.read_text()), {})


if __name__ == "__main__":
    unittest.main()
