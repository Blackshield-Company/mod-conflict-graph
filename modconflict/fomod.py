"""Read FOMOD metadata and enumerate conservative install destination sets."""
import posixpath
import xml.etree.ElementTree as ET


def normalize(path):
    normalized = posixpath.normpath(path.replace('\\', '/')).casefold()
    return '' if normalized == '.' else normalized


def _xml(data):
    root = ET.fromstring(data)
    for node in root.iter():
        node.tag = node.tag.split('}')[-1]
    return root


def info(data):
    root = _xml(data)
    return {'title': root.findtext('Name'), 'version': root.findtext('Version')}


def _files(node):
    return [{'source': normalize(x.get('source', '')),
             'destination': normalize(x.get('destination', x.get('source', ''))),
             'folder': x.tag == 'folder'}
            for x in node.iter() if x.tag in ('file', 'folder') and x.get('source')]


def parse(data, shipped):
    root = _xml(data)
    required = _files(root.find('requiredInstallFiles')) if root.find('requiredInstallFiles') is not None else []
    steps, options = [], []
    for step in root.findall('.//installStep'):
        groups = []
        for group in step.findall('./optionalFileGroups/group'):
            opts = []
            for option in group.findall('./plugins/plugin'):
                descriptor = option.find('./typeDescriptor/type')
                if descriptor is None:
                    descriptor = option.find('./typeDescriptor/dependencyType/defaultType')
                item = {'name': option.get('name', ''),
                        'type': descriptor.get('name', '') if descriptor is not None else '',
                        'description': (option.findtext('description') or '')[:200], 'files': _files(option)}
                opts.append(item)
                options.append(item)
            groups.append({'name': group.get('name', ''), 'type': group.get('type', ''), 'options': opts})
        steps.append({'name': step.get('name', ''), 'groups': groups})
    conditional = root.findall('./conditionalFileInstalls/patterns/pattern')
    all_files = required + [f for o in options for f in o['files']] + [f for n in conditional for f in _files(n)]
    required += [f for o in options if o['type'].casefold() == 'required' for f in o['files']]
    paths = sorted(set(normalize(p) for p in shipped))
    missing = sorted(set(f['source'] for f in all_files if not _expand(f, paths)))
    installed_sources = {src for f in all_files for src, _dst in _expand(f, paths)}
    result = {'module_name': root.findtext('moduleName'), 'steps': steps,
              'required_install': required, 'conditional_installs': len(conditional),
              'installed_files': {'required only': installed_files(required, paths),
                                  'all options': installed_files(all_files, paths)},
              'missing_sources': missing,
              'never_installed': [p for p in paths if p not in installed_sources and not p.startswith('fomod/')],
              'install_map': [{'source': src, 'destination': dst} for f in all_files for src, dst in _expand(f, paths)]}
    return result


def _expand(item, paths):
    src, dst = item['source'], item['destination']
    if item['folder']:
        prefix = src.rstrip('/') + '/' if src else ''
        return [(p, normalize(posixpath.join(dst, p[len(prefix):]))) for p in paths if p.startswith(prefix)]
    return [(src, dst)] if src in paths else []


def installed_files(items, paths):
    return sorted({dst for f in items for _src, dst in _expand(f, paths)})
