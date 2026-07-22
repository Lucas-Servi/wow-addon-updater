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
import os
import queue
import re
import shutil
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
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
# Terminal styling (stdlib-only ANSI; no third-party deps)
# ---------------------------------------------------------------------------

COLOR = False  # flipped once in main() based on tty / env / --no-color

_ANSI = {
    "bold": "1", "dim": "2",
    "red": "31", "green": "32", "yellow": "33", "cyan": "36",
    "gray": "90",
}

# Status glyphs, one per possible outcome.
GLYPH_OK = "✓"       # ✓ updated now
GLYPH_UP = "↑"       # ↑ update available (dry-run)
GLYPH_FAIL = "✗"     # ✗ fetch/install failed
GLYPH_SKIP = "–"     # – installed but not in registry
GLYPH_CURRENT = "·"  # · already up to date
GLYPH_PIN = "⇧"      # ⇧ pinned (held at its tracked version)


def c(text, *styles):
    """Wrap text in ANSI styles, or return it unchanged when color is off."""
    if not COLOR or not styles:
        return text
    codes = ";".join(_ANSI[s] for s in styles)
    return f"\033[{codes}m{text}\033[0m"


def format_size(n):
    """Render a byte count as e.g. '1.4 MB'; some addon zips run 50+ MB."""
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024


def highlight_json(text):
    """Syntax-highlight a JSON snippet: keys cyan, string values green, braces dim.

    Runs a single regex pass over the original text so the ANSI codes it inserts
    are never re-scanned (JSON string values here contain regex backslashes).
    """
    if not COLOR:
        return text
    token = re.compile(r'"(?:\\.|[^"\\])*"|[{}\[\],]')

    def repl(m):
        tok = m.group(0)
        if tok[0] != '"':
            return c(tok, "dim")
        following = text[m.end():].lstrip(" \t")
        return c(tok, "cyan" if following.startswith(":") else "green")

    return token.sub(repl, text)


def color_enabled(no_color_flag):
    """Decide whether to emit ANSI codes (honors NO_COLOR / FORCE_COLOR / tty)."""
    if no_color_flag or os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    return sys.stdout.isatty() and os.environ.get("TERM") != "dumb"


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


def http_download(url, dest, on_progress=None):
    """Stream url to dest. If given, on_progress(downloaded_bytes, total_bytes_or_None)
    is called after every chunk so slow, large downloads (some addon zips run
    50+ MB) can show they're still moving instead of sitting silent."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=120) as resp, open(dest, "wb") as f:
        total = resp.getheader("Content-Length")
        total = int(total) if total is not None else None
        downloaded = 0
        while chunk := resp.read(65536):
            f.write(chunk)
            downloaded += len(chunk)
            if on_progress:
                on_progress(downloaded, total)


def safe_extract(zip_path, dest):
    try:
        with zipfile.ZipFile(zip_path) as zf:
            for member in zf.namelist():
                p = Path(member)
                if p.is_absolute() or ".." in p.parts:
                    raise ValueError(f"unsafe path in zip: {member}")
            zf.extractall(dest)
    except zipfile.BadZipFile as e:
        raise ValueError(f"invalid zip archive: {e}") from e


def safe_child(root, value, *, top_level=False):
    """Return a path contained by ``root`` or reject an unsafe relative path."""
    root = root.resolve()
    relative = Path(value)
    if (not relative.parts or relative.is_absolute() or ".." in relative.parts
            or (top_level and len(relative.parts) != 1)):
        raise ValueError(f"unsafe path outside addon root: {value!r}")
    target = (root / relative).resolve(strict=False)
    try:
        target.relative_to(root)
    except ValueError:
        raise ValueError(f"unsafe path outside addon root: {value!r}")
    if target == root:
        raise ValueError(f"unsafe path refers to addon root: {value!r}")
    return target


def remove_path(path):
    """Remove a file, directory, or symlink without following symlinks."""
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def toc_folders(source_dir, allow_wrapper=False):
    """Return (source root, addon folders) for a validated extracted package.

    A folder is installable only when it contains a direct ``.toc`` file. GitHub
    release assets may put all addon folders beneath one wrapper directory.
    """
    top_dirs = sorted((p for p in source_dir.iterdir() if p.is_dir()),
                      key=lambda p: p.name)

    def candidates(root):
        return [p.name for p in sorted(root.iterdir(), key=lambda p: p.name)
                if p.is_dir() and any(
                    child.is_file() and child.suffix.lower() == ".toc"
                    for child in p.iterdir())]

    folders = candidates(source_dir)
    if folders:
        return source_dir, folders
    if allow_wrapper and len(top_dirs) == 1:
        folders = candidates(top_dirs[0])
        if folders:
            return top_dirs[0], folders
    raise ValueError("download contains no top-level addon folder with a .toc file")


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
    if pattern.groups < 1:
        raise ValueError(
            f"tag_pattern must contain a capture group for the version, "
            f"got {spec['tag_pattern']!r}")
    best = None
    for tag in tags:
        m = pattern.fullmatch(tag["name"])
        if not m:
            continue
        version = m.group(1)
        if not re.fullmatch(r"\d+(?:\.\d+)*", version):
            raise ValueError(
                f"tag_pattern group 1 must be a numeric dotted version, got {version!r}")
        sort_key = tuple(int(part) for part in version.split("."))
        if best is None or sort_key > best[0]:
            best = (sort_key, version, tag["zipball_url"])
    if best is None:
        raise LookupError(f"no tag matching {spec['tag_pattern']!r} in {spec['repo']}")
    return best[1], best[2]


def resolve_github_branch(spec):
    """Return (version, zipball_url) for the tip of a branch (default branch if unset).

    For projects that stopped tagging and release straight from their main
    branch. The version is 'YYYY-MM-DD.shortsha' of the tip commit.
    """
    branch = spec.get("branch", "HEAD")
    commit = http_json(f"https://api.github.com/repos/{spec['repo']}/commits/{branch}")
    date = commit["commit"]["committer"]["date"][:10]
    sha = commit["sha"]
    return f"{date}.{sha[:7]}", f"https://api.github.com/repos/{spec['repo']}/zipball/{sha}"


def pkgmeta_ignores(addon_dir):
    """Read the 'ignore:' list from a repo's .pkgmeta, if present."""
    pkgmeta = addon_dir / ".pkgmeta"
    if not pkgmeta.is_file():
        return []
    ignores, in_ignore = [], False
    for line in pkgmeta.read_text(errors="replace").splitlines():
        if re.match(r"^\S", line):
            in_ignore = line.startswith("ignore:")
            continue
        m = re.match(r"^\s+-\s+(\S+)", line)
        if in_ignore and m:
            ignores.append(m.group(1))
    return ignores


