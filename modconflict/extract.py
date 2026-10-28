"""Read clones and packages without extracting archives or retaining asset bytes."""
from collections import Counter
import json
import hashlib
from pathlib import Path, PurePosixPath
import re
import zipfile
import zlib
import xml.etree.ElementTree as ET

from . import docs, fomod, papyrus
from .plugin import parse_plugin, PluginError, FLAG_LOCALIZED, IGNORED_FORCED_REFS

MAX_BYTES = 200 * 1024 * 1024
MAX_ENTRIES = 20000
PLUGIN_EXTS = ('.esp', '.esm', '.esl')
EXCLUDED_DIRS = frozenset(('tools', 'masters', 'vendor', 'vendored', 'third_party',
                           'node_modules', 'papyrus-stubs', 'stubs', 'tests', 'test',
                           'fixtures', 'build', 'dist', '.git', '.github', 'docs'))


def own_script(path, clone=False):
    """Recognise source roots and packaged script roots, excluding build inputs."""
    parts = PurePosixPath(path.casefold()).parts
    if EXCLUDED_DIRS.intersection(parts[:-1]):
        return False
    if clone:
        return path.casefold().endswith('.psc') and (
            parts[:2] == ('src', 'papyrus') or (len(parts) == 2 and parts[0] == 'scripts'))
    if path.casefold().endswith('.psc'):
        return any(parts[i:i + 2] == ('source', 'scripts') for i in range(len(parts) - 2))
    return path.casefold().endswith('.pex') and 'scripts' in parts[:-1]


class InputError(ValueError):
    """An unsafe or unreadable input."""


def safe_path(name):
    name = name.replace('\\', '/')
    if name.startswith('/') or re.match(r'^[A-Za-z]:', name) or '..' in name.split('/'):
        raise InputError('unsafe package path: %s' % name)
    return name


def kind(path):
    lower = path.casefold()
    if lower.endswith(PLUGIN_EXTS):
        return 'plugin'
    for ext, value in [('.pex', 'script-pex'), ('.psc', 'script-psc'), ('.seq', 'seq')]:
        if lower.endswith(ext):
            return value
    if lower.startswith('sound/voice/'):
        return 'voice'
    return 'fomod' if lower.startswith('fomod/') else 'other'


def _zip_members(path):
    """Validate the entire directory before reading even the first member."""
    try:
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
            if len(entries) > MAX_ENTRIES or sum(x.file_size for x in entries) > MAX_BYTES:
                raise InputError('archive exceeds entry or uncompressed size cap: %s' % path.name)
            for entry in entries:
                safe_path(entry.filename)
            for entry in entries:
                if not entry.is_dir():
                    yield safe_path(entry.filename), archive.read(entry), 'release-zip'
    except (zipfile.BadZipFile, RuntimeError, OSError, NotImplementedError, EOFError, zlib.error) as exc:
        raise InputError('%s: %s' % (path.name, exc))


def _loose_members(root):
    for path in sorted(root.rglob('*')):
        # Do not follow symlinks outside the selected tree or inspect VCS internals.
        relative = path.relative_to(root)
        if any(part.startswith('.') for part in relative.parts) or path.is_symlink():
            continue
        if path.is_file() and root.resolve() in path.resolve().parents:
            yield relative.as_posix(), path.read_bytes(), 'clone'


def _record(plugin, record):
    owner, local = plugin.owner_key(record.form_id)
    ref = lambda fid: {'plugin': plugin.owner_of(fid), 'local_id': plugin.local_id(fid)} if fid is not None else None
    return {'type': record.type, 'form_id': '%08X' % record.form_id,
            'owner': owner, 'local_id': local, 'editor_id': record.editor_id,
            'is_override': record.is_override, 'scripts': sorted(record.scripts),
            'script_props': [{'script': s, 'property': p, 'target_plugin': plugin.owner_of(f),
                              'local_id': plugin.local_id(f)} for s, p, f in record.script_props if f],
            'parent_cell': ref(record.parent_cell), 'parent_world': ref(record.parent_world),
            'base': ref(record.base) if record.base else None, 'position': record.position,
            'cell_flags': record.cell_flags, 'interior': record.interior,
            'forced_refs': [ref(f) for f in record.forced_refs if f]}


