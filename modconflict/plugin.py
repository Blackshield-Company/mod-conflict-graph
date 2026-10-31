"""Read-only parser for Skyrim SE plugin files (.esp/.esm/.esl).

Stdlib only. Extracts the header (flags, masters), and a record index with
FormIDs, EditorIDs, record types and attached Papyrus script names. It never
writes a plugin and it never copies record payloads out of the file.
"""

import struct
import math
import zlib

FLAG_ESM = 0x1
FLAG_LOCALIZED = 0x80
FLAG_ESL = 0x200
REC_COMPRESSED = 0x00040000

# Player alias targets are routine quest plumbing, not contested references.
IGNORED_FORCED_REFS = {('skyrim.esm', 0x14)}


class PluginError(Exception):
    """The file is not a readable Skyrim SE plugin."""


class Record:
    __slots__ = ("type", "form_id", "editor_id", "scripts", "flags", "is_override",
                 "parent_cell", "parent_world", "base", "position", "cell_flags",
                 "interior", "script_props", "forced_refs")

    def __init__(self, rtype, form_id, flags):
        self.type = rtype
        self.form_id = form_id          # raw 32-bit FormID as stored in the file
        self.flags = flags
        self.editor_id = None
        self.scripts = []
        self.is_override = False
        self.parent_cell = self.parent_world = None
        self.base = self.position = self.cell_flags = self.interior = None
        self.script_props = []
        self.forced_refs = []


class Plugin:
    def __init__(self, name):
        self.name = name
        self.flags = 0
        self.masters = []
        self.records = []               # every non-TES4 record, in file order
        self.version = None             # HEDR version float
        self.author = None              # TES4 CNAM
        self.description = None         # TES4 SNAM
        self.warnings = []

    @property
    def is_esl(self):
        return bool(self.flags & FLAG_ESL)

    @property
    def is_esm(self):
        return bool(self.flags & FLAG_ESM)

    def master_index(self, form_id):
        return form_id >> 24

    def owner_of(self, form_id):
        """Name of the plugin that owns form_id (a master name, or this plugin)."""
        idx = form_id >> 24
        if idx < len(self.masters):
            return self.masters[idx]
        return self.name

    def owner_key(self, form_id):
        """Stable ownership key; retain all 24 bits even for light masters."""
        return self.owner_of(form_id), self.local_id(form_id)

    def local_id(self, form_id):
        """FormID with the load-order byte stripped (24-bit object id)."""
        return form_id & 0x00FFFFFF


def _subrecords(data):
    """Yield (type, payload) for a record's field data, handling XXXX."""
    pos = 0
    size_override = None
    n = len(data)
    while pos + 6 <= n:
        stype = data[pos:pos + 4]
        size = struct.unpack_from("<H", data, pos + 4)[0]
        pos += 6
        if stype == b"XXXX":
            if size != 4 or pos + 4 > n:
                raise PluginError("malformed XXXX subrecord")
            size_override = struct.unpack_from("<I", data, pos)[0]
            pos += size
            continue
        if size_override is not None:
            size = size_override
            size_override = None
        if pos + size > n:
            raise PluginError("subrecord %r overruns its record" % stype)
        yield stype, data[pos:pos + size]
        pos += size
    if pos != n or size_override is not None:
        raise PluginError("trailing truncated subrecord")


def _safe_subrecords(data, plugin):
    try:
        yield from _subrecords(data)
    except (PluginError, struct.error) as exc:
        plugin.warnings.append(str(exc))


def _zstring(raw):
    end = raw.find(b"\0")
    if end >= 0:
        raw = raw[:end]
    return raw.decode("utf-8", "replace")


def _wstring(data, pos):
    (length,) = struct.unpack_from("<H", data, pos)
    pos += 2
    if pos + length > len(data):
        raise PluginError("string overruns VMAD")
    return data[pos:pos + length].decode("utf-8", "replace"), pos + length