def fetch_addon(name, spec, workdir, on_progress=None):
    """Download and extract an addon once per run.

    Returns (version, source_dir) where source_dir contains the addon's
    top-level folder(s) ready to copy into an AddOns directory.
    """
    strategy = spec["strategy"]
    if strategy == "github-release":
        version, url = resolve_github_release(spec)
    elif strategy == "github-source":
        version, url = resolve_github_source(spec)
    elif strategy == "github-branch":
        version, url = resolve_github_branch(spec)
    else:
        raise ValueError(f"{name}: unknown strategy {strategy!r}")

    zip_path = workdir / f"{name}.zip"
    extract_dir = workdir / name
    http_download(url, zip_path, on_progress=on_progress)
    extract_dir.mkdir()
    safe_extract(zip_path, extract_dir)

    if strategy in ("github-source", "github-branch"):
        # The zipball has a single "<owner>-<repo>-<sha>" root; rename it to
        # the addon folder name and strip files that only exist for development:
        # the repo's own .pkgmeta ignore list, any extra spec ignores, and
        # top-level dotfiles.
        roots = list(extract_dir.iterdir())
        if len(roots) != 1 or not roots[0].is_dir():
            raise ValueError("GitHub source archive did not contain one root directory")
        root = roots[0]
        addon_dir = safe_child(extract_dir, spec["package_as"], top_level=True)
        root.rename(addon_dir)
        dotfiles = [p.name for p in addon_dir.iterdir() if p.name.startswith(".")]
        for ignored in pkgmeta_ignores(addon_dir) + spec.get("ignore", []) + dotfiles:
            target = safe_child(addon_dir, ignored)
            remove_path(target)

        source_dir, _ = toc_folders(extract_dir)
    else:
        source_dir, _ = toc_folders(extract_dir, allow_wrapper=True)

    return version, source_dir


def patch_toc(addon_dir, interface, version):
    """Fill in the .toc fields that the addon's CI would normally generate.

    The Interface line is only replaced when it doesn't already list this
    client's interface number (repos increasingly keep a correct
    multi-flavor list checked in). The Version line is only replaced when
    it holds a build-time placeholder like @project-version@.
    """
    for toc in addon_dir.glob("*.toc"):
        text = toc.read_text(errors="replace")
        m = re.search(r"^## Interface:(.*)$", text, flags=re.MULTILINE)
        if interface and m and str(interface) not in m.group(1):
            text = text.replace(m.group(0), f"## Interface: {interface}")
        # A function replacement avoids re.sub interpreting backslashes/group
        # references that could appear in a version string.
        text = re.sub(r"^## Version:.*(@[\w-]+@|AUTO_GENERATED_VERSION).*$",
                      lambda _m: f"## Version: {version}", text, flags=re.MULTILINE)
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
    atomic_write_json(addons_dir / STATE_FILENAME, state)


def atomic_write_json(path, value):
    """Durably replace a JSON file without exposing a partially written file."""
    path = Path(path)
    mode = (path.stat().st_mode & 0o7777) if path.exists() else 0o644
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=path.parent,
                prefix=f".{path.name}.", delete=False) as temp:
            temp_path = Path(temp.name)
            json.dump(value, temp, indent=2)
            temp.write("\n")
            temp.flush()
            os.fsync(temp.fileno())
        os.chmod(temp_path, mode)
        os.replace(temp_path, path)
    except BaseException:
        if temp_path is not None:
            try:
                temp_path.unlink()
            except OSError:
                pass
        raise


def install(source_dir, addons_dir, folders, remove_folders=()):
    """Atomically replace new folders and remove obsolete tracked folders."""
    folders = list(dict.fromkeys(folders))
    targets = list(dict.fromkeys(folders + list(remove_folders)))
    for folder in targets:
        safe_child(addons_dir, folder, top_level=True)
    with tempfile.TemporaryDirectory(dir=addons_dir) as backup:
        moved = []
        try:
            for folder in targets:
                old = addons_dir / folder
                if old.exists() or old.is_symlink():
                    shutil.move(str(old), str(Path(backup) / folder))
                    moved.append(folder)
            for folder in folders:
                shutil.copytree(source_dir / folder, addons_dir / folder)
        except BaseException:
            for folder in folders:
                new = addons_dir / folder
                remove_path(new)
            for folder in moved:
                shutil.move(str(Path(backup) / folder), str(addons_dir / folder))
            raise


def install_addon(spec, source_dir, flavor, latest, previous_folders=()):
    """Copy a fetched addon's folders into one flavor and patch its .toc.

    Returns the installed top-level folder names. Shared by `update` and
    `install`; the caller records state. Raises OSError on copy failure.
    """
    source_dir, folders = toc_folders(source_dir)
    obsolete = [folder for folder in previous_folders if folder not in folders]
    if spec["strategy"] in ("github-source", "github-branch"):
        with tempfile.TemporaryDirectory(prefix="wow-addon-stage-") as staging:
            staging = Path(staging)
            for folder in folders:
                shutil.copytree(source_dir / folder, staging / folder)
            # Patch every folder the package ships, not just package_as — a
            # multi-folder addon (e.g. Foo + Foo_Config) has a .toc in each.
            for folder in folders:
                patch_toc(staging / folder, flavor.interface, latest)
            install(staging, flavor.addons_dir, folders, obsolete)
    else:
        install(source_dir, flavor.addons_dir, folders, obsolete)
    return folders


def classify_installed(flavor, registry, state):
    """Split a flavor's installed folders into managed vs. unmanaged.

    Single source of truth for "which installed folder belongs to which addon",
    shared by `update_flavor` and `gather_status` so the two can never drift.

    Returns (managed, unmanaged, covered):
      managed   - registry names whose own or state-recorded folder is installed
      covered   - installed primary and state-recorded folders for those names
                  (e.g. an addon that ships AdiBags + AdiBags_Config)
      unmanaged - sorted installed dirs not covered by any registry addon
    """
    installed = {p.name for p in flavor.addons_dir.iterdir() if p.is_dir()}
    managed = []
    covered = set()
    for name in registry:
        tracked = set(state.get(name, {}).get("folders", [])) & installed
        if name in installed or tracked:
            managed.append(name)
            if name in installed:
                covered.add(name)
            covered.update(tracked)
    unmanaged = sorted(installed - covered)
    return managed, unmanaged, covered


# ---------------------------------------------------------------------------
# Status / removal / pinning (backend for `list`, `remove`, `pin`, and the TUI)
# ---------------------------------------------------------------------------

def all_flavors(wow_roots):
    """Every flavor across every detected WoW install, in a stable order."""
    flavors = []
    for wow_root in wow_roots:
        flavors.extend(find_flavors(wow_root))
    return flavors


def flavor_display_names(flavors):
    """Build unambiguous flavor labels, adding a shortest root suffix as needed."""
    name_counts = {}
    roots = []
    for flavor in flavors:
        name_counts[flavor.name] = name_counts.get(flavor.name, 0) + 1
        root = str(flavor.wow_root.resolve())
        if root not in roots:
            roots.append(root)

    root_labels = {}
    for root in roots:
        parts = Path(root).parts
        label = root
        for depth in range(1, len(parts) + 1):
            suffix = parts[-depth:]
            if all(suffix != Path(other).parts[-depth:]
                   for other in roots if other != root):
                label = "/".join(suffix)
                break
        root_labels[root] = label

    labels = {}
    for flavor in flavors:
        key = (str(flavor.wow_root.resolve()), flavor.name)
        labels[key] = flavor.name
        if name_counts[flavor.name] > 1:
            labels[key] += f" · …/{root_labels[key[0]]}"
    return labels


