"""Evidence-based checks and deterministic dependency ordering."""
from collections import defaultdict
from itertools import combinations
import math
import re
from .plugin import IGNORED_FORCED_REFS

SEVERITIES = ('collision', 'probable', 'warning', 'dependency', 'info', 'none')
CAST_TYPES = {'globalvariable': 'GLOB', 'quest': 'QUST', 'formlist': 'FLST',
              'keyword': 'KYWD', 'spell': 'SPEL', 'weapon': 'WEAP', 'armor': 'ARMO',
              'objectreference': 'REFR', 'actor': 'ACHR', 'actorbase': 'NPC_',
              'faction': 'FACT', 'message': 'MESG', 'magiceffect': 'MGEF',
              'activator': 'ACTI', 'cell': 'CELL', 'worldspace': 'WRLD',
              'leveleditem': 'LVLI', 'race': 'RACE', 'perk': 'PERK', 'book': 'BOOK',
              'sound': 'SOUN', 'location': 'LCTN', 'outfit': 'OTFT', 'miscobject': 'MISC'}



VANILLA = {'skyrim.esm', 'update.esm', 'dawnguard.esm', 'hearthfires.esm', 'dragonborn.esm'}


def is_vanilla(name):
    name = name.casefold()
    return name in VANILLA or bool(re.match(r'^cc[a-z]{3}sse.*\.es[ml]$', name))


def key(ref):
    return (ref['plugin'].casefold(), ref['local_id'])


def record_key(record):
    return record['owner'].casefold(), record['local_id']


def evidence(label, source, detail):
    return {'label': label, 'source': source, 'detail': detail}


def detail_text(items):
    """Cap both item count and character count, counting omitted items."""
    items = sorted(set(str(x) for x in items))
    for count in range(min(25, len(items)), -1, -1):
        suffix = '; ... +%d more' % (len(items) - count) if count < len(items) else ''
        text = '; '.join(items[:count]) + suffix
        if len(text) <= 300:
            if count == 0 and items:
                suffix = '... +%d more' % len(items)
                return items[0][:300 - len(suffix)] + suffix
            return text


class Findings:
    def __init__(self):
        self.items = {}

    def add(self, kind, severity, mods, title, items=(), label='verified', source='plugin bytes', hint='', extra=(), discriminator=''):
        mods = sorted(set(mods), key=str.casefold)
        identity = (kind, tuple(mods), discriminator)
        items = list(items)
        ev = evidence(label, source, '; '.join(str(x) for x in items) or title)
        if identity not in self.items:
            self.items[identity] = {'severity': severity, 'kind': kind, 'mods': mods,
                                    'title': title, 'detail': '', 'evidence': [], 'hint': hint,
                                    '_items': set()}
        finding = self.items[identity]
        if SEVERITIES.index(severity) < SEVERITIES.index(finding['severity']):
            finding['severity'] = severity
        finding['_items'].update(str(x) for x in items)
        for item in [ev] + list(extra):
            if item not in finding['evidence']:
                finding['evidence'].append(item)

    def finish(self):
        result = []
        for identity, item in sorted(self.items.items()):
            item['detail'] = detail_text(item.pop('_items'))
            # Merge repeated evidence from the same source and label, cap item lists.
            grouped = defaultdict(list)
            for ev in item['evidence']:
                grouped[(ev['label'], ev['source'])].extend(ev['detail'].split('; '))
            item['evidence'] = [evidence(label, source, detail_text(values))
                                for (label, source), values in sorted(grouped.items())]
            item['id'] = 'F%04d' % (len(result) + 1)
            result.append(item)
        return result


def _index(mod, field, new_only=False):
    result = defaultdict(list)
    for plugin in mod['plugins']:
        for r in plugin['records']:
            if new_only and r['is_override']:
                continue
            value = record_key(r) if field == 'owner' else r.get(field)
            if value:
                result[value.casefold() if isinstance(value, str) else value].append(r)
    return result


def _installed(mod):
    result = defaultdict(list)
    if 'installed_file_entries' in mod:
        for entry in mod['installed_file_entries']:
            result[entry['mapping']['destination']].append(entry['file'])
        return result
    source_files = defaultdict(list)
    for f in mod['files']:
        source_files[f['path'].casefold()].append(f)
    if mod['fomod']:
        for mapping in mod['fomod']['install_map']:
            result[mapping['destination']].extend(source_files[mapping['source']])
    else:
        for path, files in source_files.items():
            result[path].extend(files)
    return result


