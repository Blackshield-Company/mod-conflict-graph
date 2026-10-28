"""Conservative Papyrus text scanning. Returned facts never contain source lines."""
import re
from collections import Counter
from pathlib import PurePosixPath


def script_name(path):
    return PurePosixPath(path.replace('\\', '/')).stem


def strip_comments(text):
    """Remove comments without treating semicolons inside strings as comments."""
    return re.sub(r'"(?:\\.|[^"\\])*"|;/[\s\S]*?/;|;[^\n]*',
                  lambda m: m.group() if m.group().startswith('"') else ' ', text)


def own_prefix(editor_ids, script_names=()):
    for names in (editor_ids, script_names):
        counts = Counter(m.group().casefold() for name in names
                         for m in [re.match(r'^[A-Za-z]{2,5}_', name or '')] if m)
        if counts:
            return sorted(counts, key=lambda x: (-counts[x], x))[0]
    return ''


def scan(text, path=''):
    text = strip_comments(text)
    header = re.search(r'\bScriptname\s+(\w+)(?:\s+extends\s+(\w+))?', text, re.I)
    properties = [{'type': m[0], 'name': m[1]} for m in
                  re.findall(r'\b(\w+)(?:\[\])?\s+Property\s+(\w+)', text, re.I)]
    calls = []
    for match in re.finditer(r'Game\s*\.\s*GetFormFromFile\s*\(\s*(0x[\da-f]+|\d+)\s*,\s*"([^"\n]+)"\s*\)(?:\s+as\s+(\w+))?', text, re.I):
        raw, plugin, cast = match.groups()
        calls.append({'plugin': plugin, 'local_id': int(raw, 16 if raw.lower().startswith('0x') else 10), 'cast': cast})
    events = sorted(set(re.findall(r'\b(?:SendModEvent|RegisterForModEvent)\s*\(\s*"([^"\n]+)"', text, re.I)))
    settings = [{'call': m[0], 'key': m[1]} for m in re.findall(
        r'\b((?:Game\.SetGameSetting\w*|Utility\.SetINI\w*))\s*\(\s*"([^"\n]+)"', text, re.I)]
    writes = []
    for prop in properties:
        for op in re.findall(r'\b' + re.escape(prop['name']) + r'\s*\.\s*(SetValueInt|SetValue|Mod|SetStage|Start|Stop|ForceRefTo)\s*\(', text, re.I):
            writes.append({'script': header[1] if header else script_name(path), 'property': prop['name'], 'op': op})
    # Only retain candidate identifiers, never full expressions or source snippets.
    identifiers = set(re.findall(r'\b[A-Z]{2,4}_\w+\b', text))
    identifiers.update(re.findall(r'\b(\w+)\s*\.\s*\w+\s*\(', text))
    return {'name': header[1] if header else script_name(path),
            'extends': header[2] if header else None, 'properties': properties,
            'form_calls': calls, 'references': sorted(identifiers), 'modevents': events,
            'engine_settings': settings, 'writes': writes, 'label': 'inferred'}