def gather_status(flavors, registry, check=False, cache=None):
    """Return one status row per addon across all flavors.

    Each row includes the exact Flavor object, its stable root/name key, addon
    metadata, and any update-check error.
    Uses `classify_installed` for coverage, so a registry addon's extra folders
    are attributed to it rather than reported as separate "not in registry"
    entries. Network-free unless `check` is set, in which case each registry
    addon's latest version is resolved once (deduped through `cache`) and
    compared.
    """
    if cache is None:
        cache = {}
    display_names = flavor_display_names(flavors)
    rows = []
    for flavor in flavors:
        state = load_state(flavor.addons_dir)
        managed, unmanaged, _ = classify_installed(flavor, registry, state)
        for name in managed:
            spec = registry[name]
            version = state.get(name, {}).get("version", "unknown")
            latest, has_update, check_error = None, False, None
            if check and not spec.get("pin"):
                if name not in cache:
                    try:
                        cache[name] = (resolve_latest_version(spec), None)
                    except (urllib.error.URLError, LookupError, ValueError, OSError, re.error) as e:
                        cache[name] = (None, str(e))
                latest, check_error = cache[name]
                has_update = bool(latest) and latest != version
            flavor_key = (str(flavor.wow_root.resolve()), flavor.name)
            rows.append({
                "flavor": flavor.name, "flavor_obj": flavor,
                "flavor_key": flavor_key,
                "flavor_display": display_names[flavor_key],
                "wow_root": str(flavor.wow_root),
                "name": name, "version": version,
                "in_registry": True, "pinned": bool(spec.get("pin")),
                "latest": latest, "has_update": has_update,
                "check_error": check_error,
            })
        for name in unmanaged:
            flavor_key = (str(flavor.wow_root.resolve()), flavor.name)
            rows.append({
                "flavor": flavor.name, "flavor_obj": flavor,
                "flavor_key": flavor_key,
                "flavor_display": display_names[flavor_key],
                "wow_root": str(flavor.wow_root),
                "name": name, "version": "unknown",
                "in_registry": False, "pinned": False,
                "latest": None, "has_update": False, "check_error": None,
            })
    return rows


def remove_addon(name, flavors, registry, drop_registry=True, dry_run=False,
                 registry_path=None, all_known_flavors=None):
    """Uninstall `name` from the given flavors and (optionally) drop its entry.

    Only folders we recorded in state — or a single folder that exactly matches
    the addon name — are deleted; we never guess which folders belong to an
    addon. When `drop_registry` is set and a `registry_path` is given, the
    updated registry is persisted only when no copy remains in another known
    flavor. Returns (results, registry_dropped), where results is a list of
    (flavor_name, removed_folders).
    """
    def present_folders(flavor, state):
        folders = state.get(name, {}).get("folders")
        if not folders:
            folders = [name] if (flavor.addons_dir / name).is_dir() else []
        present = []
        for folder in folders:
            path = safe_child(flavor.addons_dir, folder, top_level=True)
            if path.exists() or path.is_symlink():
                present.append((folder, path))
        return present

    # Validate every recorded deletion path and determine whether another copy
    # will remain before making any change.
    operations = []
    for flavor in flavors:
        state = load_state(flavor.addons_dir)
        present = present_folders(flavor, state)
        if not present:
            continue
        operations.append((flavor, state, present))

    selected = {str(flavor.addons_dir.resolve()) for flavor in flavors}
    remaining = False
    for flavor in all_known_flavors or flavors:
        if str(flavor.addons_dir.resolve()) in selected:
            continue
        if present_folders(flavor, load_state(flavor.addons_dir)):
            remaining = True
            break

    results = []
    for flavor, state, present in operations:
        if not dry_run:
            for _, path in present:
                remove_path(path)
            state.pop(name, None)
            save_state(flavor.addons_dir, state)
        results.append((flavor.name, [folder for folder, _ in present]))

    should_drop = drop_registry and name in registry and not remaining
    if should_drop and not dry_run:
        updated = dict(registry)
        del updated[name]
        if registry_path is not None:
            save_registry(updated, registry_path)
        del registry[name]
    return results, should_drop


def save_registry(registry, registry_path):
    """Persist the registry using the same formatting as `install`."""
    atomic_write_json(registry_path, registry)


def set_pin(name, registry, pinned, registry_path):
    """Toggle a registry entry's pin flag and persist. Returns True on change."""
    if name not in registry:
        return False
    updated_registry = dict(registry)
    updated_spec = dict(registry[name])
    if pinned:
        updated_spec["pin"] = True
    else:
        updated_spec.pop("pin", None)
    updated_registry[name] = updated_spec
    save_registry(updated_registry, registry_path)
    registry[name] = updated_spec
    return True


def update_flavor(flavor, registry, cache, workdir, dry_run, is_last=True):
    branch = "└─" if is_last else "├─"
    if flavor.version:
        version_note = c(f"{flavor.version} (interface {flavor.interface})", "dim")
    else:
        version_note = c("unknown client version", "dim")
    print(f"{c(branch, 'dim')} {c(flavor.name, 'bold')}  {version_note}")

    state = load_state(flavor.addons_dir)
    managed, unmanaged, _ = classify_installed(flavor, registry, state)

    width = max((len(n) for n in managed + unmanaged), default=0)
    counts = {"updated": 0, "available": 0, "current": 0,
              "pinned": 0, "failed": 0, "skipped": 0}

    def row(glyph, style, name, detail):
        # \r + clear-line erases a "…  working…" progress line left by the
        # network call below, so the final status overwrites it in place
        # instead of leaving stale text before it (only meaningful on a tty).
        prefix = "\r\033[K" if COLOR else ""
        print(f"{prefix}   {c(glyph, style)} {c(name.ljust(width), 'bold')}  {detail}")

    def change(old, new, new_style):
        return f"{c(old, 'dim')} {c('→', 'dim')} {c(new, new_style)}"

    for name in managed:
        spec = registry[name]
        current = state.get(name, {}).get("version", "unknown")
        if spec.get("pin"):
            counts["pinned"] += 1
            row(GLYPH_PIN, "dim", name, c(f"pinned ({current})", "dim"))
            continue
        try:
            if name not in cache:
                # Cheap metadata-only check first: most addons are already
                # current most runs, and some zips run 50+ MB, so we don't
                # want to pay for the actual download until we know it's needed.
                cache[name] = (resolve_latest_version(spec), None)
            latest, source_dir = cache[name]
        except (urllib.error.URLError, LookupError, ValueError, OSError, re.error) as e:
            counts["failed"] += 1
            row(GLYPH_FAIL, "red", name, c(f"fetch failed: {e}", "red"))
            continue

        if current == latest:
            counts["current"] += 1
            row(GLYPH_CURRENT, "dim", name, c(current, "dim"))
            continue
        if dry_run:
            counts["available"] += 1
            row(GLYPH_UP, "cyan", name, change(current, latest, "cyan"))
            continue

        if source_dir is None:
            try:
                if COLOR:
                    print(f"   {c('…', 'dim')} {name.ljust(width)}  {c('working…', 'dim')}",
                          end="", flush=True)
                last_update = 0.0

                def on_progress(downloaded, total, _name=name):
                    # On a slow link a large zip can take minutes with nothing
                    # else printed, which reads as a hang. Throttled so a
                    # ~65 KB chunk callback doesn't flood stdout.
                    nonlocal last_update
                    if not COLOR:
                        return
                    now = time.monotonic()
                    if now - last_update < 0.15 and downloaded != total:
                        return
                    last_update = now
                    detail = (f"{format_size(downloaded)} / {format_size(total)}"
                              if total else format_size(downloaded))
                    print(f"\r   {c('…', 'dim')} {_name.ljust(width)}  {c(detail, 'dim')}\033[K",
                          end="", flush=True)

                latest, source_dir = fetch_addon(name, spec, workdir, on_progress=on_progress)
                cache[name] = (latest, source_dir)
            except (urllib.error.URLError, LookupError, ValueError, OSError, re.error) as e:
                counts["failed"] += 1
                row(GLYPH_FAIL, "red", name, c(f"fetch failed: {e}", "red"))
                continue

        try:
            previous = state.get(name, {}).get("folders", [])
            folders = install_addon(spec, source_dir, flavor, latest, previous)
            updated_state = dict(state)
            updated_state[name] = {"version": latest, "folders": folders}
            save_state(flavor.addons_dir, updated_state)
            state = updated_state
        except (OSError, ValueError) as e:
            counts["failed"] += 1
            row(GLYPH_FAIL, "red", name, c(f"install failed: {e}", "red"))
            continue
        counts["updated"] += 1
        row(GLYPH_OK, "green", name, change(current, latest, "green"))

    for name in unmanaged:
        counts["skipped"] += 1
        row(GLYPH_SKIP, "dim", name, c("not in registry", "dim"))

    labels = [
        (counts["updated"], "updated"),
        (counts["available"], "update available"),
        (counts["current"], "current"),
        (counts["pinned"], "pinned"),
        (counts["failed"], "failed"),
        (counts["skipped"], "skipped"),
    ]
    total = sum(counts.values())
    parts = [f"{total} addon{'s' if total != 1 else ''}"]
    parts += [f"{n} {label}" for n, label in labels if n]
    print(f"   {c('── ' + ' · '.join(parts) + ' ──', 'dim')}")
    return counts


