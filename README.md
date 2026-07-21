# wow-addon-updater

Update your World of Warcraft addons on Linux when WoW is installed through
**Steam Proton** — no Battle.net-in-Wine addon managers, no CurseForge account,
no API keys.

```console
$ ./update_addons.py
World of Warcraft · ~/.local/share/Steam/steamapps/compatdata/3403821404/pfx/drive_c/Program Files (x86)/World of Warcraft
└─ _anniversary_  2.5.6 (interface 20506)
   ✓ Questie       unknown → v11.32.1
   ✓ Auctionator   unknown → 582
   ── 2 addons · 2 updated ──
```

Output is colored on a terminal (green for updated, cyan for available in
`--dry-run`, red for failures, dim for up-to-date/skipped) and falls back to
plain text when piped or when `--no-color` / `NO_COLOR` is set.

## Why

Most addon managers rely on the CurseForge API, which requires an API key, or
need to run inside the same Wine prefix as the game. This tool instead:

- **Auto-detects** every WoW install inside a Steam Proton prefix
  (`steamapps/compatdata/...`), across all your Steam libraries.
- Handles **every flavor** you have installed — `_retail_`, `_classic_`,
  `_classic_era_`, `_anniversary_`, PTRs — each with the correct client
  version.
- Downloads addons **straight from GitHub**: either published release assets,
  or built from the latest tagged source when the project only publishes to
  key-gated stores (the `.toc` fields their CI would generate are patched in
  from your local client version).

## Requirements

- Python 3.8+ (standard library only — nothing to `pip install`)
- WoW installed via Steam Proton (Battle.net added as a non-Steam game, or any
  setup that puts `World of Warcraft` inside a `compatdata` prefix)

## Usage

```bash
git clone https://github.com/Lucas-Servi/wow-addon-updater
cd wow-addon-updater
./update_addons.py --dry-run          # see what would happen
./update_addons.py                    # update everything
./update_addons.py list               # what's installed, per flavor
./update_addons.py search bag         # find addons on GitHub
./update_addons.py install 1          # add + install a search result
./update_addons.py remove AdiBags     # uninstall + drop from registry
./update_addons.py tui                # interactive terminal UI
```

Global flags (`--dry-run`, `--wow-dir`, `--registry`, `--no-color`) go **before**
the subcommand, e.g. `./update_addons.py --dry-run install 1`.

Options:

| Flag | Effect |
|------|--------|
| `--dry-run` | Report what would be updated without touching anything |
| `--wow-dir PATH` | Skip auto-detection and use this `World of Warcraft` folder (repeatable) |
| `--registry PATH` | Use a different `addons.json` |
| `--no-color` | Disable colored output (also honors `NO_COLOR` / `FORCE_COLOR`) |

Only addons that are **already installed** and listed in `addons.json` are
updated. Installed addons without a registry entry are reported and left
untouched. Installed versions are tracked in a small
`.addon-updater.json` file inside each `Interface/AddOns` folder, so re-runs
are fast no-ops. Commands return a non-zero exit status when a fetch, validation,
installation, persistence, or explicit update check fails, so scheduled runs do
not silently report success.

## Finding and installing addons

`search` looks up addons on GitHub (by stars, filtered to `language:Lua`) so
you can discover what to add, and numbers each result:

```console
$ ./update_addons.py search bag
Searching GitHub for bag (language:Lua, by stars)
1. ★ 153  AdiAddons/AdiBags
      WoW Addon — Adirelle's bag addon.
2. ★  98  Cidan/BetterBags
      A total replacement AddOn for World of Warcraft bag frames…
3. ★  70  TheMouseNest/Baganator
      World of Warcraft Addon: Bag/bank overhaul.

→ install with: ./update_addons.py install <number>
```

`install` then adds the addon to `addons.json` **and** installs it in one step:

```console
$ ./update_addons.py install 1
Installing AdiBags (AdiAddons/AdiBags, github-release)
Install into which flavor?
  1) _anniversary_
  2) _classic_era_
  a) all
> a
   + added to addons.json
├─ _anniversary_   ✓ installed v1.10.29
└─ _classic_era_   ✓ installed v1.10.29
```

`install` accepts a **search result number** (from your last search), an
**`owner/repo`** slug, or the **name of an addon already in `addons.json`**. It
detects the right strategy automatically (`github-release` if the repo ships a
`.zip`, else `github-branch`), fetches, installs the addon's folders, patches
the `.toc` for your client, and records the version — the registry entry is
only written after at least one flavor installs successfully. It prompts for
which flavor(s) to install into; pass `--flavor _retail_` to skip the prompt
(useful for scripts), or `--dry-run` to preview without changing anything.

Search is a **live query** — there's no authoritative catalog of WoW addons to
index, and the complete ones (CurseForge/Wago) are the key-gated APIs this tool
avoids, so each run hits GitHub directly. `language:Lua` also matches the
FiveM/GMod ecosystems, so obvious non-WoW repos are filtered out by default (a
`N non-WoW results hidden` note tells you how many); pass `--all` to see them.
Use `--limit N` to show more results (default 10, max 100). Repos already in
your registry are flagged `(in registry)`. The last search's results are cached
under `~/.cache/wow-addon-updater/` so `install <number>` can resolve them.

## Listing, removing, and pinning

