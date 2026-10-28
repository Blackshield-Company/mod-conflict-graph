"""Render deterministic, asset-free graph summaries."""
import copy
from datetime import datetime, timezone
import json
from pathlib import Path
import re

from . import __version__
from .analyze import SEVERITIES

SYMBOLS = dict(zip(SEVERITIES, ('X', 'P', 'w', 'd', 'i', '.')))


def graph_data(mods, analysis, inputs=None, full=False, no_timestamp=False):
    summaries = copy.deepcopy(sorted(mods, key=lambda m: m['name'].casefold()))
    if not full:
        for mod in summaries:
            for plugin in mod['plugins']:
                plugin.pop('records', None)
                plugin['cell_placement_count'] = len(plugin.pop('cell_placements', []))
    data = {'tool': 'mod-conflict-graph', 'version': __version__,
            'inputs': inputs or {}, 'mods': summaries}
    if not no_timestamp:
        data['generated_at'] = datetime.now(timezone.utc).isoformat()
    data.update(copy.deepcopy(analysis))
    return data


def escape(text):
    return str(text).replace('|', '\\|').replace('\n', ' ').replace('<', '&lt;').replace('>', '&gt;')


def report(data):
    lines = ['# Mod conflict graph', '', '## Summary', '', '| Severity | Count |', '| --- | ---: |']
    for severity in SEVERITIES[:-1]:
        lines.append('| %s | %d |' % (severity, data['stats']['severities'][severity]))
    lines += ['', '## Mods', '', '| Mod | Plugin | ESL | Masters | Records | Overrides | Scripts | Files | FOMOD options |', '| --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: |']
    mods = data['mods']
    for mod in mods:
        options = sum(len(g['options']) for s in (mod['fomod'] or {}).get('steps', []) for g in s['groups'])
        for p in mod['plugins'] or [{}]:
            lines.append('| %s%s | %s | %s | %s | %d | %d | %d | %d | %d |' % (
                escape(mod['name']), ' *' if mod['extra'] else '', escape(p.get('file_name', '—')),
                'yes' if p.get('esl') else 'no', escape(', '.join(p.get('masters', []))),
                p.get('record_count', 0), len(p.get('overrides', [])), len(mod['script_names']), len(mod['files']), options))
    names = [m['name'] for m in mods]
    statuses = {frozenset((p['a'], p['b'])): p['status'] for p in data['pairs']}
    lines += ['', '* marks a mod outside the primary set.', '', '## Pairwise status matrix', '',
              '| Mod | ' + ' | '.join(escape(n) for n in names) + ' |',
              '| --- | ' + ' | '.join('---' for _ in names) + ' |']
    for name in names:
        lines.append('| %s | %s |' % (escape(name), ' | '.join('.' if name == other else SYMBOLS[statuses.get(frozenset((name, other)), 'none')] for other in names)))
    lines += ['', 'Legend: `.` none, `i` info, `d` dependency, `w` warning, `P` probable, `X` collision.', '', '## Findings']
    for severity in SEVERITIES[:-1]:
        selected = sorted((f for f in data['findings'] if f['severity'] == severity), key=lambda f: (f['kind'], f['id']))
        if not selected:
            continue
        lines += ['', '### ' + severity]
        current = None
        for f in selected:
            if f['kind'] != current:
                current = f['kind']
                lines += ['', '#### ' + current]
            lines += ['', '**%s — %s** (%s)' % (f['id'], escape(f['title']), ', '.join(escape(x) for x in f['mods'])), '', escape(f['detail'])]
            for ev in f['evidence']:
                lines.append('- [%s] %s: %s' % (ev['label'], escape(ev['source']), escape(ev['detail'])))
            if f['hint']:
                lines += ['', 'Hint: ' + escape(f['hint'])]
    lines += ['', '## What was verified vs inferred', '',
              'Verified evidence comes from plugin bytes, archive directories, loose file bytes, and FOMOD XML. Inferred evidence comes from Papyrus source scanning, documentation, and package completeness assumptions. Probable placement impact uses a 128-unit heuristic over verified positions.', '', '## Gaps', '']
    gaps = sorted({mod['name'] + ': ' + gap for mod in mods for gap in mod['gaps']})
    lines += ['- ' + escape(gap) for gap in gaps] or ['No input-specific parsing or availability gaps were encountered.']
    return '\n'.join(lines) + '\n'


def mermaid(data):
    ids = {m['name']: 'm%d_%s' % (i, re.sub(r'[^a-zA-Z0-9_]', '_', m['name'])) for i, m in enumerate(data['mods'])}
    lines = ['graph LR', '  %% solid: dependency; thick: collision; dashed: probable/warning; *: extra']
    for mod in data['mods']:
        # Entity escaping prevents quotes, brackets and newlines from changing syntax.
        label = ''.join(c if c.isalnum() or c in ' ._-' else '#%d;' % ord(c) for c in mod['name'])
        lines.append('  %s["%s"]' % (ids[mod['name']], label))
        if mod['extra']:
            lines.append('  class %s extra' % ids[mod['name']])
    for pair in data['pairs']:
        if pair['status'] in ('none', 'info'):
            continue
        kinds = sorted({f['kind'] for f in data['findings'] if f['id'] in pair['finding_ids'] and f['severity'] not in ('info',)})
        if pair['status'] == 'dependency':
            directions = {(e['from'], e['to']) for e in data.get('dependency_edges', [])
                          if {e['from'], e['to']} == {pair['a'], pair['b']}}
            for start, end in sorted(directions or {(pair['a'], pair['b'])}):
                lines.append('  %s -->|"%s"| %s' % (ids[start], ', '.join(kinds), ids[end]))
        else:
            arrow = '==>' if pair['status'] == 'collision' else '-.->'
            lines.append('  %s %s|"%s"| %s' % (ids[pair['a']], arrow, ', '.join(kinds), ids[pair['b']]))
    lines.append('  classDef extra stroke-dasharray: 5 5')
    return '\n'.join(lines) + '\n'


def write(out, data, json_only=False):
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    (out / 'graph.json').write_text(json.dumps(data, indent=2, sort_keys=True, allow_nan=False) + '\n', encoding='utf-8')
    if not json_only:
        (out / 'report.md').write_text(report(data), encoding='utf-8')
        (out / 'graph.mmd').write_text(mermaid(data), encoding='utf-8')
