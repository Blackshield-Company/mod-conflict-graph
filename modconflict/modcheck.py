"""Pre-release checks for a single mod, built on the conflict-graph engine.

Three checks run against a mod directory (normally its git checkout):

* masters: every TES4 master must be declared by FOMOD, README, or config;
* conflicts: the engine's findings between this mod and other local mods;
* version: FOMOD info.xml, plugin description, CHANGELOG and git tag agree.

Inputs are read only. Git is queried with read-only commands.
"""
import argparse
from fnmatch import fnmatchcase
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import xml.etree.ElementTree as ET

from . import docs, fomod
from .analyze import SEVERITIES, analyze
from .extract import EXCLUDED_DIRS, PLUGIN_EXTS, InputError, _loose_members, _zip_members, build_mod
from .plugin import PluginError, parse_header
from .render import escape

LEVELS = ('blocker', 'warning', 'info')
CHECKS = ('masters', 'conflicts', 'version')
IMPLICIT_MASTERS = ('Skyrim.esm', 'Update.esm')
CHANGELOGS = ('CHANGELOG.md', 'CHANGELOG', 'CHANGES.md', 'docs/CHANGELOG.md')
README = re.compile(r'^readme(?:\.md|\.txt)?$', re.I)
CHANGELOG_HEADING = re.compile(r'^#{1,3}\s+(.*)$')
HEADING_VERSION = re.compile(r'^\[?v?(\d+(?:\.\d+)*(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?)\]?(?=$|[\s:(])', re.I)
# Only an explicit 'Version: 1.2.3' or a leading version; 'Requires SKSE v2.2' is not ours.
DESCRIPTION_VERSION = re.compile(r'(?:^\s*|\bversion\s*[:=]?\s*)v?(\d+(?:\.\d+)+(?:-[0-9A-Za-z.-]+)?)\b', re.I)
VERSION = re.compile(r'^v?(\d+(?:\.\d+)*)(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?$', re.I)

CONFIG_SCHEMA = {
    '': {'name': str, 'fail_on': str, 'masters': dict, 'conflicts': dict, 'version': dict, 'ignore': list},
    'masters': {'declared': list, 'implicit': list, 'requirements_file': str},
    'conflicts': {'against': list, 'fail_on': str},
    'version': {'changelog': str, 'tag_prefix': str, 'git': bool, 'plugin_description': bool},
    'ignore': {'check': str, 'kind': str, 'subject': str, 'reason': str},
}


class ConfigError(ValueError):
    """modcheck.toml is unreadable or invalid."""


def finding(check, level, kind, subject, detail, label='verified', source=''):
    return {'check': check, 'level': level, 'kind': kind, 'subject': subject,
            'detail': detail, 'label': label, 'source': source}


# Configuration -------------------------------------------------------------

def _validate(table, section, where):
    schema = CONFIG_SCHEMA[section]
    for key, value in table.items():
        if key not in schema:
            raise ConfigError('%s: unknown key %r' % (where, key))
        if not isinstance(value, schema[key]):
            raise ConfigError('%s: %r must be %s' % (where, key, schema[key].__name__))
        if schema[key] is list and section != '' and not all(isinstance(x, str) for x in value):
            raise ConfigError('%s: %r must be a list of strings' % (where, key))


