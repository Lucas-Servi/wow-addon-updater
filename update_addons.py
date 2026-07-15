#!/usr/bin/env python3
"""wow-addon-updater — update World of Warcraft addons in Steam Proton installs.

Auto-detects every WoW installation living inside a Steam Proton prefix
(compatdata), finds each installed flavor (_retail_, _classic_, _classic_era_,
_anniversary_, ...), and updates the addons listed in addons.json.

No accounts, no API keys: addons are fetched from GitHub, either as published
release assets or built from tagged source (with the .toc lines that CI would
normally generate patched in from your local client version).

Python 3.8+, standard library only.
"""

import argparse
import json
import re
import shutil
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

USER_AGENT = "wow-addon-updater (https://github.com/Lucas-Servi/wow-addon-updater)"
STATE_FILENAME = ".addon-updater.json"

STEAM_ROOTS = [
    "~/.local/share/Steam",
    "~/.steam/steam",
    "~/.var/app/com.valvesoftware.Steam/.local/share/Steam",
]


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def steam_libraries():
    """Yield every Steam library path, following libraryfolders.vdf."""
    seen = set()
    for root in STEAM_ROOTS:
        root = Path(root).expanduser()
        vdf = root / "steamapps" / "libraryfolders.vdf"
        if not vdf.is_file():
            continue
        paths = [root] + [
            Path(m) for m in re.findall(r'"path"\s+"([^"]+)"', vdf.read_text(errors="replace"))
        ]
        for lib in paths:
            try:
                key = lib.resolve()
            except OSError:
                continue
            if key not in seen and lib.is_dir():
                seen.add(key)
                yield lib


def find_wow_roots():
    """Find 'World of Warcraft' folders inside Proton prefixes across all libraries."""
    roots = []
    seen = set()
    for lib in steam_libraries():
        compatdata = lib / "steamapps" / "compatdata"
        if not compatdata.is_dir():
            continue
        for pattern in ("*/pfx/drive_c/*/World of Warcraft",
                        "*/pfx/drive_c/*/*/World of Warcraft"):
            for wow in compatdata.glob(pattern):
                key = wow.resolve()
                if key not in seen and wow.is_dir():
                    seen.add(key)
                    roots.append(wow)
    return roots


def read_build_versions(wow_root):
    """Map product name -> client version (e.g. 'wow_anniversary' -> '2.5.6')
    from the pipe-delimited .build.info at the WoW root."""
    build_info = wow_root / ".build.info"
    versions = {}
    if not build_info.is_file():
        return versions
    lines = build_info.read_text(errors="replace").splitlines()
    if not lines:
        return versions
    header = [col.split("!")[0] for col in lines[0].split("|")]
    try:
        v_idx, p_idx = header.index("Version"), header.index("Product")
    except ValueError:
        return versions
    for line in lines[1:]:
        cols = line.split("|")
        if len(cols) <= max(v_idx, p_idx):
            continue
        m = re.match(r"(\d+)\.(\d+)\.(\d+)", cols[v_idx])
        if m:
            versions[cols[p_idx]] = m.group(0)
    return versions


class Flavor:
    def __init__(self, wow_root, flavor_dir):
        self.wow_root = wow_root
        self.name = flavor_dir.name
        self.addons_dir = flavor_dir / "Interface" / "AddOns"
        self.version = self._client_version(flavor_dir)

    def _client_version(self, flavor_dir):
        versions = read_build_versions(self.wow_root)
        flavor_info = flavor_dir / ".flavor.info"
        if flavor_info.is_file():
            lines = flavor_info.read_text(errors="replace").splitlines()
            if len(lines) >= 2 and lines[1].strip() in versions:
                return versions[lines[1].strip()]
        if len(versions) == 1:
            return next(iter(versions.values()))
        return None

    @property
    def interface(self):
        """WoW interface number, e.g. 2.5.6 -> 20506."""
        if not self.version:
            return None
        major, minor, patch = (int(n) for n in self.version.split("."))
        return major * 10000 + minor * 100 + patch