def _topology(nodes, edges):
    """Return a stable order and strongly connected components for actual cycles."""
    graph = {n: set() for n in nodes}
    for edge in edges:
        graph[edge['from']].add(edge['to'])
    indices, low, stack, active, components = {}, {}, [], set(), []

    for root in sorted(nodes):
        if root in indices:
            continue
        frames = [(root, iter(sorted(graph[root])))]
        indices[root] = low[root] = len(indices)
        stack.append(root)
        active.add(root)
        while frames:
            node, children = frames[-1]
            other = next(children, None)
            if other is not None:
                if other not in indices:
                    indices[other] = low[other] = len(indices)
                    stack.append(other)
                    active.add(other)
                    frames.append((other, iter(sorted(graph[other]))))
                elif other in active:
                    low[node] = min(low[node], indices[other])
                continue
            frames.pop()
            if frames:
                parent = frames[-1][0]
                low[parent] = min(low[parent], low[node])
            if low[node] == indices[node]:
                component = []
                while True:
                    other = stack.pop()
                    active.remove(other)
                    component.append(other)
                    if other == node:
                        break
                if len(component) > 1 or node in graph[node]:
                    components.append(sorted(component))

    remaining = set(nodes)
    order = ['Skyrim.esm']
    while remaining:
        ready = sorted(n for n in remaining if not (graph[n] & remaining))
        if not ready:
            break
        order.extend(n for n in ready if n != 'Skyrim.esm')
        remaining.difference_update(ready)
    return order, sorted(components)


