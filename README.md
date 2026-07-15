# wow-addon-updater

Update your World of Warcraft addons on Linux when WoW is installed through
**Steam Proton** — no Battle.net-in-Wine addon managers, no CurseForge account,
no API keys.

```console
$ ./update_addons.py
World of Warcraft @ ~/.local/share/Steam/steamapps/compatdata/3403821404/pfx/drive_c/Program Files (x86)/World of Warcraft
  _anniversary_ (2.5.6, interface 20506)
    Questie: updated (unknown -> v11.32.1)
    Auctionator: updated (unknown -> 582)
```

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
./update_addons.py --dry-run   # see what would happen
./update_addons.py             # update everything
```

Options:

| Flag | Effect |
|------|--------|
| `--dry-run` | Report what would be updated without touching anything |
| `--wow-dir PATH` | Skip auto-detection and use this `World of Warcraft` folder (repeatable) |
| `--registry PATH` | Use a different `addons.json` |

Only addons that are **already installed** and listed in `addons.json` are
updated. Installed addons without a registry entry are reported and left
untouched. Installed versions are tracked in a small
`.addon-updater.json` file inside each `Interface/AddOns` folder, so re-runs
are fast no-ops.

## Adding addons

Edit `addons.json`. Two source strategies are supported:

**`github-release`** — for addons that publish ready-to-use zips on GitHub
Releases (the common case):

```json
"Questie": {
  "strategy": "github-release",
  "repo": "Questie/Questie",
  "asset": "^Questie-v[\\d.]+\\.zip$"
}
```

`asset` is a regex matched against the release's asset filenames; the zip's
top-level folders are copied into `Interface/AddOns`.

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
array). In the `.toc`, a `## Version:` build placeholder like
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

`tag_pattern` group 1 must be a sortable number.

## Limitations

- Sources must be on GitHub — either releases, or source that works without a
  packaging step beyond `.toc` field substitution (no `.pkgmeta` externals).
- Addons are matched by their folder name in `Interface/AddOns`.

## Roadmap

- `list` command: show addons available in the registry vs. installed
- `install <name>`: install a registry addon from the terminal

## License

MIT — see [LICENSE](LICENSE).