def resolve_latest_version(spec):
    strategy = spec["strategy"]
    if strategy == "github-release":
        return resolve_github_release(spec)[0]
    if strategy == "github-branch":
        return resolve_github_branch(spec)[0]
    if strategy == "github-source":
        return resolve_github_source(spec)[0]
    raise ValueError(f"unknown strategy {strategy!r}")


# ---------------------------------------------------------------------------
# Search (discover addons on GitHub)
# ---------------------------------------------------------------------------

def search_cache_path():
    """Path to the file holding the last search's results, for `install <n>`."""
    base = os.environ.get("XDG_CACHE_HOME") or "~/.cache"
    return Path(base).expanduser() / "wow-addon-updater" / "last-search.json"


def save_search_results(items):
    """Persist the fields `install <n>` needs from a search's results."""
    path = search_cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        slim = [{"full_name": r["full_name"],
                 "default_branch": r.get("default_branch", "main")} for r in items]
        atomic_write_json(path, slim)
    except OSError:
        pass  # a non-writable cache dir just disables `install <n>`, not search


def load_search_results():
    """Return the last search's results, or [] if none/unreadable."""
    try:
        return json.loads(search_cache_path().read_text())
    except (OSError, json.JSONDecodeError):
        return []


# Non-WoW Lua ecosystems (FiveM/GTA-RP, GMod) that pollute a `language:Lua`
# search. Matched against a repo's full name + description to drop them.
_NON_WOW_RE = re.compile(
    r"\b(fivem|five-m|esx|qbcore|qb-core|qbox|ox_inventory|ox_lib|vrp|"
    r"gmod|garry'?s?mod|roblox|love2d|luau|nui|mta|redm)\b", re.IGNORECASE)


def search_github_addons(query, limit, wow_only=True):
    """Search GitHub for Lua repositories matching `query`, ranked by stars.

    There's no authoritative catalog of WoW addons to index, so this queries
    the live GitHub repository-search API each time. `language:Lua` is a strong
    signal, but it also matches the FiveM/GMod ecosystems — so by default we
    over-fetch and down-filter obvious non-WoW repos (preserving recall better
    than AND-ing extra keywords into the query would). Returns
    (items, hidden_count).
    """
    per_page = min(limit * 3, 100) if wow_only else limit
    q = urllib.parse.quote(f"{query} language:Lua")
    url = (f"https://api.github.com/search/repositories"
           f"?q={q}&sort=stars&order=desc&per_page={per_page}")
    items = http_json(url).get("items", [])
    if not wow_only:
        return items[:limit], 0
    kept = [r for r in items
            if not _NON_WOW_RE.search(f"{r['full_name']} {r.get('description') or ''}")]
    hidden = len(items) - len(kept)
    return kept[:limit], hidden


def fmt_stars(n):
    """1100 -> '1.1k', 340 -> '340'."""
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


def asset_to_pattern(name):
    """Turn a concrete asset filename into a version-agnostic anchored regex.

    'Questie-v11.32.1.zip' -> r'^Questie\\-v[\\d.]+\\.zip$'
    Version numbers (digits with internal dots) become [\\d.]+; everything else,
    including the extension separator, stays literal.
    """
    out = []
    for token in re.split(r"(\d+(?:\.\d+)*)", name):
        if not token:
            continue
        out.append(r"[\d.]+" if re.fullmatch(r"\d+(?:\.\d+)*", token) else re.escape(token))
    return "^" + "".join(out) + "$"


def suggest_registry_spec(repo):
    """Return an addons.json spec dict for `repo`, detecting release vs branch.

    Uses the repo's latest release if it ships a downloadable .zip asset,
    otherwise falls back to the branch-tip source strategy.
    """
    full_name = repo["full_name"]
    package = full_name.split("/")[-1]
    try:
        release = http_json(f"https://api.github.com/repos/{full_name}/releases/latest")
        zips = [a["name"] for a in release.get("assets", []) if a["name"].lower().endswith(".zip")]
        if zips:
            return {"strategy": "github-release", "repo": full_name,
                    "asset": asset_to_pattern(zips[0])}
    except (urllib.error.HTTPError, urllib.error.URLError, LookupError):
        pass  # no release / not reachable -> source from the branch tip
    return {"strategy": "github-branch", "repo": full_name,
            "branch": repo.get("default_branch", "main"), "package_as": package}


def cmd_search(query, limit, registry, wow_only=True):
    """List GitHub matches for `query` and print a paste-ready registry snippet."""
    print(f"{c('Searching GitHub for', 'bold')} {c(query, 'cyan')} {c('(language:Lua, by stars)', 'dim')}")
    try:
        items, hidden = search_github_addons(query, limit, wow_only)
    except urllib.error.HTTPError as e:
        if e.code == 403:
            sys.exit("error: GitHub rate limit reached (60 requests/hour unauthenticated). "
                     "Try again later.")
        sys.exit(f"error: GitHub search failed: {e}")
    except (urllib.error.URLError, ValueError) as e:
        sys.exit(f"error: GitHub search failed: {e}")

    if not items:
        print(f"{c('└─', 'dim')} {c('no matching repositories found', 'dim')}")
        return 0

    save_search_results(items)
    known_repos = {spec.get("repo") for spec in registry.values()}
    num_w = len(str(len(items)))
    star_w = max(len(fmt_stars(r["stargazers_count"])) for r in items)
    for i, repo in enumerate(items):
        num = str(i + 1).rjust(num_w)
        stars = fmt_stars(repo["stargazers_count"]).rjust(star_w)
        desc = (repo.get("description") or "").strip()
        if len(desc) > 60:
            desc = desc[:57] + "…"
        tag = c("  (in registry)", "green") if repo["full_name"] in known_repos else ""
        print(f"{c(num + '.', 'dim')} {c('★', 'yellow')} {c(stars, 'yellow')}  "
              f"{c(repo['full_name'], 'bold')}{tag}")
        if desc:
            print(f"{' ' * (num_w + 1)}   {c(desc, 'dim')}")

    if hidden:
        plural = "s" if hidden != 1 else ""
        note = f"  ({hidden} non-WoW result{plural} hidden; use --all to show them)"
        print(c(note, "dim"))

    print(f"\n{c('→ install with:', 'dim')} "
          f"{c('./update_addons.py install <number>', 'cyan')}")

    top = items[0]
    if top["full_name"] not in known_repos:
        print(f"\n{c('Or add ' + top['full_name'] + ' to addons.json manually:', 'dim')}")
        spec = suggest_registry_spec(top)
        snippet = f'"{top["full_name"].split("/")[-1]}": {json.dumps(spec, indent=2)}'
        print(highlight_json(snippet))
    return 0


