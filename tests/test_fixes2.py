"""Second review regression cases; all assets are temporary."""
import contextlib
import io
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch
import zlib

from helpers import make_clone, make_plugin, make_zip, sub
from modconflict.analyze import analyze, _installed, _topology
from modconflict.extract import build_mod
from modconflict.plugin import parse_plugin, MAX_RECORD_BYTES
from modconflict.cli import main


class RoundTwoTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def package(self, name, members):
        release = self.root / name
        make_zip(release / 'one.zip', members)
        return build_mod(name, release=release)

    def config(self, source, dest):
        return '<config><requiredInstallFiles><file source="%s" destination="%s"/></requiredInstallFiles></config>' % (source, dest)

    def kinds(self, *mods):
        return {f['kind']: f for f in analyze(mods)['findings']}

    def test_installed_plugin_names_even_unreadable(self):
        a = self.package('A', {'Original.esp': b'broken', 'fomod/ModuleConfig.xml': self.config('Original.esp', 'Shared.ESP')})
        b = self.package('B', {'Different.esp': make_plugin(), 'fomod/ModuleConfig.xml': self.config('Different.esp', 'shared.esp')})
        self.assertIn('plugin-name', self.kinds(a, b))
        c = self.package('C', {'Original.esp': make_plugin(), 'fomod/ModuleConfig.xml': self.config('Original.esp', 'Unique.esp')})
        self.assertNotIn('plugin-name', self.kinds(a, c))

    def test_archive_configs_and_unconfigured_archive_stay_separate(self):
        release = self.root / 'A'
        for archive, dest, payload in [('one', 'first.bin', b'one'), ('two', 'second.bin', b'two')]:
            make_zip(release / (archive + '.zip'), {'payload.bin': payload, 'fomod/ModuleConfig.xml': self.config('payload.bin', dest)})
        make_zip(release / 'three.zip', {'loose.bin': b'loose'})
        a = build_mod('A', release=release)
        installed = _installed(a)
        self.assertEqual([f['crc32'] for f in installed['first.bin']], ['7a6c86f1'])
        self.assertEqual(len(installed['second.bin']), 1)
        self.assertIn('loose.bin', installed)
        self.assertNotIn('payload.bin', installed)
        for dest in ('first.bin', 'second.bin', 'loose.bin'):
            self.assertIn('file-path', self.kinds(a, self.package('B', {dest: b'other'})))

    def test_compressed_size_bounds_and_recovery(self):
        fields = sub('EDID', b'Hidden\0')
        for declared, packed in [(1, zlib.compress(fields)), (len(fields)+1, zlib.compress(fields)),
                                 (MAX_RECORD_BYTES+1, zlib.compress(fields)), (0, zlib.compress(fields)),
                                 (len(fields), zlib.compress(fields)[:-1]), (len(fields), b'bad')]:
            with self.subTest(declared=declared, packed=packed):
                body = struct.pack('<I', declared) + packed
                raw = b'GLOB' + struct.pack('<III', len(body), 0x40000, 0x800) + bytes(8) + body
                # Use the exact TES4 header length rather than fixture assumptions.
                tail = make_plugin(records=[('GLOB', 0x801, 'Visible', [])])
                data = make_plugin() + raw + tail[24 + struct.unpack_from('<I', tail, 4)[0]:]
                parsed = parse_plugin(data, 'A.esp')
                self.assertTrue(parsed.warnings)
                self.assertIsNone(parsed.records[0].editor_id)
                self.assertEqual(parsed.records[1].editor_id, 'Visible')
        parsed = parse_plugin(make_plugin(records=[{'type': 'GLOB', 'id': 0x800, 'edid': 'OK', 'compressed': True}]), 'A.esp')
        self.assertFalse(parsed.warnings)
        self.assertEqual(parsed.records[0].editor_id, 'OK')

    def test_malformed_metadata_gaps_and_cli(self):
        clone = make_clone(self.root / 'clones/A', 'README', formids={'Bad': float('inf')})
        badxml = b'<?xml version="1.0" encoding="unknown-codec"?><config/>'
        for filename in ('ModuleConfig.xml', 'info.xml'):
            dst = clone / 'fomod' / filename
            dst.parent.mkdir(exist_ok=True)
            dst.write_bytes(badxml)
        mod = build_mod('A', clone=clone)
        self.assertIn('Invalid pinned formids.json', mod['gaps'])
        self.assertIn('Unparsed FOMOD info.xml', mod['gaps'])
        self.assertIn('Unparsed FOMOD ModuleConfig.xml', mod['gaps'])
        self.assertEqual(main(['--clones', str(self.root / 'clones'), '--out', str(self.root / 'out'), '--quiet']), 0)
        with patch('modconflict.cli.build_mod', side_effect=OverflowError('bad metadata')), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(['--clones', str(self.root / 'clones')]), 2)

    def test_documented_order_uses_author_direction(self):
        a = self.package('A', {'A.esp': make_plugin()})
        z = self.package('Z', {'Z.esp': make_plugin()})
        z['docs']['load_after'] = ['A.esp']
        result = analyze([a, z])
        self.assertIn({'from': 'Z', 'to': 'A', 'kind': 'load-order', 'label': 'inferred'}, result['dependency_edges'])
        self.assertNotIn({'from': 'A', 'to': 'Z', 'kind': 'load-order'}, result['dependency_edges'])
        order = result['load_order']['suggested_order']
        self.assertLess(order.index('A.esp'), order.index('Z.esp'))
        self.assertTrue(any(e.get('label') == 'inferred' for e in result['load_order']['plugin_edges']))
        z['docs']['load_after'] = []
        self.assertFalse(analyze([a, z])['load_order']['plugin_edges'])

    def test_internal_references_validated_without_dependency(self):
        for target, missing in [(0x900, True), (0x800, False)]:
            a = self.package('A', {'A.esp': make_plugin(records=[{'type': 'QUST', 'id': 0x800, 'scripts': ['Main'], 'props': [('Main', 'Target', target)], 'forced': [target]}, {'type': 'REFR', 'id': 0x801, 'base': target}])})
            kinds = self.kinds(a)
            self.assertEqual('dangling-reference' in kinds, missing)
            self.assertNotIn('record-reference', kinds)
            self.assertFalse(analyze([a])['dependency_edges'])

    def test_vanilla_classification_and_claims(self):
        for master, vanilla in [('Skyrim.esm', True), ('Update.esm', True), ('Dawnguard.esm', True), ('HearthFires.esm', True), ('Dragonborn.esm', True), ('ccBGSSSE001.esm', True), ('ccfsvsse001.esl', True), ('ccOther.esm', False), ('ThirdParty.esm', False)]:
            with self.subTest(master=master):
                a = self.package('A', {'A.esp': make_plugin(masters=[master], records=[('GLOB', 0x123, 'Override', [])])})
                a['docs']['no_vanilla_edits_claim'] = True
                kinds = self.kinds(a)
                self.assertEqual('vanilla-edit' in kinds, vanilla)
                self.assertEqual('doc-mismatch' in kinds, vanilla)
                self.assertEqual('external-master-override' in kinds, not vanilla)
                if not vanilla:
                    self.assertEqual(kinds['external-master-override']['severity'], 'warning')
                    self.assertEqual(kinds['external-master-override']['evidence'][0]['label'], 'inferred')
                owner = self.package('Owner', {master: make_plugin()})
                self.assertEqual('doc-mismatch' in self.kinds(a, owner), vanilla)

    def test_iterative_topology_long_chain_and_cycle(self):
        nodes = ['N%04d' % i for i in range(1100)]
        edges = [{'from': a, 'to': b} for a, b in zip(nodes, nodes[1:])]
        order, cycles = _topology(nodes, edges)
        self.assertFalse(cycles)
        self.assertEqual(order[1:], list(reversed(nodes)))
        edges.append({'from': nodes[-1], 'to': nodes[0]})
        self.assertEqual(_topology(nodes, edges)[1], [nodes])
