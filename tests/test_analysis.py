import copy
from pathlib import Path
import tempfile
import unittest

from helpers import make_plugin, make_zip, make_clone
from modconflict.extract import build_mod
from modconflict.analyze import analyze, Findings


class AnalysisTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def mod(self, name, records=(), masters=(), files=None, psc=None, readme='Ordinary mod.', esl=False, plugin=None, pins=None, config=None):
        clone = make_clone(self.root / 'clones' / name, readme, psc, pins, config)
        members = {plugin or name + '.esp': make_plugin(esl=esl, masters=masters, records=records)}
        members.update(files or {})
        if config:
            members['fomod/ModuleConfig.xml'] = config
        make_zip(self.root / 'releases' / name / 'v1.zip', members)
        return build_mod(name, clone, self.root / 'releases' / name)

    def kinds(self, mods, pair=True):
        return {f['kind']: f for f in analyze(mods)['findings'] if (len(f['mods']) > 1) == pair}

    def test_empty_pair_negative(self):
        a, b = self.mod('A'), self.mod('B')
        self.assertEqual(self.kinds([a, b]), {})
        self.assertEqual(analyze([a, b])['pairs'][0]['status'], 'none')

    def test_names_editor_ids_and_scripts(self):
        a = self.mod('A', [('GLOB', 0x800, 'Shared', ['SharedScript'])], files={'Scripts/Extra.pex': b'one'}, plugin='same.esp')
        b = self.mod('B', [('QUST', 0x900, 'SHARED', ['sharedscript'])], files={'Source/Scripts/Extra.psc': 'Scriptname Extra'}, plugin='SAME.ESP')
        kinds = self.kinds([a, b])
        self.assertTrue({'plugin-name', 'editor-id', 'script-name'} <= kinds.keys())
        labels = {e['label'] for e in kinds['script-name']['evidence']}
        self.assertEqual(labels, {'verified', 'inferred'})
        a['plugins'][0]['records'][0]['is_override'] = True
        self.assertNotIn('editor-id', self.kinds([a, b]))

    def test_shared_overrides_and_scripts(self):
        records = [('QUST', 0x123, 'Vanilla', ['AA_S'])]
        a, b = self.mod('A', records, ['Skyrim.esm']), self.mod('B', records, ['Skyrim.esm'])
        kinds = self.kinds([a, b])
        self.assertEqual(kinds['record-override']['severity'], 'collision')
        self.assertIn('script-on-same-record', kinds)
        self.assertNotIn('vanilla-edit', self.kinds([a, b], False))
        b = self.mod('B', [('QUST', 0x124, None, [])], ['Skyrim.esm'])
        self.assertNotIn('record-override', self.kinds([a, b]))
        self.assertIn('vanilla-edit', self.kinds([a, b], False))

    def test_override_owner_and_master_dependencies(self):
        a = self.mod('A', [('GLOB', 0x800, 'AA_Value', [])])
        b = self.mod('B', [('GLOB', 0x800, 'AA_Value', [])], ['A.esp'])
        result = analyze([a, b])
        kinds = self.kinds([a, b])
        self.assertEqual(kinds['record-override']['severity'], 'dependency')
        self.assertIn('load-order', kinds)
        self.assertEqual(result['load_order']['suggested_order'], ['Skyrim.esm', 'A.esp', 'B.esp'])
        self.assertFalse(result['load_order']['cycles'])
        a = self.mod('A', masters=['B.esp'])
        cyclic = analyze([a, b])
        self.assertEqual(cyclic['load_order']['cycles'], [['A.esp', 'B.esp']])
        self.assertIn('master-cycle', self.kinds([a, b]))

    def test_external_master_and_light_note(self):
        a = self.mod('A', [('GLOB', 0x800, 'AA_Value', [])], esl=True)
        b = self.mod('B', masters=['A.esp', 'Skyrim.esm', 'ccTest.esl', 'Third.esp'])
        self.assertNotIn('master-ordering-note', self.kinds([a, b]))
        kinds = self.kinds([a, b], False)
        self.assertEqual(kinds['master-ordering-note']['mods'], ['B'])
        self.assertIn('external-master', kinds)
        self.assertIn('cc', kinds['external-master']['detail'])
        self.assertIn('esl-budget', kinds)
        self.assertNotIn('esl-range', kinds)
        a = self.mod('A', [('GLOB', 0x700, 'AA_Value', [])], esl=True)
        self.assertEqual(self.kinds([a], False)['esl-range']['severity'], 'collision')

    def test_installed_paths_crc_voice_seq(self):
        a = self.mod('A', files={'Meshes/shared.nif': b'x', 'textures/same.dds': b'common', 'Sound/Voice/Shared.esp/a.fuz': b'x', 'SEQ/shared.seq': b'x'})
        b = self.mod('B', files={'meshes/SHARED.nif': b'y', 'textures/same.dds': b'common', 'sound/voice/shared.esp/b.fuz': b'y', 'seq/SHARED.seq': b'y'})
        result = analyze([a, b])
        paths = [f for f in result['findings'] if f['kind'] == 'file-path']
        self.assertEqual({f['severity'] for f in paths}, {'info', 'collision'})
        self.assertTrue(all(e['label'] == 'verified' for f in paths for e in f['evidence']))
        self.assertTrue({'voice-folder', 'seq'} <= self.kinds([a, b]).keys())
        self.assertNotIn('file-path', self.kinds([a, self.mod('C')]))

    def test_fomod_destination_collision(self):
        config = '<config><requiredInstallFiles><file source="payload.bin" destination="Shared/data.bin"/></requiredInstallFiles></config>'
        a = self.mod('A', files={'payload.bin': b'A'}, config=config)
        b = self.mod('B', files={'Shared/data.bin': b'B'})
        self.assertIn('file-path', self.kinds([a, b]))
        a['fomod']['install_map'][0]['destination'] = 'other/data.bin'
        self.assertNotIn('file-path', self.kinds([a, b]))

    def test_record_references_and_resolution(self):
        a = self.mod('A', [('GLOB', 0x800, 'AA_Value', [])])
        b = self.mod('B', [
            {'type': 'QUST', 'id': 0x1000800, 'scripts': ['BB_Main'], 'props': [('BB_Main', 'Target', 0x800)], 'forced': [0x800]},
            {'type': 'REFR', 'id': 0x1000801, 'base': 0x800}], ['A.esp'])
        kinds = self.kinds([a, b])
        self.assertIn('record-reference', kinds)
        self.assertNotIn('dangling-reference', kinds)
        prop = b['plugins'][0]['script_property_links'][0]
        self.assertEqual(prop['target_type'], 'GLOB')
        self.assertEqual(prop['target_editor_id'], 'AA_Value')
        a['plugins'][0]['records'] = []
        self.assertEqual(self.kinds([a, b])['dangling-reference']['severity'], 'collision')
        c = self.mod('C', [{'type': 'QUST', 'id': 0x800, 'scripts': ['CC_Main'], 'props': [('CC_Main', 'Null', 0)]}])
        self.assertNotIn('record-reference', self.kinds([a, c]))

    def test_soft_references_valid_missing_wrong_type_external_and_drift(self):
        a = self.mod('A', [('GLOB', 0x800, 'AA_Value', [])], pins={'AA_Value': '0x801'})
        b = self.mod('B', psc={'BB_Main': 'Scriptname BB_Main\nGame.GetFormFromFile(0x800, "A.esp") as GlobalVariable'})
        kinds = self.kinds([a, b])
        self.assertIn('soft-reference', kinds)
        self.assertIn('pinned-id-drift', kinds)
        self.assertNotIn('stale-form-id', kinds)
        self.assertIn('pinned-id-mismatch', self.kinds([a, b], False))
        for expression in ('Game.GetFormFromFile(0x900, "A.esp")', 'Game.GetFormFromFile(0x800, "A.esp") as Quest'):
            b = self.mod('B', psc={'BB_Main': 'Scriptname BB_Main\n' + expression})
            stale = self.kinds([a, b])['stale-form-id']
            self.assertEqual({e['label'] for e in stale['evidence']}, {'verified', 'inferred'})
        b = self.mod('B', psc={'BB_Main': 'Scriptname BB_Main\nGame.GetFormFromFile(0x800, "Dawnguard.esm")'})
        self.assertNotIn('soft-reference', self.kinds([a, b]))
        self.assertIn('external-soft-reference', self.kinds([b], False))

    def test_script_call_foreign_write_write_write_events_settings(self):
        a = self.mod('A', [('GLOB', 0x800, 'AA_Value', [])], psc={'AA_Main': '''Scriptname AA_Main
GlobalVariable Property CC_State Auto
GlobalVariable Property AA_Own Auto
CC_State.SetValue(1)
AA_Own.SetValue(2)
BB_Main.Run()
SendModEvent("SharedEvent")
Game.SetGameSettingFloat("fShared", 1)'''})
        b = self.mod('B', psc={'BB_Main': '''Scriptname BB_Main
GlobalVariable Property CC_State Auto
CC_State.Mod(1)
RegisterForModEvent("SharedEvent", "Handler")
Game.SetGameSettingInt("fShared", 2)'''})
        c = self.mod('C', [('GLOB', 0x800, 'CC_State', [])])
        kinds = self.kinds([a, b, c])
        self.assertTrue({'script-call', 'foreign-write', 'write-write', 'modevent', 'engine-setting'} <= kinds.keys())
        self.assertEqual(len(a['source_facts'][0]['writes']), 1)
        d = self.mod('D', psc={'DD_Main': 'Scriptname DD_Main\nSendModEvent("Different")'})
        self.assertEqual(self.kinds([a, d]), {})

    def test_placements_distance_cell_identity_and_forced_refs(self):
        def placement(name, cell, position, forced=0x999):
            return self.mod(name, [
                {'type': 'REFR', 'id': 0x1000800, 'cell': cell, 'position': position},
                {'type': 'QUST', 'id': 0x1000801, 'forced': [forced]}], ['Skyrim.esm'])
        a = placement('A', 0x123, [0, 0, 0])
        b = placement('B', 0x123, [128, 0, 0])
        kinds = self.kinds([a, b])
        self.assertIn('placement-overlap', kinds)
        self.assertEqual(kinds['placement-overlap']['evidence'][0]['label'], 'verified')
        self.assertIn('alias-forced-ref', kinds)
        b = placement('B', 0x123, [129, 0, 0], 0x888)
        self.assertNotIn('placement-overlap', self.kinds([a, b]))
        self.assertIn('same-cell', self.kinds([a, b]))
        self.assertNotIn('alias-forced-ref', self.kinds([a, b]))
        b = placement('B', 0x124, [0, 0, 0], 0x888)
        self.assertEqual(self.kinds([a, b]), {})

    def test_doc_contradictions(self):
        a = self.mod('A', [
            ('GLOB', 0x123, 'Vanilla', []),
            {'type': 'REFR', 'id': 0x2000800, 'cell': 0x123, 'position': [0, 0, 0]}],
            ['Skyrim.esm', 'Other.esp'], readme='ESL. Skyrim.esm is the only master. No edits. Adds no cells.')
        mismatch = self.kinds([a], False)['doc-mismatch']
        self.assertEqual(mismatch['severity'], 'warning')
        self.assertEqual(len(mismatch['detail'].split('; ')), 4)
        good = self.mod('Good', [('GLOB', 0x1000800, 'GG_Value', [])], ['Skyrim.esm'], esl=True,
                        readme='ESL. Skyrim.esm is the only master. No edits. Places nothing in the world.')
        self.assertNotIn('doc-mismatch', self.kinds([good], False))

    def test_documented_pairs(self):
        a = self.mod('A', [('GLOB', 0x800, 'Shared', [])], readme='Compatible with https://github.com/o/B')
        b = self.mod('B', [('GLOB', 0x800, 'Shared', [])])
        self.assertIn('doc-mismatch', self.kinds([a, b]))
        a = self.mod('A', readme='Incompatible with https://github.com/o/B')
        self.assertIn('documented-incompatibility', self.kinds([a, b]))
        a = self.mod('A', readme='Integration details at https://github.com/o/B')
        kinds = self.kinds([a, b])
        self.assertIn('documented-interaction', kinds)
        self.assertNotIn('doc-mismatch', kinds)
        a = self.mod('A', readme='Load A.esp after B.esp.')
        self.assertIn('load-order', self.kinds([a, b]))

    def test_series_claim_requires_link(self):
        b = self.mod('B', [('GLOB', 0x800, 'Shared', [])], readme='Compatible with the rest of the series.')
        a = self.mod('A', [('GLOB', 0x800, 'Shared', [])], readme='Works with all of it.')
        self.assertNotIn('doc-mismatch', self.kinds([a, b]))
        a = self.mod('A', [('GLOB', 0x800, 'Shared', [])], readme='All of it in https://github.com/o/B works well.')
        self.assertIn('doc-mismatch', self.kinds([a, b]))

    def test_dedupe_cap_and_worst_status(self):
        records = [('GLOB', 0x800 + i, 'Shared_%d' % i, []) for i in range(30)]
        a, b = self.mod('A', records), self.mod('B', records)
        result = analyze([a, b])
        collisions = [f for f in result['findings'] if f['kind'] == 'editor-id']
        self.assertEqual(len(collisions), 1)
        self.assertIn('... +5 more', collisions[0]['detail'])
        self.assertEqual(result['pairs'][0]['status'], 'collision')

    def test_cycle_within_mod_and_interleaved_plugins(self):
        a = self.mod('A', masters=['A2.esp'], files={
            'A2.esp': make_plugin(masters=['A.esp'])})
        result = analyze([a])
        self.assertEqual(result['load_order']['cycles'], [['A.esp', 'A2.esp']])
        self.assertIn('master-cycle', self.kinds([a], False))
        a = self.mod('A', files={'A2.esp': make_plugin(masters=['B.esp'])})
        b = self.mod('B', masters=['A.esp'])
        result = analyze([a, b])
        self.assertFalse(result['load_order']['cycles'])
        self.assertEqual(result['load_order']['suggested_order'], ['Skyrim.esm', 'A.esp', 'B.esp', 'A2.esp'])
        self.assertNotIn('master-cycle', self.kinds([a, b]))

    def test_psc_only_doc_collision_does_not_invent_verified_evidence(self):
        a = self.mod('A', psc={'Shared': 'Scriptname Shared'}, readme='Compatible with https://github.com/o/B')
        b = self.mod('B', psc={'Shared': 'Scriptname Shared'})
        mismatch = self.kinds([a, b])['doc-mismatch']
        self.assertEqual({ev['label'] for ev in mismatch['evidence']}, {'inferred'})