# ---------------------------------------------------------------------------
# Install (add an addon to the registry and install it)
# ---------------------------------------------------------------------------

def resolve_install_target(target, registry):
    """Map an `install` argument to (name, spec, is_new).

    `target` may be a 1-based index into the last search, an 'owner/repo' slug,
    or the name of an addon already in the registry. This is pure: it never
    mutates `registry`. `is_new` is True when the spec was freshly derived (the
    addon is not yet in the registry), so the caller knows to add + persist it.
    """
    # 1) An existing registry entry by name.
    if target in registry:
        return target, registry[target], False

    # 2) A number referring to the last search's results.
    if target.isdigit():
        results = load_search_results()
        idx = int(target) - 1
        if not results:
            sys.exit("error: no cached search results. Run 'search <terms>' first, "
                     "or pass an owner/repo slug.")
        if not (0 <= idx < len(results)):
            sys.exit(f"error: {target} is out of range (last search had {len(results)} results).")
        repo = results[idx]
    # 3) An owner/repo slug fetched live from GitHub.
    elif re.fullmatch(r"[^/\s]+/[^/\s]+", target):
        try:
            repo = http_json(f"https://api.github.com/repos/{target}")
        except urllib.error.HTTPError as e:
            sys.exit(f"error: cannot find GitHub repo {target!r}: {e}")
        except (urllib.error.URLError, ValueError) as e:
            sys.exit(f"error: GitHub lookup failed: {e}")
    else:
        sys.exit(f"error: {target!r} is not a search number, an owner/repo slug, "
                 "or a known registry name.")

    name = repo["full_name"].split("/")[-1]
    if name in registry:
        return name, registry[name], False
    return name, suggest_registry_spec(repo), True


def choose_flavors(flavors):
    """Prompt the user to pick which flavor(s) to install into (or 'all')."""
    if len(flavors) == 1:
        return flavors
    print(f"{c('Install into which flavor?', 'bold')}")
    labels = flavor_display_names(flavors)
    for i, flavor in enumerate(flavors):
        ver = f" ({flavor.version})" if flavor.version else ""
        key = (str(flavor.wow_root.resolve()), flavor.name)
        print(f"  {c(str(i + 1) + ')', 'dim')} {labels[key]}{c(ver, 'dim')}")
    print(f"  {c('a)', 'dim')} all")
    try:
        choice = input(c("> ", "cyan")).strip().lower()
    except EOFError:
        choice = ""
    if choice in ("a", "all", ""):
        return flavors
    picked = []
    for part in re.split(r"[,\s]+", choice):
        if part.isdigit() and 1 <= int(part) <= len(flavors):
            picked.append(flavors[int(part) - 1])
    if not picked:
        sys.exit("error: no valid flavor selected.")
    return picked


def cmd_install(target, registry, registry_path, wow_roots, only_flavor, dry_run):
    """Add an addon to the registry (if new) and install it into chosen flavors."""
    name, spec, is_new = resolve_install_target(target, registry)

    print(f"{c('Installing', 'bold')} {c(name, 'cyan')} "
          f"{c('(' + spec['repo'] + ', ' + spec['strategy'] + ')', 'dim')}")

    flavors = all_flavors(wow_roots)
    if not flavors:
        sys.exit("error: no WoW flavor with an Interface/AddOns folder found.")

    if only_flavor:
        flavors = [f for f in flavors if f.name == only_flavor]
        if not flavors:
            sys.exit(f"error: no installed flavor named {only_flavor!r}.")
    else:
        flavors = choose_flavors(flavors)

    exit_code = 0
    with tempfile.TemporaryDirectory(prefix="wow-addon-updater-") as workdir:
        try:
            if dry_run:
                latest, source_dir = resolve_latest_version(spec), None
            else:
                latest, source_dir = fetch_addon(name, spec, Path(workdir))
        except (urllib.error.URLError, LookupError, ValueError, OSError, re.error) as e:
            sys.exit(f"error: cannot fetch {name}: {e}")

        width = max(len(f.name) for f in flavors)
        installed_count = 0
        for i, flavor in enumerate(flavors):
            branch = "└─" if i == len(flavors) - 1 else "├─"
            label = c(flavor.name.ljust(width), "bold")
            if dry_run:
                print(f"{c(branch, 'dim')} {label}  "
                      f"{c(GLYPH_UP, 'cyan')} {c('would install ' + latest, 'cyan')}")
                continue
            try:
                state = load_state(flavor.addons_dir)
                previous = state.get(name, {}).get("folders", [])
                folders = install_addon(spec, source_dir, flavor, latest, previous)
                updated_state = dict(state)
                updated_state[name] = {"version": latest, "folders": folders}
                save_state(flavor.addons_dir, updated_state)
            except (OSError, ValueError) as e:
                print(f"{c(branch, 'dim')} {label}  "
                      f"{c(GLYPH_FAIL, 'red')} {c('install failed: ' + str(e), 'red')}")
                exit_code = 1
                continue
            installed_count += 1
            print(f"{c(branch, 'dim')} {label}  "
                  f"{c(GLYPH_OK, 'green')} {c('installed ' + latest, 'green')}")
        if is_new and not dry_run and installed_count:
            updated_registry = dict(registry)
            updated_registry[name] = spec
            try:
                save_registry(updated_registry, registry_path)
            except OSError as e:
                print(f"   {c(GLYPH_FAIL, 'red')} "
                      f"{c('could not update ' + registry_path.name + ': ' + str(e), 'red')}")
                exit_code = 1
            else:
                registry[name] = spec
                print(f"   {c('+', 'green')} added to {registry_path.name}")
    return exit_code


# ---------------------------------------------------------------------------
# list / remove / pin (CLI twins of the backend, usable without the TUI)
# ---------------------------------------------------------------------------

def cmd_list(wow_roots, registry, check):
    """Show installed addons per flavor, with registry/pin/update status."""
    flavors = all_flavors(wow_roots)
    if not flavors:
        sys.exit("error: no WoW flavor with an Interface/AddOns folder found.")
    if check:
        print(c("Checking GitHub for updates…", "dim"))
    rows = gather_status(flavors, registry, check=check)

    by_flavor = {}
    for r in rows:
        by_flavor.setdefault(r["flavor_key"], []).append(r)

    roots = []
    for flavor in flavors:
        if flavor.wow_root not in roots:
            roots.append(flavor.wow_root)
    for wow_root in roots:
        print(f"{c('World of Warcraft', 'bold')}{c(' · ' + str(wow_root), 'dim')}")
        root_flavors = [f for f in flavors if f.wow_root == wow_root]
        for fi, flavor in enumerate(root_flavors):
            key = (str(flavor.wow_root.resolve()), flavor.name)
            frows = by_flavor.get(key, [])
            branch = "└─" if fi == len(root_flavors) - 1 else "├─"
            print(f"{c(branch, 'dim')} {c(flavor.name, 'bold')}  "
                  f"{c(f'{len(frows)} addons', 'dim')}")
            width = max((len(r["name"]) for r in frows), default=0)
            for r in frows:
                if not r["in_registry"]:
                    glyph, style, detail = GLYPH_SKIP, "dim", c("not in registry", "dim")
                elif r["check_error"]:
                    glyph, style = GLYPH_FAIL, "red"
                    detail = c(f"check failed: {r['check_error']}", "red")
                elif r["pinned"]:
                    glyph, style, detail = GLYPH_PIN, "dim", c(f"pinned ({r['version']})", "dim")
                elif r["has_update"]:
                    glyph, style = GLYPH_UP, "cyan"
                    detail = f"{c(r['version'], 'dim')} {c('→', 'dim')} {c(r['latest'], 'cyan')}"
                elif check:
                    glyph, style, detail = GLYPH_OK, "green", c(r["version"], "green")
                else:
                    glyph, style, detail = GLYPH_CURRENT, "dim", c(r["version"], "dim")
                print(f"   {c(glyph, style)} {c(r['name'].ljust(width), 'bold')}  {detail}")
    return 1 if any(r["check_error"] for r in rows) else 0


