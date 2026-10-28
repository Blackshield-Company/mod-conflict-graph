# mod-conflict-graph

A Python 3.8+ standard-library tool that compares Skyrim SE mod repositories and release packages. It produces an evidence-labelled conflict graph, Markdown matrix, and Mermaid diagram. It reads inputs without modifying them and never extracts release archives to disk.

```sh
python3 -m modconflict --clones /tmp/work/clones --releases /tmp/work/releases --out ./out
bin/mod-conflict-graph --releases /tmp/work/releases --mods alpha,beta --extra optional-mod
python3 -m unittest discover -s tests
```

Place clones in `clones/<repo>/` and release zips in `releases/<repo>/*.zip`. An unpacked release directory is also accepted. At least one input directory is required. Repository names found in either directory identify mods. When a release zip exists, all clone plugins are ignored; clone documentation and owned source scripts supply additional facts. All provided archives are analysed conservatively, retaining distinct plugin variants and reporting this limitation.

`--mods a,b` selects repositories. `--extra a,b` includes repositories outside the primary set and marks them in reports. `--out DIR` defaults to `./out`. `--json-only` writes only JSON; `--quiet` suppresses the success message. `--full` includes the full parsed record facts in JSON. `--no-timestamp` omits the otherwise UTC ISO timestamp for reproducible output.

`--fail-on collision` exits 1 for collisions. `--fail-on probable` exits 1 for probable findings or collisions. The default, `--fail-on never`, exits 0 after successful output. Invalid arguments, missing input directories, unsafe archives, and input/output errors exit 2. Findings do not prevent writing reports.

The optional `scripts/fetch-sources.sh --work /tmp/mcg-work --owner OWNER --repos "a b"` helper uses installed `git`, `gh`, `timeout`, and `nice` to fetch shallow clones and latest release zips, falling back to the newest prerelease. It performs sequential, time-limited downloads, never pushes, and prints the analysis command. The main tool does not invoke this helper or require these programs.

## Evidence and severity

Every finding contains `verified` or `inferred` evidence entries with sources and details:

- **Verified**: plugin headers, master tables, records, VMAD properties, placements, archive file metadata, and FOMOD XML. Loose plugin bytes are also verified.
- **Inferred**: documentation claims, Papyrus source patterns, pinned source IDs, inferred property ownership, and assumptions about loose-file installation. Script-name collisions use each mod's own evidence label: PEX/VMAD names are verified; PSC-only names are inferred.

Severity order is `collision > probable > warning > dependency > info > none`. A pair receives its worst severity. Evidence reliability and impact severity are separate: nearby placements have verified coordinates but only probable impact because 128 units is a heuristic.

| Kind | Meaning |
| --- | --- |
| `plugin-name` | Plugins share a filename, ignoring case. |
| `record-override` | Two plugins override the same owner/local ID; overriding an analysed owner's record also establishes a load dependency. |
| `editor-id` | New records share an EditorID; overrides are excluded. |
| `script-name`, `script-on-same-record` | Shared script names or script attachments to the same overridden record. |
| `file-path` | All-option installed destinations overlap. Matching size and CRC32 are info-level identical duplicates; differing files collide. |
| `voice-folder`, `seq` | Shared voice plugin folders or sequence filenames. |
| `load-order`, `master-cycle` | Analysed master dependencies and cyclic hard load requirements. |
| `external-master`, `master-ordering-note`, `vanilla-edit` | External masters (base/DLC, CC, third party), light/ESP master notes, or singly overridden external records. |
| `record-reference`, `dangling-reference` | VMAD object properties, forced references, or placement base references to analysed plugins; missing targets collide. |
| `soft-reference`, `stale-form-id`, `external-soft-reference` | Source GetFormFromFile dependencies, missing/type-mismatched targets, or external targets. |
| `pinned-id-mismatch`, `pinned-id-drift` | Pinned source IDs disagree with plugin EditorIDs or resolved source references. |
| `script-call` | Source references another mod's script; renaming can break the soft dependency. |
| `foreign-write`, `write-write` | Writes through properties with another prefix, or shared writes through foreign properties. |
| `modevent`, `engine-setting` | Shared ModEvent names or literal GameSetting/INI keys. |
| `placement-overlap`, `same-cell`, `alias-forced-ref` | Nearby/same-cell placements or quests forcing the same external reference. |
| `doc-mismatch`, `documented-incompatibility`, `documented-interaction` | Contradicted claims, stated incompatibilities, or documentation-only integration. |
| `esl-range`, `esl-budget` | ESL new-record IDs outside 0x800–0xFFF, or usage of 4096 light slots. |
| `referenced-but-missing`, `shipped-but-never-installed` | Per-mod FOMOD information, not pair collisions. |
| `package-plugin-variants` | Multiple distinct plugin variants were retained from the inputs. |

