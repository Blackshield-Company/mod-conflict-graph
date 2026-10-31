# modcheck

`modcheck` is a pre-release checker for a single mod. It is a subcommand of the existing CLI and uses the conflict-graph engine (`modconflict.extract.build_mod` and `modconflict.analyze.analyze`) as a library. Like the rest of the tool it uses only the standard library and reads its inputs without modifying them.

```sh
bin/mod-conflict-graph modcheck path/to/MyMod
python3 -m modconflict modcheck path/to/MyMod --against ../OtherMod --against ../ThirdMod
bin/mod-conflict-graph modcheck . --release dist/ --tag v1.2.0 --out modcheck.md
```

Every finding has a level: **blocker**, **warning**, or **info**. The Markdown report lists blockers and warnings, a version table, the suggested load order (when peers are given), and collapsible info and ignored sections. By default the report goes to stdout. `--out FILE` writes it to a file instead, and `--summary FILE` appends it to a file such as `$GITHUB_STEP_SUMMARY`.

Exit status: `0` passes, `1` means a finding at or above `--fail-on` (default `blocker`; `warning` is stricter; `never` always passes), and `2` covers usage, config, and input errors.

## Checks

### Masters (`masters`)

modcheck reads the TES4 header of every shipped `.esp`/`.esm`/`.esl` (only the header; records are not parsed) and lists its `MAST` entries. If `--release` contains plugins, those plugins are used. Otherwise it uses plugins in the mod directory, skipping the engine's excluded directories (`tests`, `fixtures`, `build`, `dist`, `docs`, `tools`, ...) and dot-directories.

Each master must be one of the following:

- a plugin the mod ships itself, or a plugin a FOMOD `file` element installs;
- an implicit master (by default `Skyrim.esm` and `Update.esm`);
- named in a FOMOD `fileDependency` anywhere in `fomod/ModuleConfig.xml` (module dependencies, option dependencies, conditional installs);
- named anywhere in the text of `fomod/info.xml`;
- named in a top-level `README`, `README.md`, or `README.txt`;
- listed in `[masters] declared` or in the `[masters] requirements_file` (one name per line, `#` comments).

Names are compared case-insensitively. A master that matches none of these is an `undeclared-master` blocker. Declared masters appear as info with the source that declared them. An unreadable plugin is a blocker. DLC masters (`Dawnguard.esm` and the others) are not implicit, so declare them or add them to `implicit`.

### Conflicts (`conflicts`)

The mod is built as a *clone* (git checkout), combined with `--release` if one is given. Each `--against DIR` (and each `[conflicts] against` entry, resolved relative to the config file) is read as a clone if it contains `.git`, otherwise as a release folder: a folder of zips or an unpacked package. Peers are named after their directory and names must be unique. All of them go through the engine together. Only findings that involve the checked mod are kept, which includes single-mod facts such as `esl-range` and `master-cycle`.

Engine severities at or above `[conflicts] fail_on` (default `collision`) become blockers. Other `collision`/`probable`/`warning` findings become warnings, and `dependency`/`info` findings become info. Engine gaps for the mod (for example a missing README) are reported as info. The finding kinds are documented in the main README.

### Version drift (`version`)

modcheck collects versions from:

- `<Version>` in each `fomod/info.xml` (mod directory and release);
- the plugin description (TES4 `SNAM`) when it contains `Version: 1.2.3` / `version v1.2.3` or starts with a version (`1.2.3 - My Mod`); other `v1.2` mentions such as SKSE requirements are ignored (turn this off with `plugin_description = false`);
- the top released heading of the changelog (`## [1.2.0] - date`, `## v1.2.0`, `# 1.2`). modcheck tries `CHANGELOG.md`, `CHANGELOG`, `CHANGES.md`, and `docs/CHANGELOG.md` unless `[version] changelog` names a file. `[Unreleased]` headings are skipped and produce a `changelog-unreleased` warning.

Versions are compared as dotted numbers. Trailing zeros are ignored (`1.2` equals `1.2.0`), a leading `v` is ignored, and a SemVer pre-release sorts before its release.