def plugin_facts(data, name, source):
    p = parse_plugin(data, name)
    records = [_record(p, r) for r in p.records]
    new = [r for r in records if not r['is_override']]
    ids = [r['local_id'] for r in new]
    overrides = [{'owner': r['owner'], 'local_id': r['local_id'], 'type': r['type'],
                  'editor_id': r['editor_id'], 'in': name} for r in records if r['is_override']]
    links = [link for r in records for link in r['script_props']]
    index = {(r['owner'].casefold(), r['local_id']): r for r in records}
    for link in links:
        target = index.get((link['target_plugin'].casefold(), link['local_id']))
        if target:
            link.update({'target_type': target['type'], 'target_editor_id': target['editor_id']})
    return {'file_name': name, 'esl': p.is_esl or name.casefold().endswith('.esl'), 'esm': p.is_esm,
            'localized': bool(p.flags & FLAG_LOCALIZED), 'masters': p.masters,
            'hedr_version': p.version, 'record_count': len(records),
            'record_type_counts': dict(sorted(Counter(r['type'] for r in records).items())),
            'records': records, 'overrides': overrides, 'new_records': len(new),
            'id_range': {'min': min(ids), 'max': max(ids)} if ids else None,
            'cell_placements': [r for r in records if r['position'] is not None],
            'vmad_scripts': sorted({s for r in records for s in r['scripts']}, key=str.casefold),
            'script_property_links': links, 'warnings': p.warnings, 'source': source, 'label': 'verified'}