## Output

- `graph.json`: version, inputs, mod summaries and counts, overrides, scripts, property links, findings, every pair's status, dependency edges, suggested hard load order, cycles, and statistics. Full record lists require `--full`; raw payloads are never emitted.
- `report.md`: severity counts, per-mod table, symmetric matrix (`.`/`i`/`d`/`w`/`P`/`X`), findings grouped by severity and kind, evidence explanations, and input-specific gaps. Extra mods have `*` markers.
- `graph.mmd`: safe mod node identifiers, extra-mod styling, solid dependency edges, thick collision edges, and dashed probable/warning edges. Info-only pairs are omitted.

Outputs sort findings and facts deterministically, apart from the optional timestamp. Colliding item lists are capped at 25; finding and evidence details are also capped at 300 characters, with `... +N more` for omitted items. Load edges point from the dependent mod to the required mod; the suggested plugin order places requirements first, starting with `Skyrim.esm`. External masters appear in that order; `mod_order` also provides an order of mod groups when they can be kept together. Plugin cycles are checked even within a single mod. Cyclic components are reported and cannot receive a complete topological order. ESL-flagged ESP master ordering notes are aggregated once per dependent mod.

## Limits and data handling

This is a static review aid. It does not simulate gameplay, evaluate Papyrus control flow, run FOMOD conditions, resolve runtime load indices, or inspect BSA contents. It recognises VMAD script lists and object properties, not quest fragment/alias script tails. It does not scan arbitrary record payload words for references. ModEvent registration versus sending does not establish impact by itself. Property prefixes and source patterns are heuristics; indirect calls, computed strings, and dynamic references can be missed.

Clone source scanning is limited to `src/papyrus/**` and direct `scripts/*.psc` files. Packaged names come from PEX files under `Scripts/`, PSC files under `Source/Scripts/`, and VMAD attachments. Paths are matched without case sensitivity and may include package option prefixes. Script scanning excludes directories named `tools`, `masters`, `vendor`, `vendored`, `third_party`, `node_modules`, `papyrus-stubs`, `stubs`, `tests`, `test`, `fixtures`, `build`, `dist`, `.git`, `.github`, or `docs`. Clone plugin fallback uses the same exclusions and records the gap `plugin data from clone (no release zip)`. Scripts elsewhere are not attributed to the mod; the report's Scripts count uses this filtered union. Archive file metadata still includes every shipped file.

Script calls must resolve to another analysed mod's own script names. Foreign writes must match another analysed mod's prefix; write-write findings require a third analysed owner. GetFormFromFile validation compares the low 24 bits against the target plugin's new records, checks recognised casts, and compares resolved EditorIDs with pinned IDs. Missing targets and mismatched casts collide. Shared quest alias targets in `IGNORED_FORCED_REFS = {("skyrim.esm", 0x14)}` are excluded from alias conflicts because the Player reference is routinely used; each mod reports its `ignored_forced_refs` count. Other shared external forced references remain probable findings.

FOMOD's all-options set deliberately includes alternatives and conditional files; mutually exclusive options can produce conservative collisions. An unmatched installation source and a shipped but unreferenced file remain visible. Archive-only input cannot supply pinned clone source IDs. Release README/docs are used when clone copies are absent. Missing README, absent release zip/plugin data, malformed metadata/VMAD, BSA contents, combined release variants, and unevaluated conditional installs are recorded under Gaps when encountered. Successful parsing does not establish universal compatibility.

Archive paths with absolute names, drive prefixes, or `..` components are rejected. Archives exceeding 200 MB uncompressed or 20,000 directory entries are refused before member reads. Archives are read using `zipfile` only. Symlinked inputs are skipped. Identical file detection uses CRC32 and size, not cryptographic identity. ESL ownership comparisons retain all 24 local-ID bits, including references to light masters.

No Bethesda assets or plugin/script files are copied into the repository or outputs. Tests create synthetic plugin bytes, source text, and archives only in temporary directories. Output contains names, IDs, paths, flags, positions, counts, and evidence descriptions, never script source lines or raw asset payloads. There are no external Python dependencies.

Each release archive uses its own FOMOD configuration and file inventory; possible installed destinations are combined conservatively. Plugin name collisions use installed destination filenames, including unreadable plugins. Compressed records are limited to their declared size and a 64 MiB decompression cap; malformed or oversized bodies are skipped with parse gaps. Malformed XML encodings and pinned IDs also produce gaps.

Documented load-after requests contribute inferred constraints to suggested ordering; master-cycle findings remain based on plugin master tables. Vanilla edit claims cover base/DLC masters and Creation Club names beginning with `cc` plus a three-letter studio code and `sse`. Overrides of other unanalysed masters receive inferred external-master override warnings.