def _skip_property_value(data, pos, ptype, obj_format):
    if ptype == 1:          # object
        return pos + 8
    if ptype == 2:          # string
        return _wstring(data, pos)[1]
    if ptype in (3, 4):     # int, float
        return pos + 4
    if ptype == 5:          # bool
        return pos + 1
    if 11 <= ptype <= 15:   # arrays
        (count,) = struct.unpack_from("<I", data, pos)
        pos += 4
        if ptype == 11:
            return pos + 8 * count
        if ptype == 12:
            for _ in range(count):
                pos = _wstring(data, pos)[1]
            return pos
        if ptype in (13, 14):
            return pos + 4 * count
        return pos + count
    raise PluginError("unknown VMAD property type %d" % ptype)


def parse_vmad(data):
    """Return names, object property links, and whether the script list parsed."""
    names, props = [], []
    try:
        _version, obj_format, count = struct.unpack_from("<hhH", data, 0)
        if obj_format not in (1, 2):
            raise PluginError("unknown VMAD object format")
        pos = 6
        for _ in range(count):
            name, pos = _wstring(data, pos)
            names.append(name)
            pos += 1
            pcount = struct.unpack_from("<H", data, pos)[0]
            pos += 2
            for _ in range(pcount):
                pname, pos = _wstring(data, pos)
                ptype = data[pos]
                pos += 2
                if ptype in (1, 11):
                    n = 1
                    if ptype == 11:
                        n = struct.unpack_from("<I", data, pos)[0]
                        pos += 4
                    if n > (len(data) - pos) // 8:
                        raise PluginError("object property overruns VMAD")
                    for _ in range(n):
                        offset = 4 if obj_format == 2 else 0
                        fid = struct.unpack_from("<I", data, pos + offset)[0]
                        props.append((name, pname, fid))
                        pos += 8
                else:
                    pos = _skip_property_value(data, pos, ptype, obj_format)
                if pos > len(data):
                    raise PluginError("property overruns VMAD")
        return names, props, True
    except (PluginError, struct.error, IndexError):
        return names, props, False


def parse_vmad_scripts(data):
    """Backward-compatible script-name API (fragment tails are ignored)."""
    names, _props, complete = parse_vmad(data)
    return names, complete


MAX_RECORD_BYTES = 64 * 1024 * 1024


def _read_record_body(data, pos, size, flags):
    body = data[pos:pos + size]
    if flags & REC_COMPRESSED:
        if len(body) < 4:
            raise PluginError("compressed record is truncated")
        try:
            declared = struct.unpack_from('<I', body)[0]
            if declared > MAX_RECORD_BYTES:
                raise PluginError("compressed record exceeds decompression cap")
            decoder = zlib.decompressobj()
            # Zero max_length means unlimited, so use one byte for a zero declaration.
            body = decoder.decompress(body[4:], max(1, declared))
            if len(body) != declared or not decoder.eof or decoder.unconsumed_tail or decoder.unused_data:
                raise PluginError("compressed record size mismatch or overflow")
        except zlib.error as exc:
            raise PluginError("compressed record: %s" % exc)
    return body


def parse_header(data, name):
    """Parse only the TES4 header (flags, masters, author, description).

    Returns a Plugin without records; raise PluginError if it is not a plugin.
    """
    if len(data) < 24 or data[:4] != b"TES4":
        raise PluginError("%s: missing TES4 header" % name)
    plugin = Plugin(name)
    size, flags = struct.unpack_from("<II", data, 4)
    plugin.flags = flags
    if 24 + size > len(data):
        raise PluginError("%s: TES4 header overruns the file" % name)
    try:
        header_body = _read_record_body(data, 24, size, flags)
    except PluginError as exc:
        plugin.warnings.append(str(exc))
        header_body = b''
    for stype, payload in _safe_subrecords(header_body, plugin):
        if stype == b"HEDR" and len(payload) >= 4:
            plugin.version = round(struct.unpack_from("<f", payload)[0], 2)
        elif stype == b"MAST":
            plugin.masters.append(_zstring(payload))
        elif stype == b"CNAM":
            plugin.author = _zstring(payload)
        elif stype == b"SNAM":
            plugin.description = _zstring(payload)
        elif stype == b"HEDR":
            plugin.warnings.append("Short HEDR subrecord")
    return plugin