def load_config(path):
    """Return a validated config dict; a missing path gives the defaults."""
    raw = {}
    if path is not None:
        try:
            import tomllib
        except ImportError:
            raise ConfigError('reading %s requires Python 3.11+ (tomllib)' % path)
        try:
            with open(path, 'rb') as handle:
                raw = tomllib.load(handle)
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise ConfigError('%s: %s' % (path, exc))
    _validate(raw, '', 'modcheck.toml')
    for section in ('masters', 'conflicts', 'version'):
        _validate(raw.get(section, {}), section, '[%s]' % section)
    rules = []
    for index, rule in enumerate(raw.get('ignore', [])):
        where = '[[ignore]] #%d' % (index + 1)
        if not isinstance(rule, dict):
            raise ConfigError('%s must be a table' % where)
        _validate(rule, 'ignore', where)
        if not rule.get('reason', '').strip():
            raise ConfigError('%s: a non-empty reason is required' % where)
        if not any(k in rule for k in ('check', 'kind', 'subject')):
            raise ConfigError('%s: set at least one of check, kind, subject' % where)
        if rule.get('check', '*') not in CHECKS + ('*',):
            raise ConfigError('%s: check must be one of %s' % (where, ', '.join(CHECKS)))
        rules.append(dict(rule, index=index + 1))
    masters, conflicts, version = (raw.get(s, {}) for s in ('masters', 'conflicts', 'version'))
    base = Path(path).parent if path is not None else Path('.')
    config = {
        'name': raw.get('name'),
        'fail_on': raw.get('fail_on', 'blocker'),
        'declared': list(masters.get('declared', [])),
        'implicit': list(masters.get('implicit', IMPLICIT_MASTERS)),
        'requirements_file': masters.get('requirements_file'),
        'against': [base / p for p in conflicts.get('against', [])],
        'conflicts_fail_on': conflicts.get('fail_on', 'collision'),
        'changelog': version.get('changelog'),
        'tag_prefix': version.get('tag_prefix', 'v'),
        'git': version.get('git', True),
        'plugin_description': version.get('plugin_description', True),
        'ignore': rules,
    }
    if config['fail_on'] not in ('blocker', 'warning', 'never'):
        raise ConfigError('fail_on must be blocker, warning or never')
    if config['conflicts_fail_on'] not in SEVERITIES[:3] + ('never',):
        raise ConfigError('[conflicts] fail_on must be collision, probable, warning or never')
    return config


# Inputs --------------------------------------------------------------------

def gather(target, release=None):
    """Return {'target': members, 'release': members} as (path, bytes) lists."""
    members = {'target': [(p, d) for p, d, _s in _loose_members(target)], 'release': []}
    if release is not None:
        zips = sorted(release.glob('*.zip'))
        source = (m for z in zips for m in _zip_members(z)) if zips else _loose_members(release)
        members['release'] = [(p, d) for p, d, _s in source]
    return members


def _plugin_members(members):
    """Shipped plugins: release members when given, else target outside build/test dirs."""
    found = [(p, d) for p, d in members['release'] if p.casefold().endswith(PLUGIN_EXTS)]
    if found:
        return found
    return [(p, d) for p, d in members['target'] if p.casefold().endswith(PLUGIN_EXTS)
            and not EXCLUDED_DIRS.intersection(PurePosixPath(p.casefold()).parts[:-1])]


def _all(members):
    return members['target'] + members['release']


def read_headers(members):
    """Parse TES4 headers of shipped plugins; return (headers, findings)."""
    headers, problems = [], []
    for path, data in sorted(_plugin_members(members)):
        try:
            headers.append((path, parse_header(data, PurePosixPath(path).name)))
        except PluginError as exc:
            problems.append(finding('masters', 'blocker', 'unreadable-plugin', path, str(exc)))
    return headers, problems


# Masters -------------------------------------------------------------------

def _fomod_declarations(members):
    """Plugin names named by FOMOD dependencies/info, and FOMOD-installed plugin names."""
    declared, installed, problems = {}, set(), []
    for path, data in _all(members):
        lower = path.casefold()
        if lower.endswith('fomod/moduleconfig.xml'):
            try:
                root = fomod._xml(data)
            except ET.ParseError as exc:
                problems.append(finding('masters', 'warning', 'unparsed-fomod', path, str(exc)))
                continue
            for node in root.iter():
                if node.tag == 'fileDependency' and node.get('file'):
                    name = PurePosixPath(node.get('file').replace('\\', '/')).name
                    declared.setdefault(name.casefold(), path)
                if node.tag in ('file', 'folder'):
                    for attr in ('source', 'destination'):
                        value = (node.get(attr) or '').replace('\\', '/')
                        if value.casefold().endswith(PLUGIN_EXTS):
                            installed.add(PurePosixPath(value).name.casefold())
        elif lower.endswith('fomod/info.xml'):
            try:
                root = fomod._xml(data)
            except ET.ParseError as exc:
                problems.append(finding('masters', 'warning', 'unparsed-fomod', path, str(exc)))
                continue
            text = ' '.join(t for t in root.itertext())
            for name in docs.plugin_names(text):
                declared.setdefault(name.casefold(), path)
    return declared, installed, problems