def cmd_remove(name, wow_roots, registry, registry_path, only_flavor,
               keep_registry, dry_run):
    """Uninstall an addon's folders and (unless kept) drop its registry entry."""
    known_flavors = all_flavors(wow_roots)
    flavors = known_flavors
    if only_flavor:
        flavors = [f for f in flavors if f.name == only_flavor]
        if not flavors:
            sys.exit(f"error: no installed flavor named {only_flavor!r}.")

    # Preview what would be removed (dry_run=True never touches disk).
    try:
        preview, _ = remove_addon(
            name, flavors, registry, drop_registry=False, dry_run=True,
            all_known_flavors=known_flavors)
    except (OSError, ValueError) as e:
        sys.exit(f"error: cannot inspect {name}: {e}")
    if not preview:
        print(f"{c(name, 'bold')}: {c('not installed in any selected flavor', 'dim')}")
        return 1

    if not dry_run and not only_flavor and sys.stdin.isatty():
        where = ", ".join(f"{fl} ({', '.join(fs)})" for fl, fs in preview)
        try:
            reply = input(c(f"Remove {name} from {where}? [y/N] ", "yellow")).strip().lower()
        except EOFError:
            reply = ""
        if reply not in ("y", "yes"):
            print(c("aborted", "dim"))
            return 0

    try:
        results, registry_dropped = remove_addon(
            name, flavors, registry, drop_registry=not keep_registry,
            dry_run=dry_run, registry_path=registry_path,
            all_known_flavors=known_flavors)
    except (OSError, ValueError) as e:
        sys.exit(f"error: cannot remove {name}: {e}")
    verb = "would remove" if dry_run else "removed"
    for i, (fl, folders) in enumerate(results):
        branch = "└─" if i == len(results) - 1 else "├─"
        print(f"{c(branch, 'dim')} {c(fl, 'bold')}  "
              f"{c(GLYPH_OK if not dry_run else GLYPH_UP, 'green' if not dry_run else 'cyan')} "
              f"{c(f'{verb}: ' + ', '.join(folders), 'dim')}")
    if registry_dropped and dry_run:
        print(c(f"   (would also drop {name} from {registry_path.name})", "dim"))
    elif registry_dropped:
        print(f"   {c('-', 'red')} dropped from {registry_path.name}")
    elif not keep_registry and name in registry:
        print(c(f"   (kept {name} in {registry_path.name}; another flavor still uses it)",
                "dim"))
    return 0


def cmd_pin(name, registry, registry_path, pinned):
    """Set or clear the pin flag on a registry entry."""
    if name not in registry:
        sys.exit(f"error: {name!r} is not in {registry_path.name}.")
    try:
        set_pin(name, registry, pinned, registry_path)
    except OSError as e:
        sys.exit(f"error: cannot update {registry_path}: {e}")
    verb = "pinned" if pinned else "unpinned"
    glyph = GLYPH_PIN if pinned else GLYPH_OK
    print(f"{c(glyph, 'cyan' if pinned else 'green')} {c(name, 'bold')} {c(verb, 'dim')}")
    return 0


# ---------------------------------------------------------------------------
# Interactive TUI (curses; stdlib only)
# ---------------------------------------------------------------------------

