"""Regression coverage for real-package false positives (fix round 1)."""
from pathlib import Path
import tempfile
import unittest

from helpers import make_clone, make_plugin, make_zip
from modconflict.analyze import analyze, Findings, CAST_TYPES, evidence
from modconflict.extract import build_mod, EXCLUDED_DIRS
from modconflict.render import graph_data, report


class FixTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def put(self, root, path, data):
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data.encode() if isinstance(data, str) else data)

    def mod(self, name, records=(), masters=(), psc=None, pins=None, files=None, esl=False):
        clone = make_clone(self.root / 'clones' / name, 'README', psc, pins)
        release = self.root / 'releases' / name
        members = {name + '.esp': make_plugin(records=records, masters=masters, esl=esl)}
        members.update(files or {})
        make_zip(release / 'v1.zip', members)
        return build_mod(name, clone, release)

    def findings(self, mods, kind):
        return [f for f in analyze(mods)['findings'] if f['kind'] == kind]

    def test_clone_scope_excludes_vendored_plugins_and_scripts(self):
        clone = make_clone(self.root / 'clone', 'README', {'AA_Main': 'Scriptname AA_Main'})
        for directory in EXCLUDED_DIRS:
            for prefix in ('', 'src/papyrus/'):
                self.put(clone, prefix + directory.upper() + '/X_Foo.psc', 'Scriptname X_Foo')
            self.put(clone, directory + '/Foreign.esp', make_plugin())
        self.put(clone, 'tools/modkit/papyrus-stubs/Actor.psc', 'Scriptname Actor')
        self.put(clone, 'masters/scripts/X_Foo.psc', 'Scriptname X_Foo')
        self.put(clone, 'scripts/AA_Top.psc', 'Scriptname AA_Top')
        self.put(clone, 'scripts/nested/NotOwn.psc', 'Scriptname NotOwn')
        self.put(clone, 'other/NotOwn.psc', 'Scriptname NotOwn')
        self.put(clone, 'src/papyrus/nested/AA_Child.psc', 'Scriptname AA_Child')
        self.put(clone, 'Own.esp', make_plugin())
        mod = build_mod('A', clone)
        self.assertEqual(mod['script_names'], ['AA_Child', 'AA_Main', 'AA_Top'])
        self.assertEqual([p['file_name'] for p in mod['plugins']], ['Own.esp'])
        self.assertIn('plugin data from clone (no release zip)', mod['gaps'])
        self.assertFalse(any(f['path'].endswith(('X_Foo.psc', 'Actor.psc', 'Foreign.esp')) for f in mod['files']))

    def test_zip_disables_all_clone_plugins_including_distinct_names(self):
        clone = make_clone(self.root / 'clone', 'README')
        self.put(clone, 'NotShipped.esp', make_plugin(records=[('GLOB', 0x800, 'Wrong', ['Wrong'])]))
        self.put(clone, 'masters/Broken.esp', b'not a plugin')
        release = self.root / 'release'
        make_zip(release / 'v1.zip', {'Shipped.esp': make_plugin()})
        mod = build_mod('A', clone, release)
        self.assertEqual([p['file_name'] for p in mod['plugins']], ['Shipped.esp'])
        self.assertEqual(mod['script_names'], [])
        self.assertFalse(any('clone (no release zip)' in g or 'Unreadable' in g for g in mod['gaps']))

    def test_zip_script_roots_and_report_count(self):
        files = {
            'Scripts/AA_Built.pex': b'pex',
            'option/sOuRcE/sCrIpTs/AA_Source.psc': 'Scriptname AA_Source',
            'Source/Scripts/SourceOnly.psc': 'Scriptname SourceOnly',
            'tools/Scripts/Actor.pex': b'pex',
            'masters/Source/Scripts/X_Foo.psc': 'Scriptname X_Foo',
            'stubs/Scripts/Quest.pex': b'pex',
            'unrelated/Stray.pex': b'pex',
            'Scripts/Stray.psc': 'Scriptname Stray',
            'src/papyrus/NotPackaged.psc': 'Scriptname NotPackaged',
        }
        a = self.mod('A', records=[('QUST', 0x800, 'AA_Quest', ['AA_Vmad'])], files=files,
                     psc={'AA_Clone': 'Scriptname AA_Clone'})
        self.assertEqual(a['script_names'], ['AA_Built', 'AA_Clone', 'AA_Source', 'AA_Vmad', 'SourceOnly'])
        self.assertEqual({s['name'] for s in a['source_facts']}, {'AA_Clone', 'AA_Source', 'SourceOnly'})
        text = report(graph_data([a], analyze([a]), no_timestamp=True))
        row = next(line for line in text.splitlines() if line.startswith('| A | A.esp'))
        self.assertEqual(row.split('|')[7].strip(), '5')
        b = self.mod('B', files={'Source/Scripts/SourceOnly.psc': 'Scriptname SourceOnly'})
        self.assertTrue(self.findings([a, b], 'script-name'))
        # Metadata still lists every shipped file even when it is not a script fact.
        self.assertTrue(any(f['path'] == 'tools/Scripts/Actor.pex' for f in a['files']))

    def test_vendored_scripts_cannot_create_interactions(self):
        a = self.mod('A', psc={'AA_Main': '''Scriptname AA_Main
GlobalVariable Property XX_State Auto
XX_State.SetValue(1)
X_Foo.Run()
Actor.Run()'''})
        clone = self.root / 'clones/B'
        make_clone(clone, 'README', {'BB_Main': 'Scriptname BB_Main'})
        self.put(clone, 'masters/scripts/X_Foo.psc', 'Scriptname X_Foo\nGlobalVariable Property XX_State Auto\nXX_State.Mod(1)')
        self.put(clone, 'tools/modkit/papyrus-stubs/Actor.psc', 'Scriptname Actor')
        b = build_mod('B', clone)
        kinds = {f['kind'] for f in analyze([a, b])['findings'] if len(f['mods']) == 2}
        self.assertFalse(kinds & {'script-call', 'foreign-write', 'write-write', 'script-name'})

    def test_write_write_requires_third_analysed_owner(self):
        text = 'GlobalVariable Property CC_State Auto\nCC_State.SetValue(1)'
        a = self.mod('A', psc={'AA_Main': 'Scriptname AA_Main\n' + text})
        b = self.mod('B', psc={'BB_Main': 'Scriptname BB_Main\n' + text})
        self.assertFalse(self.findings([a, b], 'write-write'))
        c = self.mod('C', records=[('GLOB', 0x800, 'CC_State', [])])
        self.assertEqual(self.findings([a, b, c], 'write-write')[0]['mods'], ['A', 'B'])
        self.assertEqual(len(self.findings([a, b, c], 'foreign-write')), 2)

    def test_player_forced_refs_ignored_and_counted_other_refs_retained(self):
        def quest(name, refs):
            return self.mod(name, records=[{'type': 'QUST', 'id': 0x1000800, 'forced': refs}], masters=['Skyrim.esm'])
        a, b = quest('A', [0x14, 0x14]), quest('B', [0x14])
        self.assertEqual(a['ignored_forced_refs'], 2)
        self.assertEqual(b['ignored_forced_refs'], 1)
        self.assertFalse(self.findings([a, b], 'alias-forced-ref'))
        a, b = quest('A', [0x14, 0x123]), quest('B', [0x14, 0x123])
        f = self.findings([a, b], 'alias-forced-ref')[0]
        self.assertEqual(f['severity'], 'probable')
        self.assertIn('000123', f['detail'])
        self.assertNotIn('000014', f['detail'])
        self.assertEqual(graph_data([a], analyze([a]))['mods'][0]['ignored_forced_refs'], 1)

    def test_soft_refs_use_new_record_low_bits_with_specific_evidence(self):
        a = self.mod('A', records=[('GLOB', 0x1000800, 'AA_Value', []),
                                   ('QUST', 0x801, 'VanillaOverride', [])], masters=['Skyrim.esm'], esl=True)
        for local, cast, stale in ((0x800, 'GlobalVariable', False), (0x900, 'Quest', True),
                                    (0x800, 'Quest', True), (0x801, 'Quest', True),
                                    (0x1000800, 'GlobalVariable', False)):
            with self.subTest(local=local, cast=cast):
                b = self.mod('B', psc={'BB_Main': 'Scriptname BB_Main\nGame.GetFormFromFile(0x%X, "A.esp") as %s' % (local, cast)})
                fs = self.findings([a, b], 'stale-form-id')
                self.assertEqual(bool(fs), stale)
                soft = self.findings([a, b], 'soft-reference')[0]
                self.assertEqual({e['label'] for e in soft['evidence']}, {'verified', 'inferred'})
                if local & 0xffffff == 0x800:
                    self.assertTrue(any('GLOB AA_Value exists in A.esp' in e['detail'] for e in soft['evidence']))
                if stale:
                    self.assertEqual(fs[0]['severity'], 'collision')
                    self.assertEqual({e['label'] for e in fs[0]['evidence']}, {'verified', 'inferred'})

    def test_all_required_soft_reference_casts(self):
        for cast in ('GlobalVariable', 'Quest', 'Spell', 'Message', 'MiscObject', 'Armor',
                     'Weapon', 'Keyword', 'Faction', 'Perk', 'FormList'):
            with self.subTest(cast=cast):
                a = self.mod('A', records=[(CAST_TYPES[cast.casefold()], 0x800, 'AA_Target', [])])
                b = self.mod('B', psc={'BB_Main': 'Scriptname BB_Main\nGame.GetFormFromFile(0x800, "A.esp") as ' + cast})
                self.assertFalse(self.findings([a, b], 'stale-form-id'))
                a['plugins'][0]['records'][0]['type'] = 'STAT'
                self.assertTrue(self.findings([a, b], 'stale-form-id'))

    def test_pinned_name_at_referenced_id_drift(self):
        b = self.mod('B', psc={'BB_Main': 'Scriptname BB_Main\nGame.GetFormFromFile(0x800, "A.esp") as Quest'})
        a = self.mod('A', records=[('QUST', 0x800, 'AA_NewName', [])], pins={'AA_OldName': '0x800'})
        drift = self.findings([a, b], 'pinned-id-drift')[0]
        self.assertEqual(drift['severity'], 'warning')
        self.assertEqual({e['label'] for e in drift['evidence']}, {'verified', 'inferred'})
        a['pinned_ids'] = {'AA_NewName': 0x800}
        self.assertFalse(self.findings([a, b], 'pinned-id-drift'))

    def test_master_notes_aggregate_per_mod_only_esl_esps(self):
        a = self.mod('A', esl=True)
        b = self.mod('B', esl=True)
        c = self.mod('C')
        d = self.mod('D', masters=['A.esp', 'B.esp', 'C.esp'], files={
            'D2.esp': make_plugin(masters=['A.esp'])})
        notes = self.findings([a, b, c, d], 'master-ordering-note')
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0]['mods'], ['D'])
        self.assertEqual(notes[0]['detail'], 'A.esp; B.esp')
        self.assertEqual(notes[0]['severity'], 'info')
        e = self.mod('E', files={'Light.esp': make_plugin(esl=True),
                                 'Dependent.esp': make_plugin(masters=['Light.esp'])})
        note = self.findings([e], 'master-ordering-note')[0]
        self.assertEqual(note['mods'], ['E'])
        self.assertEqual(note['detail'], 'Light.esp')

    def test_detail_caps_for_many_and_long_items_and_extra_evidence(self):
        findings = Findings()
        for i in range(100):
            findings.add('test', 'collision', ['A', 'B'], 'title', ['name_%03d_' % i + 'x' * 100],
                         extra=[evidence('inferred', 'source', 'y' * 1000)])
        findings.add('long', 'info', ['A'], 'title', ['z' * 1000])
        for finding in findings.finish():
            for detail in [finding['detail']] + [e['detail'] for e in finding['evidence']]:
                self.assertLessEqual(len(detail), 300)
                self.assertRegex(detail, r'\.\.\. \+\d+ more')