```console
$ ./update_addons.py list           # instant, offline
└─ _anniversary_  2 addons
   · Questie       v11.33.0
   ⇧ Auctionator   2026-07-17.9013aa8 (pinned)

$ ./update_addons.py list --check   # also queries GitHub for updates
   ↑ Questie       v11.33.0 → v11.34.0
```

- **`list`** shows what's installed in each flavor with status glyphs
  (`·` current, `↑` update available, `⇧` pinned, `–` not in the registry). It's
  network-free by default; add `--check` to flag which addons have newer
  versions on GitHub. Multiple WoW installations are grouped under their own
  root paths; duplicate flavor names are disambiguated in the TUI.
- **`remove <name>`** uninstalls an addon and drops it from `addons.json`. It
  only deletes the folders it recorded when installing (or a single folder that
  exactly matches the name) — it never guesses. Add `--flavor` to remove from
  one flavor, `--keep-registry` to uninstall the files but keep the entry, or
  `--dry-run` to preview. Flavor-scoped removal keeps the global registry entry
  while another discovered flavor still has the addon, and drops it with the
  final installed copy.
- **`pin <name>` / `unpin <name>`** freeze an addon at its installed version:
  `update` skips a pinned addon (shown as `⇧ pinned`) instead of fetching it —
  handy when a new release breaks something. Note: pinning **holds the current
  version**; it does not download a specific older one.

## Interactive TUI

`./update_addons.py tui` opens a keyboard-driven terminal UI (stdlib `curses`,
no extra install) over the same commands:

```
 wow-addon-updater · interactive
 ────────────────────────────────────────────────
  ▸ · Questie       _anniversary_  v11.33.0
    ⇧ Auctionator   _anniversary_  2026-… (pinned)
    – RandoAddon    _anniversary_  (not in registry)
 ────────────────────────────────────────────────
 [↑↓/jk] move  [space] mark  [u] update  [c] check
 [r] remove  [p] pin  [/] search  [q] quit
```

Move with the arrow keys or `j`/`k`, `space` to multi-select, then `u` to
update, `r` to remove, or `p` to pin the selection; `c` checks GitHub for
updates, `/` opens a search-and-install prompt, and `?` shows the full key list.
Network actions (update, check, search, install) run on a background thread with
a live progress spinner and can be cancelled with `Esc`, so the UI stays
responsive during a slow download.

## Adding addons manually

Edit `addons.json` (or start from a `search` snippet). Two source strategies
are supported:

**`github-release`** — for addons that publish ready-to-use zips on GitHub
Releases (the common case):

```json
"Questie": {
  "strategy": "github-release",
  "repo": "Questie/Questie",
  "asset": "^Questie-v[\\d.]+\\.zip$"
}
```

`asset` is a regex matched against the release's asset filenames. Direct
top-level addon folders, or addon folders beneath one release wrapper, are
recognized by a direct `.toc` file and copied into `Interface/AddOns`; unrelated
documentation/metadata folders are ignored, and a package without an addon
folder is rejected.

**`github-branch`** — for addons that only publish to CurseForge/Wago but keep
their source on GitHub and release straight from their main branch:

```json
"Auctionator": {
  "strategy": "github-branch",
  "repo": "TheMouseNest/Auctionator",
  "branch": "master",
  "package_as": "Auctionator"
}
```

The branch tip is downloaded as source and renamed to `package_as`.
Development-only paths are removed automatically (the repo's own `.pkgmeta`
`ignore:` list, top-level dotfiles, plus anything in an optional `"ignore"`
array). Ignore entries must stay inside the addon root; absolute and parent
paths are rejected. In the `.toc`, a `## Version:` build placeholder like
`@project-version@` is replaced with `YYYY-MM-DD.shortsha`, and the
`## Interface:` line is filled in from your installed client's version (read
from the game's own `.build.info` / `.flavor.info` files) — but only when the
checked-in line doesn't already list your client.

**`github-source`** — same packaging as `github-branch`, but pinned to the
newest git tag matching a pattern instead of a branch tip. Use it only when a
project's tags are actually kept current — a stale tag will silently get you
years-old code:

```json
"SomeAddon": {
  "strategy": "github-source",
  "repo": "author/SomeAddon",
  "tag_pattern": "v([\\d.]+)",
  "package_as": "SomeAddon"
}
```

`tag_pattern` group 1 must be an integer or numeric dotted version such as
`582` or `1.10.0`; dotted components are compared numerically.

Any entry may also carry `"pin": true` to hold it at its installed version
(managed by `pin`/`unpin`, or set by hand); `update` skips pinned addons.

## Limitations

- Sources must be on GitHub — either releases, or source that works without a
  packaging step beyond `.toc` field substitution (no `.pkgmeta` externals).
- Addons are matched by their folder name in `Interface/AddOns`.
- Pinning holds an addon at its **currently installed** version; it does not
  fetch a specific older release.

## Roadmap

- ~~`search` command: discover addons on GitHub~~ ✓ done
- ~~`install`: add an addon and install it from the terminal~~ ✓ done
- ~~`list`: show installed addons per flavor (`--check` for updates)~~ ✓ done
- ~~`remove <name>`: uninstall an addon and drop it from the registry~~ ✓ done
- ~~`pin`/`unpin` and an interactive TUI~~ ✓ done

## Development

Tests are stdlib `unittest` (no dependencies), run offline against a throwaway
addon tree:

```bash
python3 -m unittest test_update_addons.py -v
```

GitHub Actions runs the same suite and byte-compilation checks on Python 3.8
and 3.13.

## License

MIT — see [LICENSE](LICENSE).