def run_tui(wow_roots, registry, registry_path):
    """Launch the interactive curses UI. Returns a process exit code."""
    import curses

    flavors = all_flavors(wow_roots)
    if not flavors:
        sys.exit("error: no WoW flavor with an Interface/AddOns folder found.")

    def app(stdscr):
        pairs = {}
        if curses.has_colors():
            # Prefer the terminal's default background (-1); some terminals
            # don't support it and raise, so fall back to the standard black.
            try:
                curses.use_default_colors()
                bg = -1
            except curses.error:
                bg = curses.COLOR_BLACK
            spec = {"green": curses.COLOR_GREEN, "cyan": curses.COLOR_CYAN,
                    "red": curses.COLOR_RED, "yellow": curses.COLOR_YELLOW}
            for i, (nm, col) in enumerate(spec.items(), start=1):
                try:
                    curses.init_pair(i, col, bg)
                    pairs[nm] = curses.color_pair(i)
                except curses.error:
                    pass
        try:
            curses.curs_set(0)
        except curses.error:
            pass

        def attr(style):
            if style == "dim":
                return curses.A_DIM
            if style == "bold":
                return curses.A_BOLD
            return pairs.get(style, 0)

        rows = gather_status(flavors, registry)   # offline first paint
        sel = 0
        marked = set()          # indices selected with space
        status = "Press ? for keys"
        checked = False

        def apply_rows(new_rows, check):
            nonlocal rows, sel, marked, checked
            rows = new_rows
            checked = check
            marked = {i for i in marked if i < len(rows)}
            sel = min(sel, max(0, len(rows) - 1))

        def rebuild(check=False):
            # Offline (check=False) is fast and safe on the main thread; the
            # networked check path runs through run_worker instead.
            apply_rows(gather_status(flavors, registry, check=check), check)

        def run_worker(work, label):
            """Run blocking `work()` in a thread; keep the UI live meanwhile.

            `work(cancelled, report)` receives a `cancelled()` predicate it
            should poll during long loops and a `report(msg)` callback to show
            progress. Returns (result, cancelled_flag, error): result is work()'s
            return value (None on error/cancel), and exactly one of cancelled or
            error may be set. All curses drawing stays here on the main thread;
            the worker only computes and reports strings.
            """
            nonlocal status
            result_q = queue.Queue()
            cancel = threading.Event()
            progress = [label]   # latest message; read by the render loop only

            def report(msg):
                progress[0] = msg

            def runner():
                try:
                    result_q.put(("ok", work(cancel.is_set, report)))
                except BaseException as exc:  # surfaced to the main thread
                    result_q.put(("err", exc))

            worker = threading.Thread(target=runner, daemon=True)
            worker.start()
            spinner = "|/-\\"
            tick = 0
            stdscr.timeout(120)   # non-blocking getch; ~8 fps spinner
            try:
                while True:
                    try:
                        kind, payload = result_q.get_nowait()
                    except queue.Empty:
                        pass
                    else:
                        if kind == "ok":
                            return payload, False, None
                        return None, False, payload
                    status = f"{spinner[tick % len(spinner)]} {progress[0]} — [esc] cancel"
                    tick += 1
                    draw()
                    k = stdscr.getch()
                    if k in (27, ord("q")):
                        cancel.set()
                        status = f"Cancelling {label}…"
                        draw()
                        worker.join()   # let it observe the flag and unwind
                        try:
                            result_q.get_nowait()   # discard late result
                        except queue.Empty:
                            pass
                        return None, True, None
            finally:
                stdscr.timeout(-1)   # restore blocking input

        def prompt(win, label):
            """Read a line of text from the user at the bottom of the screen."""
            maxy, maxx = win.getmaxyx()
            curses.curs_set(1)
            curses.echo()
            win.addstr(maxy - 1, 0, " " * (maxx - 1))
            win.addstr(maxy - 1, 0, label)
            win.refresh()
            try:
                text = win.getstr(maxy - 1, len(label), maxx - len(label) - 2)
            except curses.error:
                text = b""
            curses.noecho()
            curses.curs_set(0)
            return text.decode("utf-8", "replace").strip()

        def draw():
            stdscr.erase()
            maxy, maxx = stdscr.getmaxyx()
            if maxy < 6 or maxx < 40:
                try:
                    stdscr.addstr(0, 0, "Terminal too small — resize me.")
                except curses.error:
                    pass
                stdscr.refresh()
                return
            title = "wow-addon-updater"
            stdscr.addstr(0, 1, title, curses.A_BOLD)
            stdscr.addstr(0, 2 + len(title), "· interactive", curses.A_DIM)
            stdscr.addstr(1, 1, "─" * (maxx - 2), curses.A_DIM)

            name_w = min(max((len(r["name"]) for r in rows), default=4), 28)
            flav_w = max((len(r["flavor_display"]) for r in rows), default=6)
            top = 2
            body_h = maxy - 5
            start = max(0, sel - body_h + 1)
            for vis, r in enumerate(rows[start:start + body_h]):
                idx = start + vis
                y = top + vis
                if r["pinned"]:
                    glyph, style = GLYPH_PIN, "cyan"
                elif not r["in_registry"]:
                    glyph, style = GLYPH_SKIP, "dim"
                elif r["check_error"]:
                    glyph, style = GLYPH_FAIL, "red"
                elif r["has_update"]:
                    glyph, style = GLYPH_UP, "cyan"
                elif checked:
                    glyph, style = GLYPH_OK, "green"
                else:
                    glyph, style = GLYPH_CURRENT, "dim"
                cursor = "▸" if idx == sel else " "
                mark = "•" if idx in marked else " "
                detail = r["version"]
                if r["has_update"]:
                    detail = f"{r['version']} → {r['latest']}"
                elif r["pinned"]:
                    detail = f"{r['version']} (pinned)"
                elif not r["in_registry"]:
                    detail = "(not in registry)"
                elif r["check_error"]:
                    detail = f"check failed: {r['check_error']}"
                line = (f" {cursor}{mark}{glyph} {r['name'][:name_w].ljust(name_w)}  "
                        f"{r['flavor_display'].ljust(flav_w)}  {detail}")
                row_attr = attr(style)
                if idx == sel:
                    row_attr |= curses.A_REVERSE
                try:
                    stdscr.addstr(y, 0, line[:maxx - 1], row_attr)
                except curses.error:
                    pass
            stdscr.addstr(maxy - 3, 1, "─" * (maxx - 2), curses.A_DIM)
            keys = ("[↑↓/jk] move  [space] mark  [u] update  [c] check  "
                    "[r] remove  [p] pin  [/] search  [?] help  [q] quit")
            try:
                stdscr.addstr(maxy - 2, 1, keys[:maxx - 2], curses.A_DIM)
                stdscr.addstr(maxy - 1, 1, status[:maxx - 2])
            except curses.error:
                pass
            stdscr.refresh()

        def targets():
            """Indices to act on: the marked set, or the cursor row."""
            return sorted(marked) if marked else ([sel] if rows else [])

        def do_update(indices):
            nonlocal status
            # Snapshot the (name, spec, flavor) work list on the main thread so
            # the worker never touches `rows` while it may be shifting.
            jobs = []
            for idx in indices:
                r = rows[idx]
                spec = registry.get(r["name"])
                if not spec or spec.get("pin"):
                    continue
                jobs.append((r["name"], spec, r["flavor_obj"]))

            def work(cancelled, report):
                done, failed, last_error = 0, 0, None
                with tempfile.TemporaryDirectory(prefix="wow-addon-updater-") as wd:
                    fetch_cache = {}
                    for name, spec, flavor in jobs:
                        if cancelled():
                            break
                        report(f"Updating {name}…")
                        try:
                            if name not in fetch_cache:
                                fetch_cache[name] = fetch_addon(name, spec, Path(wd))
                            latest, source_dir = fetch_cache[name]
                            state = load_state(flavor.addons_dir)
                            previous = state.get(name, {}).get("folders", [])
                            folders = install_addon(spec, source_dir, flavor, latest, previous)
                            updated_state = dict(state)
                            updated_state[name] = {"version": latest, "folders": folders}
                            save_state(flavor.addons_dir, updated_state)
                            done += 1
                        except (urllib.error.URLError, LookupError, ValueError,
                                OSError, re.error) as e:
                            failed += 1
                            last_error = f"{name}: {e}"
                return done, failed, last_error

            result, cancelled, error = run_worker(work, "Updating")
            if cancelled:
                status = "Update cancelled"
            elif error:
                status = f"Update failed: {error}"
            else:
                done, failed, last_error = result
                status = f"Updated {done}" + (f", {failed} failed" if failed else "")
                if last_error:
                    status += f" — {last_error}"
            marked.clear()
            rebuild()

        def do_remove(indices):
            nonlocal status
            try:
                removed = 0
                for idx in list(indices):
                    r = rows[idx]
                    flavor = r["flavor_obj"]
                    res, _ = remove_addon(
                        r["name"], [flavor], registry, drop_registry=True,
                        registry_path=registry_path, all_known_flavors=flavors)
                    removed += len(res)
                status = f"Removed {removed} folder-set(s)"
            except (OSError, ValueError) as e:
                status = f"Remove failed: {e}"
            marked.clear()
            rebuild()

        def do_pin(indices):
            nonlocal status
            try:
                for idx in indices:
                    r = rows[idx]
                    if r["in_registry"]:
                        set_pin(r["name"], registry, not r["pinned"], registry_path)
                status = "Toggled pin"
            except OSError as e:
                status = f"Pin failed: {e}"
            marked.clear()
            rebuild()

        def do_search():
            nonlocal status
            query = prompt(stdscr, "Search GitHub: ")
            if not query:
                return

            def search_work(cancelled, report):
                return search_github_addons(query, 10)[0]

            items, cancelled, error = run_worker(search_work, f"Searching {query}")
            if cancelled:
                status = "Cancelled"
                return
            if error:
                status = f"Search failed: {error}"
                return
            if not items:
                status = "No results"
                return
            # Simple picker overlay.
            pick = 0
            while True:
                stdscr.erase()
                maxy, maxx = stdscr.getmaxyx()
                stdscr.addstr(0, 1, f"Results for {query}", curses.A_BOLD)
                for i, repo in enumerate(items[:maxy - 3]):
                    marker = "▸" if i == pick else " "
                    desc = (repo.get("description") or "")[:maxx - 30]
                    line = f" {marker} ★{repo['stargazers_count']:>5}  {repo['full_name']}  {desc}"
                    a = curses.A_REVERSE if i == pick else 0
                    try:
                        stdscr.addstr(1 + i, 0, line[:maxx - 1], a)
                    except curses.error:
                        pass
                stdscr.addstr(maxy - 1, 1, "[↑↓] move  [enter] install  [esc] cancel", curses.A_DIM)
                stdscr.refresh()
                k = stdscr.getch()
                if k in (curses.KEY_UP, ord("k")):
                    pick = max(0, pick - 1)
                elif k in (curses.KEY_DOWN, ord("j")):
                    pick = min(len(items) - 1, pick + 1)
                elif k in (27, ord("q")):
                    status = "Cancelled"
                    return
                elif k in (curses.KEY_ENTER, 10, 13):
                    break
            repo = items[pick]
            name = repo["full_name"].split("/")[-1]
            spec = registry.get(name) or suggest_registry_spec(repo)

            def install_work(cancelled, report):
                report(f"Installing {name}…")
                with tempfile.TemporaryDirectory(prefix="wow-addon-updater-") as wd:
                    latest, source_dir = fetch_addon(name, spec, Path(wd))
                    installed, failed, last_error = 0, 0, None
                    for flavor in flavors:
                        if cancelled():
                            break
                        report(f"Installing {name} → {flavor.name}…")
                        try:
                            state = load_state(flavor.addons_dir)
                            previous = state.get(name, {}).get("folders", [])
                            folders = install_addon(
                                spec, source_dir, flavor, latest, previous)
                            updated_state = dict(state)
                            updated_state[name] = {"version": latest, "folders": folders}
                            save_state(flavor.addons_dir, updated_state)
                            installed += 1
                        except (OSError, ValueError) as e:
                            failed += 1
                            last_error = f"{flavor.name}: {e}"
                    if name not in registry and installed:
                        updated_registry = dict(registry)
                        updated_registry[name] = spec
                        save_registry(updated_registry, registry_path)
                        registry[name] = spec
                return latest, installed, failed, last_error

            result, cancelled, error = run_worker(install_work, f"Installing {name}")
            if cancelled:
                status = f"Install of {name} cancelled"
            elif error:
                status = f"Install failed: {error}"
            else:
                latest, installed, failed, last_error = result
                status = f"Installed {name} {latest} into {installed} flavor(s)"
                if failed:
                    status += f", {failed} failed"
                    if last_error:
                        status += f" — {last_error}"
            rebuild()

        def show_help():
            """Overlay the key bindings until any key is pressed."""
            lines = [
                "wow-addon-updater — keys",
                "",
                "  ↑ / k        move up",
                "  ↓ / j        move down",
                "  space        mark / unmark row",
                "  u            update selection",
                "  c            check GitHub for updates",
                "  r            remove selection",
                "  p            pin / unpin selection",
                "  /            search GitHub and install",
                "  ?            this help",
                "  q / Esc      quit",
                "",
                "Actions apply to marked rows, or the cursor row if none.",
                "",
                "Press any key to return.",
            ]
            stdscr.erase()
            maxy, maxx = stdscr.getmaxyx()
            for i, text in enumerate(lines[:maxy - 1]):
                a = curses.A_BOLD if i == 0 else curses.A_DIM
                try:
                    stdscr.addstr(i, 1, text[:maxx - 2], a)
                except curses.error:
                    pass
            stdscr.refresh()
            stdscr.getch()

        draw()
        while True:
            k = stdscr.getch()
            if k in (ord("q"), 27):
                return
            elif k in (curses.KEY_UP, ord("k")):
                sel = max(0, sel - 1)
            elif k in (curses.KEY_DOWN, ord("j")):
                sel = min(max(0, len(rows) - 1), sel + 1)
            elif k == ord(" "):
                if rows:
                    marked.symmetric_difference_update({sel})
            elif k == ord("c"):
                def check_work(cancelled, report):
                    return gather_status(flavors, registry, check=True)
                new_rows, cancelled, error = run_worker(check_work, "Checking for updates")
                if cancelled:
                    status = "Check cancelled"
                elif error:
                    status = f"Check failed: {error}"
                else:
                    apply_rows(new_rows, True)
                    status = "Update check complete"
            elif k == ord("u"):
                if rows:
                    do_update(targets())
            elif k == ord("r"):
                if rows:
                    do_remove(targets())
            elif k == ord("p"):
                if rows:
                    do_pin(targets())
            elif k == ord("/"):
                do_search()
            elif k == ord("?"):
                show_help()
            elif k == curses.KEY_RESIZE:
                pass
            draw()

    return curses.wrapper(app) or 0


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
    parser.add_argument("--no-color", action="store_true",
                        help="disable colored output (also honors NO_COLOR / FORCE_COLOR)")

    sub = parser.add_subparsers(dest="command")
    p_search = sub.add_parser("search", help="search GitHub for addons to add to the registry")
    p_search.add_argument("terms", nargs="+", help="search terms (e.g. 'bag', 'boss timer')")
    p_search.add_argument("--limit", type=int, default=10,
                          help="max results to show (default: 10)")
    p_search.add_argument("--all", action="store_true",
                          help="don't filter out non-WoW (FiveM/GMod) Lua repos")
    p_install = sub.add_parser("install",
                               help="add an addon to the registry and install it")
    p_install.add_argument("target",
                           help="a search result number, an owner/repo slug, or a registry name")
    p_install.add_argument("--flavor",
                           help="install only into this flavor (e.g. _retail_); skips the prompt")
    p_list = sub.add_parser("list", help="show installed addons per flavor")
    p_list.add_argument("--check", action="store_true",
                        help="also query GitHub to flag addons with updates available")
    p_remove = sub.add_parser("remove", help="uninstall an addon and drop it from the registry")
    p_remove.add_argument("name", help="the addon (folder) name to remove")
    p_remove.add_argument("--flavor", help="remove only from this flavor; skips the prompt")
    p_remove.add_argument("--keep-registry", action="store_true",
                          help="uninstall the files but keep the addons.json entry")
    p_pin = sub.add_parser("pin", help="hold an addon at its installed version (skip on update)")
    p_pin.add_argument("name", help="the registry addon name to pin")
    p_unpin = sub.add_parser("unpin", help="allow a pinned addon to update again")
    p_unpin.add_argument("name", help="the registry addon name to unpin")
    sub.add_parser("tui", help="launch the interactive terminal UI")

    args = parser.parse_args()

    global COLOR
    COLOR = color_enabled(args.no_color)

    try:
        registry = json.loads(args.registry.read_text())
    except (OSError, json.JSONDecodeError) as e:
        sys.exit(f"error: cannot read registry {args.registry}: {e}")

    if args.command == "search":
        return cmd_search(" ".join(args.terms), max(1, min(args.limit, 100)),
                          registry, wow_only=not args.all)
    if args.command == "pin":
        return cmd_pin(args.name, registry, args.registry, True)
    if args.command == "unpin":
        return cmd_pin(args.name, registry, args.registry, False)

    wow_roots = [d.expanduser() for d in args.wow_dir] or find_wow_roots()
    if not wow_roots:
        sys.exit("error: no World of Warcraft install found in any Steam Proton prefix.\n"
                 "Pass --wow-dir /path/to/World of Warcraft to point at it directly.")

    if args.command == "install":
        return cmd_install(args.target, registry, args.registry, wow_roots,
                           args.flavor, args.dry_run)
    if args.command == "list":
        return cmd_list(wow_roots, registry, args.check)
    if args.command == "remove":
        return cmd_remove(args.name, wow_roots, registry, args.registry,
                          args.flavor, args.keep_registry, args.dry_run)
    if args.command == "tui":
        return run_tui(wow_roots, registry, args.registry)

    cache = {}
    exit_code = 0
    with tempfile.TemporaryDirectory(prefix="wow-addon-updater-") as workdir:
        for wow_root in wow_roots:
            print(f"{c('World of Warcraft', 'bold')}{c(' · ' + str(wow_root), 'dim')}")
            flavors = find_flavors(wow_root)
            if not flavors:
                print(f"{c('└─', 'dim')} {c('no flavor with an Interface/AddOns folder found', 'dim')}")
                continue
            for i, flavor in enumerate(flavors):
                try:
                    counts = update_flavor(
                        flavor, registry, cache, Path(workdir), args.dry_run,
                        is_last=(i == len(flavors) - 1))
                    if counts["failed"]:
                        exit_code = 1
                except (OSError, ValueError) as e:
                    print(f"   {c(GLYPH_FAIL, 'red')} {c(flavor.name, 'bold')}  {c(f'FAILED ({e})', 'red')}")
                    exit_code = 1
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