def build_mod(name, clone=None, release=None, extra=False):
    mod = {'name': name, 'title': name, 'version': None, 'extra': extra, 'plugins': [],
           'files': [], 'scripts': {'pex': [], 'psc': [], 'vmad': []}, 'script_names': [],
           'source_facts': [], 'fomod': None, 'sequences': [], 'voice_folders': [],
           'pinned_ids': {}, 'docs': docs.extract(''), 'facts': [], 'gaps': [],
           'provenance': {}}
    clone = Path(clone) if clone else None
    release = Path(release) if release else None
    zips = sorted(release.glob('*.zip')) if release else []
    # Multiple release archives may be platform/optional packages; inspect every archive.
    # Prefer package plugins over clone plugins with the same filename.
    plugin_map, files, psc, docs_text = {}, {}, {}, {}
    package_plugin_names = set()
    metadata, configs = [], []
    packages = []
    current_package = {'files': [], 'configs': []}
    seen_readme = False
    origins = set()
    clone_plugin_names = set()

    def consume(path, data, source, shipped, from_clone=False):
        nonlocal seen_readme
        path = safe_path(path)
        lower = path.casefold()
        base = PurePosixPath(path).name
        origins.add(source)
        excluded = bool(EXCLUDED_DIRS.intersection(PurePosixPath(lower).parts[:-1]))
        if source == 'clone' and lower.endswith(PLUGIN_EXTS) and excluded:
            return
        if from_clone and ((lower.endswith(PLUGIN_EXTS) and zips) or
                           (lower.endswith(('.psc', '.pex')) and not own_script(path, clone=True))):
            return
        if shipped:
            entry = {'path': path, 'size': len(data), 'crc32': '%08x' % (zlib.crc32(data) & 0xffffffff),
                     'kind': kind(path), 'source': source, 'label': 'verified'}
            # Keep archive conflicts visible rather than silently replacing facts.
            key = (lower, entry['crc32'], entry['size'])
            files[key] = entry
            current_package['files'].append(entry)
        if lower.endswith(PLUGIN_EXTS):
            key = base.casefold()
            if source != 'release-zip' and key in package_plugin_names:
                return
            try:
                candidate = plugin_facts(data, base, source)
                if from_clone:
                    clone_plugin_names.add(key)
                if source == 'release-zip' and key not in package_plugin_names:
                    plugin_map[key] = {}
                    package_plugin_names.add(key)
                plugin_map.setdefault(key, {})[hashlib.sha256(data).hexdigest()] = candidate
            except PluginError as exc:
                mod['gaps'].append('Unreadable plugin %s: %s' % (base, exc))
        elif lower.endswith('.psc') and own_script(path, clone=from_clone):
            fact = papyrus.scan(data.decode('utf-8-sig', 'replace'), path)
            fact.update({'source': source, 'path': path})
            psc[(source, lower)] = fact
        elif lower.endswith('fomod/info.xml'):
            metadata.append((source, data))
        elif lower.endswith('fomod/moduleconfig.xml'):
            configs.append((source, data))
            current_package['configs'].append((source, data))
        if lower == 'readme.md' or (lower.startswith('docs/') and lower.endswith('.md')):
            if lower not in docs_text or source == 'clone':
                docs_text[lower] = (source, data.decode('utf-8-sig', 'replace'))
            seen_readme |= lower == 'readme.md'
        if source == 'clone' and lower == 'src/plugin/formids.json':
            try:
                raw = json.loads(data)
                mod['pinned_ids'] = {k: int(v, 16) if isinstance(v, str) else int(v) for k, v in raw.items()}
            except (ValueError, TypeError, AttributeError, OverflowError, LookupError, RecursionError):
                mod['gaps'].append('Invalid pinned formids.json')

    if clone:
        for path, data, source in _loose_members(clone):
            consume(path, data, source, not bool(release), from_clone=True)
    if current_package['files']:
        packages.append(current_package)
    current_package = {'files': [], 'configs': []}
    if release:
        if zips:
            mod['version'] = zips[-1].stem
            for archive in zips:
                current_package = {'files': [], 'configs': []}
                packages.append(current_package)
                for path, data, source in _zip_members(archive):
                    consume(path, data, source, True)
        else:
            packages.append(current_package)
            for path, data, source in _loose_members(release):
                consume(path, data, source, True)
    mod['plugins'] = [variants[digest] for name, variants in sorted(plugin_map.items()) for digest in sorted(variants)]
    mod['ignored_forced_refs'] = sum(
        (ref['plugin'].casefold(), ref['local_id']) in IGNORED_FORCED_REFS
        for p in mod['plugins'] for r in p['records'] if r['type'] == 'QUST'
        for ref in r['forced_refs'])
    for name, variants in sorted(plugin_map.items()):
        if len(variants) > 1:
            mod['facts'].append({'kind': 'package-plugin-variants', 'severity': 'info',
                                 'items': [name + ': %d distinct versions analysed conservatively' % len(variants)],
                                 'label': 'verified', 'source': 'package files'})
    mod['files'] = sorted(files.values(), key=lambda f: (f['path'].casefold(), f['crc32']))
    mod['source_facts'] = sorted(psc.values(), key=lambda s: (s['name'].casefold(), s['source'], s['path']))
    for source, data in sorted(metadata, key=lambda x: x[0] == 'release-zip'):
        try:
            values = fomod.info(data)
            mod['title'] = values['title'] or mod['title']
            mod['version'] = values['version'] or mod['version']
        except (ET.ParseError, LookupError, ValueError, OverflowError, RecursionError):
            mod['gaps'].append('Unparsed FOMOD info.xml')
    mod['installed_file_entries'] = []
    parsed_configs = []
    for package in packages:
        entries = package['files']
        package_config = None
        for source, data in package['configs']:
            try:
                parsed = fomod.parse(data, [f['path'] for f in entries])
                parsed.update({'source': source, 'label': 'verified'})
                parsed_configs.append(parsed)
                package_config = parsed
                for key, title in [('missing_sources', 'referenced-but-missing'), ('never_installed', 'shipped-but-never-installed')]:
                    if parsed[key]:
                        mod['facts'].append({'kind': title, 'severity': 'info', 'items': parsed[key], 'label': 'verified', 'source': source})
            except (ET.ParseError, LookupError, ValueError, OverflowError, RecursionError):
                mod['gaps'].append('Unparsed FOMOD ModuleConfig.xml')
        mappings = package_config['install_map'] if package_config else [
            {'source': f['path'].casefold(), 'destination': f['path'].casefold()} for f in entries]
        for mapping in mappings:
            for entry in entries:
                if entry['path'].casefold() == mapping['source']:
                    mod['installed_file_entries'].append({'mapping': mapping, 'file': entry})
    if parsed_configs:
        combined = dict(parsed_configs[-1])
        for field in ('steps', 'required_install', 'missing_sources', 'never_installed', 'install_map'):
            combined[field] = [item for config in parsed_configs for item in config[field]]
        combined['conditional_installs'] = sum(c['conditional_installs'] for c in parsed_configs)
        combined['installed_files'] = {mode: sorted({path for config in parsed_configs for path in config['installed_files'][mode]})
                                       for mode in ('required only', 'all options')}
        mod['fomod'] = combined
    mod['docs'] = docs.extract('\n'.join(docs_text[path][1] for path in sorted(docs_text)))
    mod['scripts']['pex'] = sorted({papyrus.script_name(f['path']) for f in mod['files'] if f['kind'] == 'script-pex' and own_script(f['path'])}, key=str.casefold)
    mod['scripts']['psc'] = sorted({s['name'] for s in mod['source_facts']} | {papyrus.script_name(s['path']) for s in mod['source_facts']}, key=str.casefold)
    mod['scripts']['vmad'] = sorted({s for p in mod['plugins'] for s in p['vmad_scripts']}, key=str.casefold)
    mod['script_names'] = sorted({s for values in mod['scripts'].values() for s in values}, key=str.casefold)
    mod['prefix'] = papyrus.own_prefix([r['editor_id'] for p in mod['plugins'] for r in p['records']], mod['script_names'])
    for fact in mod['source_facts']:
        fact['writes'] = [w for w in fact['writes'] if not mod['prefix'] or not w['property'].casefold().startswith(mod['prefix'])]
    mod['sequences'] = sorted({f['path'] for f in mod['files'] if f['kind'] == 'seq'})
    mod['voice_folders'] = sorted({f['path'].replace('\\', '/').split('/')[2] for f in mod['files'] if f['path'].casefold().startswith('sound/voice/') and len(f['path'].split('/')) > 3}, key=str.casefold)
    for plugin in mod['plugins']:
        mod['gaps'].extend(plugin['warnings'])
        invalid = [r['local_id'] for r in plugin['records'] if not r['is_override'] and not 0x800 <= r['local_id'] <= 0xfff]
        if plugin['esl'] and invalid:
            mod['facts'].append({'kind': 'esl-range', 'severity': 'collision', 'items': ['%06X' % i for i in invalid], 'label': 'verified', 'source': plugin['file_name']})
    by_edid = {r['editor_id'].casefold(): r['local_id'] for p in mod['plugins'] for r in p['records'] if r['editor_id']}
    for edid, pinned in mod['pinned_ids'].items():
        if edid.casefold() not in by_edid or by_edid[edid.casefold()] != (pinned & 0xffffff):
            mod['facts'].append({'kind': 'pinned-id-mismatch', 'severity': 'warning', 'items': [edid], 'label': 'inferred', 'source': 'formids.json', 'verified': bool(mod['plugins'])})
    if not zips:
        mod['gaps'].append('No release zip; package completeness cannot be verified (loose plugin bytes, if present, are verified)')
        if clone_plugin_names:
            mod['gaps'].append('plugin data from clone (no release zip)')
    if not seen_readme:
        mod['gaps'].append('Missing README.md')
    if not mod['plugins']:
        mod['gaps'].append('No plugin bytes available')
    if mod['fomod'] and mod['fomod']['conditional_installs']:
        mod['gaps'].append('Conditional FOMOD dependencies are not evaluated; all-options destinations are conservative')
    if any(f['path'].casefold().endswith('.bsa') for f in mod['files']):
        mod['gaps'].append('BSA contents are not inspected')
    if zips and len(zips) > 1:
        mod['gaps'].append('Multiple release archives combined conservatively; alternate packages may be mutually exclusive')
    provenance = 'both' if len(origins) > 1 else next(iter(origins), 'clone')
    for group in ('scripts', 'files', 'source_facts', 'docs', 'pinned_ids', 'sequences', 'voice_folders'):
        label = 'inferred' if group in ('source_facts', 'docs', 'pinned_ids') else 'verified'
        if group == 'scripts' and not (mod['scripts']['pex'] or mod['scripts']['vmad']):
            label = 'inferred'
        mod['provenance'][group] = {'source': provenance if group not in ('docs', 'pinned_ids') else 'clone', 'label': label}
    doc_sources = {source for source, _text in docs_text.values()}
    if doc_sources:
        mod['provenance']['docs']['source'] = 'both' if len(doc_sources) > 1 else next(iter(doc_sources))
    return mod
