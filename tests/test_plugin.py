import struct
import unittest

from helpers import make_plugin, vmad, sub, group
from modconflict.plugin import parse_plugin, parse_vmad_scripts, PluginError


class PluginTests(unittest.TestCase):
    def test_header_and_compressed_extended(self):
        data = make_plugin(esl=True, esm=True, localized=True, masters=['Skyrim.esm'], records=[
            {'type': 'GLOB', 'id': 0x1000800, 'edid': 'AA_Global', 'compressed': True, 'extended': True}])
        p = parse_plugin(data, 'A.esp')
        self.assertTrue(p.is_esl and p.is_esm)
        self.assertEqual(p.version, 1.7)
        self.assertEqual(p.records[0].editor_id, 'AA_Global')
        self.assertFalse(p.records[0].is_override)
        self.assertEqual(p.owner_key(0x123456), ('Skyrim.esm', 0x123456))
        self.assertEqual(p.owner_key(0x1000800), ('A.esp', 0x800))

    def test_groups_placement_cell_and_alias(self):
        data = make_plugin(masters=['Skyrim.esm'], records=[
            {'type': 'REFR', 'id': 0x1000801, 'cell': 0x123, 'world': 0x456,
             'base': 0x789, 'position': [1, 2, 3]},
            {'type': 'CELL', 'id': 0x1000802, 'cell_flags': 1},
            {'type': 'QUST', 'id': 0x1000803, 'forced': [0x123]}])
        records = parse_plugin(data, 'A.esp').records
        self.assertEqual((records[0].parent_cell, records[0].parent_world), (0x123, 0x456))
        self.assertEqual(records[0].position, [1, 2, 3])
        self.assertEqual(records[0].base, 0x789)
        self.assertIsNone(records[1].parent_cell)
        self.assertTrue(records[1].interior)
        self.assertEqual(records[2].forced_refs, [0x123])

    def test_vmad_formats_and_arrays(self):
        for fmt in (1, 2):
            data = make_plugin(records=[{'type': 'QUST', 'id': 0x800, 'scripts': ['AA_S'],
                                        'props': [('AA_S', 'One', 0x123456), ('AA_S', 'Many', [0x800, 0x900])], 'obj_format': fmt}])
            p = parse_plugin(data, 'A.esp')
            self.assertEqual(p.records[0].script_props, [('AA_S', 'One', 0x123456), ('AA_S', 'Many', 0x800), ('AA_S', 'Many', 0x900)])
            self.assertFalse(p.warnings)
        self.assertEqual(parse_vmad_scripts(vmad(['AA_S'])), (['AA_S'], True))

    def test_odd_subrecords_warn(self):
        for raw in (b'XXXX\x01\x00?', b'EDID\xff\xff?', sub('VMAD', b'?'), b'?'):
            p = parse_plugin(make_plugin(records=[{'type': 'QUST', 'id': 0x800, 'raw': raw}]), 'A.esp')
            self.assertTrue(p.warnings)

    def test_bad_structure(self):
        for data in (b'no', make_plugin()[:-1], make_plugin() + b'?', make_plugin() + group(b'', 0, 0)[:-1]):
            with self.assertRaises(PluginError):
                parse_plugin(data, 'bad.esp')

    def test_empty_and_override(self):
        self.assertEqual(parse_plugin(make_plugin(), 'A.esp').records, [])
        p = parse_plugin(make_plugin(masters=['Light.esl'], records=[('GLOB', 0x123456, None, [])]), 'A.esp')
        self.assertTrue(p.records[0].is_override)
        self.assertEqual(p.local_id(0x123456), 0x123456)

    def test_nonfinite_position_and_short_fields_warn(self):
        p = parse_plugin(make_plugin(records=[{'type': 'REFR', 'id': 0x800,
            'position': [float('nan'), 0, 0], 'raw': sub('NAME', b'x')}]), 'A.esp')
        self.assertIsNone(p.records[0].position)
        self.assertEqual(len(p.warnings), 2)

    def test_vmad_invalid_types_and_bounds(self):
        from helpers import wstring
        header = struct.pack('<hhH', 5, 2, 1) + wstring('AA_S') + b'\0\x01\x00' + wstring('Prop')
        for suffix in (b'\xff\0', b'\x01\0bad', b'\x0b\0\xff\xff\xff\xff', b'\x03\0?'):
            names, complete = parse_vmad_scripts(header + suffix)
            self.assertEqual(names, ['AA_S'])
            self.assertFalse(complete)