def find_flavors(wow_root):
    flavors = []
    for entry in sorted(wow_root.iterdir()):
        if entry.is_dir() and re.fullmatch(r"_\w+_", entry.name):
            flavor = Flavor(wow_root, entry)
            if flavor.addons_dir.is_dir():
                flavors.append(flavor)
    return flavors


# ---------------------------------------------------------------------------
# GitHub fetching
# ---------------------------------------------------------------------------

def http_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                               "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def http_download(url, dest):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=120) as resp, open(dest, "wb") as f:
        shutil.copyfileobj(resp, f)


def safe_extract(zip_path, dest):
    with zipfile.ZipFile(zip_path) as zf:
        for member in zf.namelist():
            p = Path(member)
            if p.is_absolute() or ".." in p.parts:
                raise ValueError(f"unsafe path in zip: {member}")
        zf.extractall(dest)


def resolve_github_release(spec):
    """Return (version, download_url) for the latest release asset matching spec['asset']."""
    release = http_json(f"https://api.github.com/repos/{spec['repo']}/releases/latest")
    pattern = re.compile(spec["asset"])
    for asset in release.get("assets", []):
        if pattern.match(asset["name"]):
            return release["tag_name"], asset["browser_download_url"]
    raise LookupError(f"no release asset matching {spec['asset']!r} in {spec['repo']}")


def resolve_github_source(spec):
    """Return (version, zipball_url) for the newest tag matching spec['tag_pattern']."""
    tags = http_json(f"https://api.github.com/repos/{spec['repo']}/tags?per_page=100")
    pattern = re.compile(spec["tag_pattern"])
    best = None
    for tag in tags:
        m = pattern.fullmatch(tag["name"])
        if m and (best is None or int(m.group(1)) > int(best[0])):
            best = (m.group(1), tag["zipball_url"])
    if best is None:
        raise LookupError(f"no tag matching {spec['tag_pattern']!r} in {spec['repo']}")
    return best


def fetch_addon(name, spec, workdir):
    """Download and extract an addon once per run.

    Returns (version, source_dir) where source_dir contains the addon's
    top-level folder(s) ready to copy into an AddOns directory.
    """
    strategy = spec["strategy"]
    if strategy == "github-release":
        version, url = resolve_github_release(spec)
    elif strategy == "github-source":
        version, url = resolve_github_source(spec)
    else:
        raise ValueError(f"{name}: unknown strategy {strategy!r}")

    zip_path = workdir / f"{name}.zip"
    extract_dir = workdir / name
    http_download(url, zip_path)
    extract_dir.mkdir()
    safe_extract(zip_path, extract_dir)

    if strategy == "github-source":
        # The zipball has a single "<owner>-<repo>-<sha>" root; rename it to
        # the addon folder name and strip files that only exist for development.
        root = next(extract_dir.iterdir())
        addon_dir = extract_dir / spec["package_as"]
        root.rename(addon_dir)
        for ignored in spec.get("ignore", []):
            target = addon_dir / ignored
            if target.is_dir():
                shutil.rmtree(target)
            elif target.exists():
                target.unlink()

    return version, extract_dir


def patch_toc(addon_dir, interface, version):
    """Fill in the .toc fields that the addon's CI would normally generate."""
    for toc in addon_dir.glob("*.toc"):
        text = toc.read_text(errors="replace")
        if interface:
            text = re.sub(r"^## Interface:.*$", f"## Interface: {interface}",
                          text, flags=re.MULTILINE)
        text = re.sub(r"^## Version:.*$", f"## Version: {version}",
                      text, flags=re.MULTILINE)
        toc.write_text(text)


# ---------------------------------------------------------------------------
# Installing
# ---------------------------------------------------------------------------

def load_state(addons_dir):
    state_file = addons_dir / STATE_FILENAME
    if state_file.is_file():
        try:
            return json.loads(state_file.read_text())
        except json.JSONDecodeError:
            pass
    return {}


def save_state(addons_dir, state):
    (addons_dir / STATE_FILENAME).write_text(json.dumps(state, indent=2) + "\n")