def _readme_declarations(members):
    declared = {}
    for path, data in _all(members):
        if '/' not in path and README.match(path):
            for name in docs.plugin_names(data.decode('utf-8-sig', 'replace')):
                declared.setdefault(name.casefold(), path)
    return declared


def _requirements_file(path):
    names = []
    for line in Path(path).read_text(encoding='utf-8-sig').splitlines():
        line = line.split('#', 1)[0].strip()
        if line:
            names.append(line)
    return names


def check_masters(target, members, headers, config):
    """Flag masters that no FOMOD, README, or configured requirement declares."""
    results = []
    fomod_names, fomod_installed, problems = _fomod_declarations(members)
    results.extend(problems)
    sources = {}
    for name in config['implicit']:
        sources.setdefault(name.casefold(), 'implicit masters')
    for name in config['declared']:
        sources.setdefault(name.casefold(), 'modcheck.toml')
    if config['requirements_file']:
        req = target / config['requirements_file']
        try:
            for name in _requirements_file(req):
                sources.setdefault(name.casefold(), config['requirements_file'])
        except OSError as exc:
            results.append(finding('masters', 'blocker', 'requirements-file', config['requirements_file'], str(exc)))
    for name, path in fomod_names.items():
        sources.setdefault(name, path)
    for name, path in _readme_declarations(members).items():
        sources.setdefault(name, path)
    own = {PurePosixPath(p).name.casefold() for p, _h in headers} | fomod_installed
    for path, header in headers:
        for warning in header.warnings:
            results.append(finding('masters', 'warning', 'header-warning', path, warning))
        for master in header.masters:
            key = master.casefold()
            if key in own:
                continue
            if key in sources:
                results.append(finding('masters', 'info', 'declared-master', master,
                                       '%s requires %s; declared in %s' % (path, master, sources[key])))
            else:
                results.append(finding('masters', 'blocker', 'undeclared-master', master,
                                       '%s requires %s, which is not declared in FOMOD, README, or modcheck.toml'
                                       % (path, master), source=path))
    return results


# Conflicts -----------------------------------------------------------------

def _mod_inputs(path):
    """A git checkout is read as a clone; anything else as an unpacked package or zip folder."""
    return {'clone': path} if (path / '.git').exists() else {'release': path}


def check_conflicts(name, target, release, against, config):
    """Run the engine over the target and its peers; keep findings that involve the target."""
    mod = build_mod(name, clone=target, release=release)
    mods = [mod]
    names = {name.casefold()}
    for path in against:
        if not path.is_dir():
            raise InputError('--against is not a directory: %s' % path)
        peer = path.resolve().name
        if peer.casefold() in names:
            raise InputError('duplicate mod name %r in --against' % peer)
        names.add(peer.casefold())
        mods.append(build_mod(peer, **_mod_inputs(path)))
    analysis = analyze(mods)
    threshold = config['conflicts_fail_on']
    limit = SEVERITIES.index(threshold) if threshold != 'never' else -1
    results = []
    for item in analysis['findings']:
        if name not in item['mods']:
            continue
        rank = SEVERITIES.index(item['severity'])
        level = 'blocker' if rank <= limit else 'warning' if rank <= SEVERITIES.index('warning') else 'info'
        others = [m for m in item['mods'] if m != name] or [name]
        labels = sorted({ev['label'] for ev in item['evidence']})
        results.append(finding('conflicts', level, item['kind'], ', '.join(others),
                               '%s (%s): %s' % (item['title'], item['severity'], item['detail']),
                               label='/'.join(labels), source=item['id']))
    for gap in mod['gaps']:
        if not gap.startswith('No release zip'):
            results.append(finding('conflicts', 'info', 'gap', name, gap, label='inferred'))
    order = analysis['load_order']['suggested_order'] if len(mods) > 1 else []
    return results, order


# Versions ------------------------------------------------------------------

def version_key(text):
    """Comparable key for dotted versions with optional SemVer pre-release; None if unparsable."""
    match = VERSION.match(text.strip())
    if not match:
        return None
    numbers = [int(x) for x in match.group(1).split('.')]
    while len(numbers) > 1 and numbers[-1] == 0:
        numbers.pop()
    pre = match.group(2)
    if pre is None:
        return tuple(numbers), (1,)
    return tuple(numbers), (0, tuple((0, int(p), '') if p.isdigit() else (1, 0, p) for p in pre.split('.')))


