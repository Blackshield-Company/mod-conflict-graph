"""Synthetic binary and tree builders. Assets exist only in temporary directories."""
import json
from pathlib import Path
import struct
import zipfile
import zlib


def sub(tag, data, extended=False):
    tag = tag.encode() if isinstance(tag, str) else tag
    if extended:
        return b'XXXX' + struct.pack('<HI', 4, len(data)) + tag + b'\0\0' + data
    return tag + struct.pack('<H', len(data)) + data


def wstring(value):
    raw = value.encode()
    return struct.pack('<H', len(raw)) + raw


def vmad(scripts, props=(), obj_format=2):
    out = struct.pack('<hhH', 5, obj_format, len(scripts))
    for name in scripts:
        entries = [p for p in props if p[0] == name]
        out += wstring(name) + b'\0' + struct.pack('<H', len(entries))
        for _script, prop, values in entries:
            array = isinstance(values, list)
            out += wstring(prop) + bytes([11 if array else 1, 0])
            values = values if array else [values]
            if array:
                out += struct.pack('<I', len(values))
            for fid in values:
                out += struct.pack('<HHI', 0, 0, fid) if obj_format == 2 else struct.pack('<IHH', fid, 0, 0)
    return out


def record(tag, fid, fields, compressed=False):
    flags = 0x40000 if compressed else 0
    if compressed:
        fields = struct.pack('<I', len(fields)) + zlib.compress(fields)
    return tag.encode() + struct.pack('<III', len(fields), flags, fid) + bytes(8) + fields


def group(data, group_type, label):
    return b'GRUP' + struct.pack('<IIi', len(data) + 24, label, group_type) + bytes(8) + data


def make_plugin(name='Test.esp', esl=False, masters=(), records=(), esm=False, localized=False):
    head = sub('HEDR', struct.pack('<fII', 1.7, len(records), 0x800))
    head += b''.join(sub('MAST', m.encode() + b'\0') for m in masters)
    flags = (0x200 if esl else 0) | (1 if esm else 0) | (0x80 if localized else 0)
    out = b'TES4' + struct.pack('<III', len(head), flags, 0) + bytes(8) + head
    for item in records:
        if not isinstance(item, dict):
            typ, fid, edid, scripts = item[:4]
            item = {'type': typ, 'id': fid, 'edid': edid, 'scripts': scripts}
        fields = b''
        if item.get('edid'):
            fields += sub('EDID', item['edid'].encode() + b'\0', item.get('extended', False))
        if item.get('scripts'):
            fields += sub('VMAD', vmad(item['scripts'], item.get('props', ()), item.get('obj_format', 2)))
        if 'base' in item:
            fields += sub('NAME', struct.pack('<I', item['base']))
        if 'position' in item:
            fields += sub('DATA', struct.pack('<ffffff', *(list(item['position']) + [0, 0, 0])))
        if 'cell_flags' in item:
            fields += sub('DATA', bytes([item['cell_flags']]))
        fields += b''.join(sub('ALFR', struct.pack('<I', x)) for x in item.get('forced', []))
        fields += item.get('raw', b'')
        body = record(item['type'], item['id'], fields, item.get('compressed', False))
        if 'cell' in item:
            body = group(body, 6, item['cell'])
        if 'world' in item:
            body = group(body, 1, item['world'])
        out += body
    return out


def make_zip(path, members):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as out:
        for name, value in members.items():
            out.writestr(name, value.encode() if isinstance(value, str) else value)
    return path


def make_clone(path, readme=None, psc=None, formids=None, fomod=None):
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    entries = {}
    if readme is not None:
        entries['README.md'] = readme
    for name, text in (psc or {}).items():
        entries['src/papyrus/' + name + '.psc'] = text
    if formids is not None:
        entries['src/plugin/formids.json'] = json.dumps(formids)
    if fomod is not None:
        entries['fomod/ModuleConfig.xml'] = fomod
    for name, text in entries.items():
        dst = path / name
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(text)
    return path