def analyze(mods):
    mods = sorted(mods, key=lambda m: m['name'].casefold())
    findings = Findings()
    plugins = defaultdict(list)
    installed_names = {}
    for mod in mods:
        installed = _installed(mod)
        for p in mod['plugins']:
            original = p['file_name']
            names = sorted({destination.rsplit('/', 1)[-1]
                            for destination, files in installed.items()
                            if destination.endswith(('.esp', '.esm', '.esl'))
                            and any(f['path'].rsplit('/', 1)[-1].casefold() == original.casefold()
                                    for f in files)})
            names = [original if n.casefold() == original.casefold() else n for n in names]
            installed_names[id(p)] = names or [original]
            for name in sorted(set(names + [original]), key=str.casefold):
                if not any(owner is mod and target is p for owner, target in plugins[name.casefold()]):
                    plugins[name.casefold()].append((mod, p))
    edges = []
    dependency_edges = []
    documented_orders = []

    def edge(a, b, kind):
        item = {'from': a, 'to': b, 'kind': kind}
        if item not in edges:
            edges.append(item)

    def add(kind, severity, owners, title, items=(), **kwargs):
        findings.add(kind, severity, [m['name'] for m in owners], title, items, **kwargs)
        if severity == 'dependency' and len(owners) == 2:
            direction = {'from': owners[0]['name'], 'to': owners[1]['name'], 'kind': kind}
            if kwargs.get('label') == 'inferred':
                direction['label'] = 'inferred'
            if direction not in dependency_edges:
                dependency_edges.append(direction)

    def targets(name):
        return plugins.get(name.casefold(), [])

    def target_records(plugin, local):
        return [r for r in plugin['records'] if not r['is_override'] and
                (int(r['form_id'], 16) & 0xffffff) == (local & 0xffffff)]

    # Individual facts and hard/soft dependencies.
    override_users = defaultdict(set)
    for mod in mods:
        for p in mod['plugins']:
            for r in p['records']:
                if r['is_override']:
                    override_users[record_key(r)].add(mod['name'])
    for mod in mods:
        for fact in mod['facts']:
            extra = [evidence('verified', 'plugin bytes', 'Compared against plugin EditorIDs')] if fact.get('verified') else []
            add(fact['kind'], fact['severity'], [mod], fact['kind'], fact.get('items', []),
                label=fact['label'], source=fact['source'], extra=extra)
        for p in mod['plugins']:
            for master in p['masters']:
                owners = targets(master)
                if not owners:
                    base = master.casefold() in VANILLA
                    category = 'base/dlc' if base else 'cc' if is_vanilla(master) else 'third-party'
                    add('external-master', 'info', [mod], 'Master outside analysed set', [master + ' (' + category + ')'])
                for owner, target in owners:
                    if target['esl'] and master.casefold().endswith('.esp'):
                        add('master-ordering-note', 'info', [mod], 'ESL-flagged ESP master ordering', [master])
                    if owner is mod:
                        continue
                    add('load-order', 'dependency', [mod, owner], 'Master dependency; load after', [p['file_name'] + ' after ' + master])
                    edge(mod['name'], owner['name'], 'master')
            for r in p['records']:
                if r['is_override']:
                    owned = targets(r['owner'])
                    for owner, _target in owned:
                        if owner is not mod:
                            add('record-override', 'dependency', [mod, owner], 'Override of analysed owner; load after', ['%s:%06X' % record_key(r)])
                            edge(mod['name'], owner['name'], 'override')
                    if is_vanilla(r['owner']) and len(override_users[record_key(r)]) == 1:
                        add('vanilla-edit', 'info', [mod], 'Vanilla record override', ['%s:%06X' % record_key(r)])
                    elif not owned and not is_vanilla(r['owner']):
                        add('external-master-override', 'warning', [mod], 'Override of external master', ['%s:%06X' % record_key(r)], label='inferred', source='master classification')
                references = list(r['script_props'])
                references += [{'target_plugin': ref['plugin'], 'local_id': ref['local_id']} for ref in r['forced_refs'] + ([r['base']] if r['base'] else [])]
                for ref in references:
                    for owner, target in targets(ref['target_plugin']):
                        item = '%s:%06X' % (ref['target_plugin'], ref['local_id'])
                        if owner is not mod:
                            add('record-reference', 'dependency', [mod, owner], 'FormID reference to analysed plugin', [item])
                            edge(mod['name'], owner['name'], 'record-reference')
                        matched = target_records(target, ref['local_id'])
                        if not matched:
                            add('dangling-reference', 'collision', [mod, owner], 'Referenced FormID is absent', [item])
                        elif 'property' in ref:
                            ref.update({'target_type': matched[0]['type'], 'target_editor_id': matched[0]['editor_id']})
            # Cross references above mutate the shared property-link dictionaries.
        for script in mod['source_facts']:
            for call in script['form_calls']:
                owners = targets(call['plugin'])
                item = '%s:%06X' % (call['plugin'], call['local_id'])
                if not owners:
                    add('external-soft-reference', 'info', [mod], 'Source reference outside analysed set', [item], label='inferred', source=script['name'])
                for owner, target in owners:
                    if owner is mod:
                        continue
                    matched = target_records(target, call['local_id'])
                    verification = ['%s %s exists in %s' % (r['type'], r['editor_id'] or '(no EditorID)', target['file_name']) for r in matched]
                    if not matched:
                        verification = [item + ' absent from new-record index']
                    verified = [evidence('verified', target['file_name'], '; '.join(verification))]
                    add('soft-reference', 'dependency', [mod, owner], 'GetFormFromFile soft dependency', [item], label='inferred', source=script['name'], extra=verified)
                    expected = CAST_TYPES.get((call['cast'] or '').casefold())
                    if not matched or (expected and all(r['type'] != expected for r in matched)):
                        add('stale-form-id', 'collision', [mod, owner], 'Source FormID missing or cast type mismatched', [item + (' as ' + expected if expected else '')], label='inferred', source=script['name'], extra=verified)
                    for record in matched:
                        pinned = {n.casefold(): value & 0xffffff for n, value in owner['pinned_ids'].items()}
                        pinned_names = {n for n, value in pinned.items() if value == (call['local_id'] & 0xffffff)}
                        edid = (record['editor_id'] or '').casefold()
                        if ((edid in pinned and pinned[edid] != (call['local_id'] & 0xffffff)) or
                                (pinned_names and pinned_names != {edid})):
                            pinned_detail = [item + ' resolves to ' + (record['editor_id'] or '(no EditorID)')]
                            pinned_detail += ['%s pinned at %06X' % (name, value) for name, value in pinned.items()
                                              if name == edid or name in pinned_names]
                            add('pinned-id-drift', 'warning', [mod, owner], 'Source reference disagrees with pinned IDs', pinned_detail, label='inferred', source='source and formids.json', extra=verified)
            refs = {r.casefold() for r in script['references']}
            for other in mods:
                if other is mod:
                    continue
                calls = refs & {s.casefold() for s in other['script_names']}
                if calls:
                    add('script-call', 'dependency', [mod, other], 'Source references another mod script', calls, label='inferred', source=script['name'], hint='soft dependency; breaks if B renames')
                writes = [w['property'] + '.' + w['op'] for w in script['writes'] if other['prefix'] and w['property'].casefold().startswith(other['prefix'])]
                if writes:
                    add('foreign-write', 'probable', [mod, other], 'Script writes a foreign property', writes, label='inferred', source=script['name'])
        claim = mod['docs']
        if mod['plugins']:
            contradictions = []
            if claim['esl_claim'] and any(not p['esl'] for p in mod['plugins']):
                contradictions.append('ESL claim contradicted by plugin flags')
            if claim['only_master_claim'] and any([m.casefold() for m in p['masters']] != ['skyrim.esm'] for p in mod['plugins']):
                contradictions.append('Only Skyrim.esm master claim contradicted')
            if claim['no_vanilla_edits_claim'] and any(r['is_override'] and is_vanilla(r['owner']) for p in mod['plugins'] for r in p['records']):
                contradictions.append('No vanilla edits claim contradicted by overrides')
            if claim['places_nothing_claim'] and any(r['type'] in ('REFR', 'ACHR', 'ACRE', 'PGRE', 'PHZD', 'CELL') for p in mod['plugins'] for r in p['records']):
                contradictions.append('Places nothing claim contradicted by placements/cells')
            if contradictions:
                add('doc-mismatch', 'warning', [mod], 'Documentation contradicted by plugin', contradictions, label='inferred', source='documentation', extra=[evidence('verified', 'plugin bytes', 'Flags, masters and record index')])

    # Pair comparisons aggregate all colliding items rather than stopping at one.
    for a, b in combinations(mods, 2):
        owners = [a, b]
        pa = {path.rsplit('/', 1)[-1] for path in _installed(a) if path.endswith(('.esp', '.esm', '.esl'))}
        pb = {path.rsplit('/', 1)[-1] for path in _installed(b) if path.endswith(('.esp', '.esm', '.esl'))}
        if pa & pb:
            add('plugin-name', 'collision', owners, 'Same plugin filename', pa & pb)
        ia, ib = _index(a, 'owner'), _index(b, 'owner')
        common_override = {k for k in ia.keys() & ib.keys() if any(r['is_override'] for r in ia[k]) and any(r['is_override'] for r in ib[k])}
        if common_override:
            add('record-override', 'collision', owners, 'Both override the same record', ['%s:%06X' % k for k in common_override])
        scripted = [k for k in common_override if any(r['scripts'] for r in ia[k]) and any(r['scripts'] for r in ib[k])]
        if scripted:
            add('script-on-same-record', 'collision', owners, 'Scripts attached to the same override', ['%s:%06X' % k for k in scripted])
        eda, edb = _index(a, 'editor_id', True), _index(b, 'editor_id', True)
        if eda.keys() & edb.keys():
            add('editor-id', 'collision', owners, 'New records share an EditorID', eda.keys() & edb.keys())
        common_scripts = {s.casefold() for s in a['script_names']} & {s.casefold() for s in b['script_names']}
        for s in sorted(common_scripts):
            ev = []
            for mod in owners:
                verified = s in {x.casefold() for group in ('pex', 'vmad') for x in mod['scripts'][group]}
                ev.append(evidence('verified' if verified else 'inferred', mod['name'] + ' scripts', s))
            add('script-name', 'collision', owners, 'Same Papyrus script name', [s], label=ev[0]['label'], source=ev[0]['source'], extra=ev[1:])
        fa, fb = _installed(a), _installed(b)
        for path in sorted(fa.keys() & fb.keys()):
            if path.endswith(('.esp', '.esm', '.esl')):
                continue
            identical = {(f['crc32'], f['size']) for f in fa[path] + fb[path]}
            verified = all(f['source'] == 'release-zip' for f in fa[path] + fb[path])
            add('file-path', 'info' if len(identical) == 1 else 'collision', owners,
                'Identical duplicate' if len(identical) == 1 else 'Different files at the same installed destination', [path],
                label='verified' if verified else 'inferred', source='installed files', discriminator='identical' if len(identical) == 1 else 'different')
        for field, finding_kind in [('voice_folders', 'voice-folder'), ('sequences', 'seq')]:
            va = {x.replace('\\', '/').split('/')[-1].casefold() for x in a[field]}
            vb = {x.replace('\\', '/').split('/')[-1].casefold() for x in b[field]}
            if va & vb:
                add(finding_kind, 'collision', owners, 'Same ' + finding_kind, va & vb, source='shipped file paths')
        for field, finding_kind in [('modevents', 'modevent'), ('engine_settings', 'engine-setting'), ('writes', 'write-write')]:
            def values(mod):
                if field == 'engine_settings':
                    return {v['key'].casefold() for s in mod['source_facts'] for v in s[field]}
                if field == 'writes':
                    return {v['property'].casefold() for s in mod['source_facts'] for v in s[field]
                            if any(other is not a and other is not b and other['prefix'] and
                                   v['property'].casefold().startswith(other['prefix']) for other in mods)}
                return {v.casefold() for s in mod['source_facts'] for v in s[field]}
            common = values(a) & values(b)
            if common:
                add(finding_kind, 'probable', owners, 'Shared ' + finding_kind, common, label='inferred', source='Papyrus source')
        placements_a = [r for p in a['plugins'] for r in p['records'] if r['type'] in ('REFR', 'ACHR') and r['parent_cell'] and r['position']]
        placements_b = [r for p in b['plugins'] for r in p['records'] if r['type'] in ('REFR', 'ACHR') and r['parent_cell'] and r['position']]
        for ra in placements_a:
            for rb in placements_b:
                if key(ra['parent_cell']) != key(rb['parent_cell']):
                    continue
                distance = math.sqrt(sum((x - y) ** 2 for x, y in zip(ra['position'], rb['position'])))
                near = distance <= 128
                add('placement-overlap' if near else 'same-cell', 'probable' if near else 'info', owners,
                    'Nearby placements' if near else 'Placements in the same cell',
                    ['%s:%06X distance %.2f' % (key(ra['parent_cell']) + (distance,))],
                    hint='128-unit distance threshold is a heuristic; positions are verified', source='plugin positions')
        def forced(mod):
            return {key(ref) for p in mod['plugins'] for r in p['records'] if r['type'] == 'QUST' for ref in r['forced_refs'] if not targets(ref['plugin']) and key(ref) not in IGNORED_FORCED_REFS}
        shared_forced = forced(a) & forced(b)
        if shared_forced:
            add('alias-forced-ref', 'probable', owners, 'Quests force the same external reference', ['%s:%06X' % k for k in shared_forced])
        for author, other in ((a, b), (b, a)):
            aliases = {other['name'].casefold()} | {name for name, owners in plugins.items() if any(owner is other for owner, _ in owners)}
            claim = author['docs']
            def names(field):
                return {s.casefold() for s in claim[field]}
            if names('incompatible_with') & aliases:
                add('documented-incompatibility', 'probable', owners, 'Documented incompatibility', [author['name'] + ' names ' + other['name']], label='inferred', source='documentation')
            if names('load_after') & aliases:
                add('load-order', 'dependency', [author, other], 'Documented load-after request', [author['name'] + ' after ' + other['name']], label='inferred', source='documentation')
                documented_orders.append((author, other))
                edges.append({'from': author['name'], 'to': other['name'], 'kind': 'documented', 'label': 'inferred'})
            explicit_compatible = bool(names('compatible_with') & aliases)
            series_compatible = (claim['series_wide'] and other['docs']['series_wide'] and bool(names('series_links') & aliases))
            pair_items = [f for f in findings.items.values() if f['mods'] == sorted([a['name'], b['name']], key=str.casefold)]
            collisions = [f['kind'] for f in pair_items if f['severity'] == 'collision']
            if (explicit_compatible or series_compatible) and collisions:
                add('doc-mismatch', 'warning', owners, 'Compatibility claim contradicted by collisions', collisions, label='inferred', source='documentation', extra=[ev for f in pair_items if f['severity'] == 'collision' for ev in f['evidence']])
            data_edges = [f for f in pair_items if f['kind'] not in ('documented-incompatibility', 'doc-mismatch', 'documented-interaction')]
            if names('mentions') & aliases and not data_edges:
                add('documented-interaction', 'info', owners, 'Documented soft integration', [author['name'] + ' mentions ' + other['name']], label='inferred', source='documentation')
    # Order actual plugins, including multiple plugins within the same mod.
    # Mod-level dependencies alone can falsely imply a cycle when plugins interleave.
    canonical = {name: installed_names[id(owners[0][1])][0] for name, owners in plugins.items()}
    plugin_nodes = {name for names in installed_names.values() for name in names}
    plugin_edges = []
    for mod in mods:
        for plugin in mod['plugins']:
            for master in plugin['masters']:
                target = canonical.get(master.casefold(), master)
                plugin_nodes.add(target)
                for name in installed_names[id(plugin)]:
                    requirement = {'from': name, 'to': target, 'kind': 'master'}
                    if requirement not in plugin_edges:
                        plugin_edges.append(requirement)
    master_order, master_cycles = _topology(plugin_nodes, plugin_edges)
    for author, other in documented_orders:
        for a_plugin in author['plugins']:
            for b_plugin in other['plugins']:
                for a_name in installed_names[id(a_plugin)]:
                    for b_name in installed_names[id(b_plugin)]:
                        plugin_edges.append({'from': a_name, 'to': b_name,
                                             'kind': 'documented', 'label': 'inferred'})
    order, cycles = _topology(plugin_nodes, plugin_edges)
    conflicting = [e for e in plugin_edges if e['kind'] == 'documented'
                   and any(e['from'] in cycle and e['to'] in cycle for cycle in cycles)]
    if conflicting:
        order = master_order
        for constraint in conflicting:
            owners = {mod['name'] for name in (constraint['from'], constraint['to'])
                      for mod, _plugin in targets(name)}
            findings.add('order-conflict', 'warning', owners,
                         'Documented ordering constraints form a cycle',
                         [constraint['from'] + ' after ' + constraint['to']],
                         label='inferred', source='documentation',
                         hint='Suggested order falls back to hard master requirements')
    hard_edges = [e for e in edges if e['kind'] != 'documented']
    mod_order, _mod_cycles = _topology([m['name'] for m in mods], hard_edges if conflicting else edges)
    for cycle in master_cycles:
        cycle_mods = {mod['name'] for name in cycle for mod, _plugin in targets(name)}
        findings.add('master-cycle', 'collision', cycle_mods, 'Cyclic plugin load requirements', cycle,
                     hint='No valid order exists for this cycle')
    for mod in mods:
        count = sum(p['esl'] for p in mod['plugins'])
        if count:
            add('esl-budget', 'info', [mod], 'Light plugin slot usage', ['%d ESL plugins; 4096 light slots' % count])
    result = findings.finish()
    pairs = []
    for a, b in combinations(mods, 2):
        selected = [f for f in result if a['name'] in f['mods'] and b['name'] in f['mods']]
        status = min((f['severity'] for f in selected), key=SEVERITIES.index) if selected else 'none'
        pairs.append({'a': a['name'], 'b': b['name'], 'status': status, 'finding_ids': [f['id'] for f in selected]})
    return {'findings': result, 'pairs': pairs,
            'dependency_edges': sorted(dependency_edges, key=lambda e: (e['from'], e['to'], e['kind'])),
            'load_order': {'edges': sorted(edges, key=lambda e: (e['from'], e['to'], e['kind'])),
                           'plugin_edges': sorted(plugin_edges, key=lambda e: (e['from'], e['to'])),
                           'suggested_order': order, 'mod_order': mod_order, 'cycles': cycles},
            'stats': {'mods': len(mods), 'pairs': len(pairs), 'findings': len(result),
                      'severities': {s: sum(f['severity'] == s for f in result) for s in SEVERITIES[:-1]},
                      'esl_plugins': sum(p['esl'] for m in mods for p in m['plugins']), 'light_slots': 4096}}