def parse_plugin(data, name):
    """Parse plugin bytes; raise PluginError if it is not a Skyrim SE plugin."""
    plugin = parse_header(data, name)
    pos = 24 + struct.unpack_from("<I", data, 4)[0]
    stack = []          # (end offset, group type, label) for enclosing GRUPs
    total = len(data)
    while pos < total:
        while stack and pos >= stack[-1][0]:
            stack.pop()
        if pos + 24 > total:
            raise PluginError("%s: truncated record header at offset %d" % (name, pos))
        rtype = data[pos:pos + 4]
        if rtype == b"GRUP":
            (gsize,) = struct.unpack_from("<I", data, pos + 4)
            if gsize < 24 or pos + gsize > total:
                raise PluginError("%s: bad GRUP size at offset %d" % (name, pos))
            if stack and pos + gsize > stack[-1][0]:
                raise PluginError("GRUP overruns parent")
            label, group_type = struct.unpack_from("<Ii", data, pos + 8)
            stack.append((pos + gsize, group_type, label))
            pos += 24
            continue
        rsize, rflags, form_id = struct.unpack_from("<III", data, pos + 4)
        pos += 24
        if pos + rsize > total or (stack and pos + rsize > stack[-1][0]):
            raise PluginError("%s: record overruns the file at offset %d" % (name, pos))
        record = Record(rtype.decode("ascii", "replace"), form_id, rflags)
        for _end, group_type, label in stack:
            if group_type in (6, 7, 8, 9):
                record.parent_cell = label
            elif group_type == 1:
                record.parent_world = label
        record.is_override = (form_id >> 24) < len(plugin.masters)
        try:
            body = _read_record_body(data, pos, rsize, rflags)
            for stype, payload in _subrecords(body):
                if stype == b"EDID":
                    record.editor_id = _zstring(payload)
                elif stype == b"NAME" and record.type in ("REFR", "ACHR", "ACRE", "PGRE", "PHZD") and len(payload) >= 4:
                    record.base = struct.unpack_from("<I", payload)[0]
                elif stype == b"DATA" and record.type in ("REFR", "ACHR", "ACRE", "PGRE", "PHZD") and len(payload) >= 24:
                    position = list(struct.unpack_from("<fff", payload))
                    if all(math.isfinite(x) for x in position):
                        record.position = position
                    else:
                        plugin.warnings.append("Non-finite position in %08X" % form_id)
                elif stype == b"DATA" and record.type == "CELL" and payload:
                    record.cell_flags = payload[0]
                    record.interior = bool(payload[0] & 1)
                elif stype == b"ALFR" and record.type == "QUST" and len(payload) >= 4:
                    record.forced_refs.append(struct.unpack_from("<I", payload)[0])
                elif stype in (b"NAME", b"ALFR", b"DATA"):
                    expected = 1 if record.type == "CELL" else 24 if stype == b"DATA" else 4
                    if len(payload) < expected:
                        plugin.warnings.append("Short %s subrecord on %08X" % (stype.decode(), form_id))
                elif stype == b"VMAD":
                    scripts, props, complete = parse_vmad(payload)
                    record.scripts = scripts
                    record.script_props = props
                    if not complete:
                        plugin.warnings.append(
                            "VMAD of %08X could not be fully parsed" % form_id)
        except PluginError as exc:
            plugin.warnings.append("record %s %08X: %s" % (record.type, form_id, exc))
        plugin.records.append(record)
        pos += rsize
    return plugin


def read_plugin(path):
    import os
    with open(path, "rb") as handle:
        return parse_plugin(handle.read(), os.path.basename(path))