def changelog_version(text):
    """Return (top released version, unreleased-section-present) from a Markdown changelog."""
    unreleased = False
    for line in text.splitlines():
        heading = CHANGELOG_HEADING.match(line.strip())
        if not heading:
            continue
        title = heading.group(1).strip()
        if re.match(r'^\[?unreleased\]?', title, re.I):
            unreleased = True
            continue
        match = HEADING_VERSION.match(title)
        if match:
            return match.group(1), unreleased
    return None, unreleased


def _git(target, *args):
    try:
        done = subprocess.run(['git', '-C', str(target)] + list(args), capture_output=True, text=True,
                              timeout=15, env=dict(os.environ, GIT_TERMINAL_PROMPT='0'))
    except (OSError, subprocess.SubprocessError) as exc:
        return None, str(exc)
    if done.returncode:
        return None, done.stderr.strip() or 'git exited %d' % done.returncode
    return done.stdout.strip(), ''


def git_tags(target, prefix):
    """Return (tags at HEAD, latest reachable tag, problem) using read-only git commands."""
    pattern = (prefix or '') + '*'
    inside, error = _git(target, 'rev-parse', '--is-inside-work-tree')
    if inside != 'true':
        return [], None, error or 'not a git work tree'
    head, error = _git(target, 'tag', '--points-at', 'HEAD', '--list', pattern)
    if head is None:
        return [], None, error
    latest, _error = _git(target, 'describe', '--tags', '--abbrev=0', '--match', pattern)
    return sorted(head.split()), latest or None, ''


def _strip(tag, prefix):
    return tag[len(prefix):] if prefix and tag.startswith(prefix) else tag


def check_version(target, members, headers, config, tag=None, use_git=True):
    """Compare metadata versions with each other and with the release tag."""
    results, versions = [], []
    for path, data in _all(members):
        if path.casefold().endswith('fomod/info.xml'):
            try:
                value = fomod.info(data)['version']
            except ET.ParseError as exc:
                results.append(finding('version', 'warning', 'unparsed-fomod', path, str(exc)))
                continue
            if value and value.strip():
                versions.append(('FOMOD info.xml', path, value.strip()))
    if config['plugin_description']:
        for path, header in headers:
            match = DESCRIPTION_VERSION.search(header.description or '')
            if match:
                versions.append(('plugin description', path, match.group(1)))
    names = [config['changelog']] if config['changelog'] else list(CHANGELOGS)
    changelog = next((target / n for n in names if (target / n).is_file()), None)
    if changelog is None:
        level = 'blocker' if config['changelog'] else 'warning'
        results.append(finding('version', level, 'missing-changelog', names[0], 'No changelog found (looked for %s)' % ', '.join(names)))
    else:
        rel = changelog.relative_to(target).as_posix()
        top, unreleased = changelog_version(changelog.read_text(encoding='utf-8-sig', errors='replace'))
        if unreleased:
            results.append(finding('version', 'warning', 'changelog-unreleased', rel,
                                   'An Unreleased section sits above the top released entry', label='inferred'))
        if top:
            versions.append(('CHANGELOG top entry', rel, top))
        else:
            results.append(finding('version', 'warning', 'changelog-no-version', rel, 'No versioned heading found', label='inferred'))
    for kind, path, value in versions:
        if version_key(value) is None:
            results.append(finding('version', 'warning', 'version-unparsed', path, '%s %r is not a dotted version' % (kind, value)))
    table = [{'source': kind, 'path': path, 'version': value} for kind, path, value in versions]
    if not versions:
        results.append(finding('version', 'warning', 'no-version', '-', 'No version in FOMOD info.xml, plugin description, or changelog'))
        reference = None
    else:
        reference = versions[0]
        keys = {version_key(v) or v for _k, _p, v in versions}
        if len(keys) > 1:
            results.append(finding('version', 'blocker', 'version-mismatch', ', '.join(sorted({p for _k, p, _v in versions})),
                                   'Versions disagree: ' + '; '.join('%s %s = %s' % (k, p, v) for k, p, v in versions)))
    prefix = config['tag_prefix']
    head_tags, latest = ([tag], None) if tag else ([], None)
    if not tag and use_git and config['git']:
        head_tags, latest, problem = git_tags(target, prefix)
        if problem:
            results.append(finding('version', 'info', 'git-unavailable', '-', 'Tag comparison skipped: ' + problem.splitlines()[0], label='inferred'))
    for name in head_tags:
        table.append({'source': 'git tag at HEAD' if not tag else 'release tag (--tag)', 'path': name, 'version': _strip(name, prefix)})
    if latest and latest not in head_tags:
        table.append({'source': 'latest earlier git tag', 'path': latest, 'version': _strip(latest, prefix)})
    if reference:
        kind, path, value = reference
        key = version_key(value)
        if head_tags:
            matched = [t for t in head_tags if (version_key(_strip(t, prefix)) or _strip(t, prefix)) == (key or value)]
            if not matched:
                results.append(finding('version', 'blocker', 'tag-mismatch', ', '.join(head_tags),
                                       'Release tag %s does not match %s %s' % (', '.join(head_tags), kind, value)))
        elif latest:
            latest_key = version_key(_strip(latest, prefix))
            if key is not None and latest_key is not None:
                if key < latest_key:
                    results.append(finding('version', 'blocker', 'version-behind-tag', latest,
                                           '%s %s is older than existing tag %s' % (kind, value, latest)))
                elif key == latest_key:
                    results.append(finding('version', 'warning', 'version-not-bumped', latest,
                                           'HEAD is past tag %s but %s is still %s' % (latest, kind, value)))
    return results, table


