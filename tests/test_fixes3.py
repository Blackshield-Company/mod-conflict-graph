"""Regression coverage for installed plugin names and inferred order conflicts."""
from pathlib import Path
import tempfile
import unittest

from helpers import make_plugin, make_zip
from modconflict.analyze import analyze
from modconflict.extract import build_mod


class RoundThreeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def package(self, name, members):
        release = self.root / name
        make_zip(release / 'release.zip', members)
        return build_mod(name, release=release)

    def test_renamed_plugin_master_and_references(self):
        a = self.package('A', {
            'Original.esp': make_plugin(records=[('GLOB', 0x800, 'Target', [])]),
            'fomod/ModuleConfig.xml': '<config><requiredInstallFiles><file source="Original.esp" destination="Renamed.esp"/></requiredInstallFiles></config>',
        })
        for target, missing in [(0x800, False), (0x999, True)]:
            with self.subTest(target=target):
                b = self.package('B', {'B.esp': make_plugin(masters=['RENAMED.ESP'], records=[
                    {'type': 'QUST', 'id': 0x1000800, 'scripts': ['Main'],
                     'props': [('Main', 'Target', target)]}])})
                result = analyze([a, b])
                kinds = {f['kind'] for f in result['findings']}
                self.assertNotIn('external-master', kinds)
                self.assertIn('load-order', kinds)
                self.assertIn('record-reference', kinds)
                self.assertEqual('dangling-reference' in kinds, missing)
                order = result['load_order']['suggested_order']
                self.assertNotIn('Original.esp', order)
                self.assertLess(order.index('renamed.esp'), order.index('B.esp'))
                self.assertEqual(a['plugins'][0]['file_name'], 'Original.esp')
                if not missing:
                    link = b['plugins'][0]['script_property_links'][0]
                    self.assertEqual(link['target_editor_id'], 'Target')

    def test_documented_cycle_falls_back_to_master_order(self):
        a = self.package('A', {'A.esp': make_plugin()})
        b = self.package('B', {'B.esp': make_plugin(masters=['A.esp'])})
        c = self.package('C', {'C.esp': make_plugin(masters=['B.esp'])})
        a['docs']['load_after'] = ['B.esp']
        result = analyze([a, b, c])
        self.assertEqual(result['load_order']['suggested_order'],
                         ['Skyrim.esm', 'A.esp', 'B.esp', 'C.esp'])
        self.assertEqual(result['load_order']['cycles'], [['A.esp', 'B.esp']])
        conflict = next(f for f in result['findings'] if f['kind'] == 'order-conflict')
        self.assertEqual(conflict['severity'], 'warning')
        self.assertEqual(conflict['mods'], ['A', 'B'])
        self.assertIn('A.esp after B.esp', conflict['detail'])
        self.assertTrue(all(e['label'] == 'inferred' for e in conflict['evidence']))
        self.assertFalse(any(f['kind'] == 'master-cycle' for f in result['findings']))
        a['docs']['load_after'] = []
        clean = analyze([a, b, c])
        self.assertFalse(clean['load_order']['cycles'])
        self.assertFalse(any(f['kind'] == 'order-conflict' for f in clean['findings']))

    def test_original_name_is_private_to_renamed_plugins_package(self):
        for target, missing in [(0x800, False), (0x999, True)]:
            with self.subTest(target=target):
                a = self.package('A', {
                    'Original.esp': make_plugin(records=[
                        {'type': 'GLOB', 'id': 0x800, 'edid': 'Target'},
                        {'type': 'QUST', 'id': 0x801, 'scripts': ['Main'],
                         'props': [('Main', 'Self', target)]}]),
                    'fomod/ModuleConfig.xml': '<config><requiredInstallFiles><file source="Original.esp" destination="Renamed.esp"/></requiredInstallFiles></config>',
                })
                b = self.package('B', {'B.esp': make_plugin(
                    masters=['ORIGINAL.ESP'], records=[
                        ('GLOB', 0x800, 'ExternalOverride', []),
                        {'type': 'QUST', 'id': 0x1000801, 'scripts': ['Other'],
                         'props': [('Other', 'External', 0x800)]}])})
                result = analyze([a, b])
                external = [f for f in result['findings']
                            if f['kind'] == 'external-master']
                self.assertEqual(len(external), 1)
                self.assertEqual(external[0]['mods'], ['B'])
                self.assertIn('ORIGINAL.ESP (third-party)', external[0]['detail'])
                self.assertFalse(result['dependency_edges'])
                self.assertEqual(result['load_order']['plugin_edges'], [
                    {'from': 'B.esp', 'to': 'ORIGINAL.ESP', 'kind': 'master'}])
                dangling = [f for f in result['findings']
                            if f['kind'] == 'dangling-reference']
                self.assertEqual(bool(dangling), missing)
                if missing:
                    self.assertEqual(dangling[0]['mods'], ['A'])
                else:
                    self.assertEqual(a['plugins'][0]['script_property_links'][0]
                                     ['target_editor_id'], 'Target')
                self.assertEqual(b['plugins'][0]['script_property_links'][0]
                                 ['target_editor_id'], 'ExternalOverride')
                self.assertTrue(any(f['kind'] == 'external-master-override'
                                    and f['mods'] == ['B']
                                    for f in result['findings']))