| Kind | Level | Condition |
| --- | --- | --- |
| `version-mismatch` | blocker | The collected versions disagree. |
| `tag-mismatch` | blocker | A tag at HEAD (or `--tag`) differs from the version. |
| `version-behind-tag` | blocker | HEAD is untagged and the version is older than the latest reachable tag. |
| `version-not-bumped` | warning | HEAD is past the latest tag but the version still equals it. |
| `missing-changelog`, `changelog-no-version`, `version-unparsed`, `no-version` | warning | Missing or unusable data. A configured changelog that is missing is a blocker. |
| `git-unavailable` | info | Not a git work tree, or git is missing or fails. With no matching tags, no tag comparison is made. |

Git is queried with read-only commands only: `rev-parse --is-inside-work-tree`, `tag --points-at HEAD`, and `describe --tags --abbrev=0`. Only tags that start with `tag_prefix` (default `v`) are considered. `--tag` replaces the git lookup, and `--no-git` (or `git = false`) skips it. In CI, check out with `fetch-depth: 0` so that tags are present.

## modcheck.toml

modcheck reads `PATH/modcheck.toml` automatically, or the file given with `--config`. Reading it requires Python 3.11+ (`tomllib`). Without a config file modcheck runs on Python 3.8+. Unknown keys and wrong types are errors (exit 2).

```toml
name = "My Mod"              # report title; default: directory name
fail_on = "blocker"          # blocker | warning | never (CLI --fail-on overrides)

[masters]
declared = ["SkyUI_SE.esp", "Dawnguard.esm"]
implicit = ["Skyrim.esm", "Update.esm"]   # default shown
requirements_file = "REQUIREMENTS"        # optional, relative to the mod directory

[conflicts]
against = ["../OtherMod", "../ThirdMod"]  # relative to this file; added to --against
fail_on = "collision"                     # collision | probable | warning | never

[version]
changelog = "CHANGELOG.md"
tag_prefix = "v"
git = true
plugin_description = true

[[ignore]]
check = "conflicts"          # masters | conflicts | version | config | *
kind = "file-path"           # finding kind; glob patterns allowed
subject = "OtherMod"         # master name, peer mod, file, or tag; glob patterns allowed
reason = "Identical shared textures, coordinated with OtherMod"
```

An ignore rule matches when all of the fields it sets match. Matching is case-insensitive with shell-style globs. Each rule needs a `reason` and at least one of `check`, `kind`, or `subject`. Ignored findings stay visible in the report's *Ignored* section along with their reason. A rule that matches nothing is reported as an `unused-ignore` info finding.

## GitHub Action

`action.yml` in the repository root is a composite action. It runs `modcheck` with the runner's `python3`, writes the report to `$RUNNER_TEMP/modcheck-report.md` (or to the `report` input) and appends it to `$GITHUB_STEP_SUMMARY`. The step fails with modcheck's exit status. On tag pushes the pushed tag is used as `--tag` unless the `tag` input is set.

```yaml
name: modcheck
on:
  push:
    tags: ['v*']
  pull_request:

jobs:
  modcheck:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0          # tags are needed for version drift
      - uses: actions/checkout@v4
        with:
          repository: example-owner/other-mod
          path: peers/other-mod
      - uses: OWNER/mod-conflict-graph@v1
        with:
          against: |
            peers/other-mod
```

Inputs: `path`, `config`, `against` (one directory per line), `release`, `tag`, `fail-on`, `git`, `python`, `report`. Outputs: `report` (path) and `exit-code`. Inputs reach the script only through environment variables. Expressions are never expanded into the shell script.

## Library use

```python
from modconflict import modcheck

config = modcheck.load_config('modcheck.toml')   # or load_config(None) for defaults
result = modcheck.run('path/to/MyMod', config, against=['../OtherMod'], use_git=True)
print(modcheck.report(result))
raise SystemExit(1 if result['failed'] else 0)
```

`result['findings']` contains dicts with `check`, `level`, `kind`, `subject`, `detail`, `label`, and `source`. `modconflict.plugin.parse_header(data, name)` returns a record-free `Plugin` with `masters`, `flags`, `author`, and `description`.