# Ignore rules and report ---------------------------------------------------

def _matches(rule, item):
    for field in ('check', 'kind', 'subject'):
        if field in rule and not fnmatchcase(item[field].casefold(), rule[field].casefold()):
            return False
    return True


def apply_ignores(results, rules):
    kept, ignored, used = [], [], set()
    for item in results:
        rule = next((r for r in rules if _matches(r, item)), None)
        if rule is None:
            kept.append(item)
        else:
            used.add(rule['index'])
            ignored.append(dict(item, reason=rule['reason']))
    for rule in rules:
        if rule['index'] not in used:
            pattern = ', '.join('%s=%s' % (k, rule[k]) for k in ('check', 'kind', 'subject') if k in rule)
            kept.append(finding('config', 'info', 'unused-ignore', '#%d' % rule['index'], 'Ignore rule matched nothing: ' + pattern))
    return kept, ignored


def _sort(results):
    return sorted(results, key=lambda f: (LEVELS.index(f['level']), f['check'], f['kind'], f['subject'].casefold(), f['detail']))


def _table(items, extra=None):
    head = '| Check | Kind | Subject | Detail |' + (' %s |' % extra if extra else '')
    lines = [head, '| --- | --- | --- | --- |' + (' --- |' if extra else '')]
    for f in items:
        row = '| %s | `%s` | %s | %s |' % (f['check'], escape(f['kind']), escape(f['subject']), escape(f['detail']))
        lines.append(row + (' %s |' % escape(f[extra.lower()]) if extra else ''))
    return lines


def report(result):
    counts = {level: sum(f['level'] == level for f in result['findings']) for level in LEVELS}
    status = 'FAIL' if result['failed'] else 'PASS'
    lines = ['# modcheck: %s' % escape(result['name']), '',
             '**%s**: %d blocker(s), %d warning(s), %d info, %d ignored.' % (
                 status, counts['blocker'], counts['warning'], counts['info'], len(result['ignored'])), '',
             '| Check | Blockers | Warnings | Info |', '| --- | ---: | ---: | ---: |']
    for check in CHECKS + ('config',):
        row = [sum(f['check'] == check and f['level'] == level for f in result['findings']) for level in LEVELS]
        if check != 'config' or any(row):
            lines.append('| %s | %d | %d | %d |' % ((check,) + tuple(row)))
    for level, title in (('blocker', 'Blockers'), ('warning', 'Warnings')):
        items = [f for f in result['findings'] if f['level'] == level]
        if items:
            lines += ['', '## ' + title, ''] + _table(items)
    if result['versions']:
        lines += ['', '## Versions', '', '| Source | Path | Version |', '| --- | --- | --- |']
        lines += ['| %s | %s | %s |' % (escape(v['source']), escape(v['path']), escape(v['version'])) for v in result['versions']]
    if result['load_order']:
        lines += ['', '## Suggested load order', '', ' → '.join('`%s`' % escape(p) for p in result['load_order'])]
    info = [f for f in result['findings'] if f['level'] == 'info']
    if info:
        lines += ['', '<details><summary>Info (%d)</summary>' % len(info), ''] + _table(info) + ['', '</details>']
    if result['ignored']:
        lines += ['', '<details><summary>Ignored by modcheck.toml (%d)</summary>' % len(result['ignored']), '']
        lines += _table(result['ignored'], 'Reason') + ['', '</details>']
    lines += ['', 'Evidence from plugin headers, FOMOD XML and git is verified; README, changelog and '
              'Papyrus-source findings are inferred. See the mod-conflict-graph README for conflict kinds.']
    return '\n'.join(lines) + '\n'