def install(source_dir, addons_dir, folders):
    """Atomically replace `folders` in addons_dir with the copies in source_dir."""
    with tempfile.TemporaryDirectory(dir=addons_dir) as backup:
        moved = []
        try:
            for folder in folders:
                old = addons_dir / folder
                if old.exists():
                    shutil.move(str(old), str(Path(backup) / folder))
                    moved.append(folder)
            for folder in folders:
                shutil.copytree(source_dir / folder, addons_dir / folder)
        except BaseException:
            for folder in folders:
                new = addons_dir / folder
                if new.exists():
                    shutil.rmtree(new)
            for folder in moved:
                shutil.move(str(Path(backup) / folder), str(addons_dir / folder))
            raise


def update_flavor(flavor, registry, cache, workdir, dry_run):
    version_note = f"{flavor.version}, interface {flavor.interface}" if flavor.version else "unknown client version"
    print(f"  {flavor.name} ({version_note})")

    state = load_state(flavor.addons_dir)
    installed = {p.name for p in flavor.addons_dir.iterdir() if p.is_dir()}
    covered = set()

    for name, spec in registry.items():
        if name not in installed:
            continue
        covered.add(name)
        covered.update(state.get(name, {}).get("folders", []))
        current = state.get(name, {}).get("version", "unknown")
        try:
            if name not in cache:
                cache[name] = fetch_addon(name, spec, workdir) if not dry_run else (
                    resolve_latest_version(spec), None)
            latest, source_dir = cache[name]
        except (urllib.error.URLError, LookupError, ValueError, OSError) as e:
            print(f"    {name}: FAILED to fetch ({e})")
            continue

        if current == latest:
            print(f"    {name}: up to date ({current})")
            continue
        if dry_run:
            print(f"    {name}: would update ({current} -> {latest})")
            continue

        folders = [p.name for p in source_dir.iterdir() if p.is_dir()]
        try:
            install(source_dir, flavor.addons_dir, folders)
        except OSError as e:
            print(f"    {name}: FAILED to install ({e})")
            continue
        if spec["strategy"] == "github-source":
            patch_toc(flavor.addons_dir / spec["package_as"], flavor.interface, latest)
        state[name] = {"version": latest, "folders": folders}
        save_state(flavor.addons_dir, state)
        covered.update(folders)
        print(f"    {name}: updated ({current} -> {latest})")

    for name in sorted(installed - covered):
        print(f"    {name}: skipped (no entry in addons.json)")


def resolve_latest_version(spec):
    if spec["strategy"] == "github-release":
        return resolve_github_release(spec)[0]
    return resolve_github_source(spec)[0]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would be updated without changing anything")
    parser.add_argument("--wow-dir", action="append", type=Path, default=[],
                        help="path to a 'World of Warcraft' folder (skips auto-detection; repeatable)")
    parser.add_argument("--registry", type=Path,
                        default=Path(__file__).resolve().parent / "addons.json",
                        help="path to the addon registry (default: addons.json next to this script)")
    args = parser.parse_args()

    try:
        registry = json.loads(args.registry.read_text())
    except (OSError, json.JSONDecodeError) as e:
        sys.exit(f"error: cannot read registry {args.registry}: {e}")

    wow_roots = [d.expanduser() for d in args.wow_dir] or find_wow_roots()
    if not wow_roots:
        sys.exit("error: no World of Warcraft install found in any Steam Proton prefix.\n"
                 "Pass --wow-dir /path/to/World of Warcraft to point at it directly.")

    cache = {}
    exit_code = 0
    with tempfile.TemporaryDirectory(prefix="wow-addon-updater-") as workdir:
        for wow_root in wow_roots:
            print(f"World of Warcraft @ {wow_root}")
            flavors = find_flavors(wow_root)
            if not flavors:
                print("  no flavor with an Interface/AddOns folder found")
                continue
            for flavor in flavors:
                try:
                    update_flavor(flavor, registry, cache, Path(workdir), args.dry_run)
                except OSError as e:
                    print(f"  {flavor.name}: FAILED ({e})")
                    exit_code = 1
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