def run(target, config, release=None, against=(), tag=None, use_git=True, fail_on=None):
    target = Path(target)
    name = config['name'] or target.resolve().name
    members = gather(target, release)
    headers, results = read_headers(members)
    results += check_masters(target, members, headers, config)
    conflicts, order = check_conflicts(name, target, release, list(config['against']) + list(against), config)
    results += conflicts
    version_results, versions = check_version(target, members, headers, config, tag, use_git)
    results += version_results
    kept, ignored = apply_ignores(results, config['ignore'])
    kept = _sort(kept)
    fail_on = fail_on or config['fail_on']
    failing = () if fail_on == 'never' else LEVELS[:LEVELS.index(fail_on) + 1]
    return {'name': name, 'findings': kept, 'ignored': _sort(ignored), 'versions': versions,
            'load_order': order, 'failed': any(f['level'] in failing for f in kept)}


# Command line ----------------------------------------------------------------

def parser():
    p = argparse.ArgumentParser(prog='mod-conflict-graph modcheck',
                                description='Pre-release checks for one mod: declared masters, conflicts, version drift.')
    p.add_argument('path', nargs='?', type=Path, default=Path('.'), help='Mod directory (default: .)')
    p.add_argument('--config', type=Path, help='Config file (default: PATH/modcheck.toml if present)')
    p.add_argument('--against', type=Path, action='append', default=[], metavar='DIR',
                   help='Another local mod directory to check conflicts with (repeatable)')
    p.add_argument('--release', type=Path, help='Built package for PATH: a folder of zips or an unpacked package')
    p.add_argument('--tag', help='Release tag to compare (default: git tags at HEAD)')
    p.add_argument('--no-git', action='store_true', help='Do not query git')
    p.add_argument('--fail-on', choices=('blocker', 'warning', 'never'), help='Exit 1 at this level (default: blocker)')
    p.add_argument('--out', type=Path, help='Write the Markdown report here instead of stdout')
    p.add_argument('--summary', type=Path, help='Append the report to this file (e.g. $GITHUB_STEP_SUMMARY)')
    p.add_argument('--quiet', action='store_true')
    return p


def main(argv=None):
    p = parser()
    args = p.parse_args(argv)
    if not args.path.is_dir():
        p.error('mod directory does not exist: %s' % args.path)
    if args.release is not None and not args.release.is_dir():
        p.error('release directory does not exist: %s' % args.release)
    config_path = args.config
    if config_path is None and (args.path / 'modcheck.toml').is_file():
        config_path = args.path / 'modcheck.toml'
    elif config_path is not None and not config_path.is_file():
        p.error('config file does not exist: %s' % config_path)
    try:
        config = load_config(config_path)
        result = run(args.path, config, args.release, args.against, args.tag, not args.no_git, args.fail_on)
        text = report(result)
        if args.out:
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text(text, encoding='utf-8')
        elif not args.quiet:
            sys.stdout.write(text)
        if args.summary:
            with open(args.summary, 'a', encoding='utf-8') as handle:
                handle.write(text)
    except (ConfigError, InputError, OSError, ValueError, LookupError, RecursionError) as exc:
        print('modcheck: %s' % exc, file=sys.stderr)
        return 2
    if args.out and not args.quiet:
        blockers = sum(f['level'] == 'blocker' for f in result['findings'])
        print('modcheck: %s; %d blocker(s); wrote %s' % ('FAIL' if result['failed'] else 'PASS', blockers, args.out))
    return 1 if result['failed'] else 0
