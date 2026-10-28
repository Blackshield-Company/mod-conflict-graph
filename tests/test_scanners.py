import unittest
from modconflict import docs, papyrus, fomod

CONFIG = '''<config><moduleName>Test</moduleName><requiredInstallFiles>
<folder source="Core" destination=""/></requiredInstallFiles><installSteps><installStep name="Choose">
<optionalFileGroups><group name="Group" type="SelectAny"><plugins><plugin name="Option">
<description>Description</description><files><file source="Optional/a.txt" destination="Scripts/A.txt"/>
<file source="missing.txt" destination="missing.txt"/></files><typeDescriptor><type name="Optional"/></typeDescriptor>
</plugin></plugins></group></optionalFileGroups></installStep></installSteps>
<conditionalFileInstalls><patterns><pattern><files><file source="cond.txt" destination="cond.txt"/></files></pattern></patterns></conditionalFileInstalls></config>'''


class ScannerTests(unittest.TestCase):
    def test_papyrus(self):
        text = '''Scriptname AA_Main extends Quest
GlobalVariable Property BB_State Auto
Quest Property AA_Own Auto
Game.GetFormFromFile(0x800, "B.esp") as GlobalVariable
BB_State.SetValue(1)
AA_Own.Start()
BB_Script.Run()
SendModEvent("Shared")
RegisterForModEvent("Shared", "OnShared")
Game.SetGameSettingFloat("fSetting", 1)
Utility.SetINIBool("bThing:General", true)
; Game.GetFormFromFile(0x999, "Hidden.esp")
;/ BB_Hidden.Run() /;
'''
        result = papyrus.scan(text)
        self.assertEqual(result['name'], 'AA_Main')
        self.assertEqual(result['extends'], 'Quest')
        self.assertEqual(len(result['properties']), 2)
        self.assertEqual(result['form_calls'], [{'plugin': 'B.esp', 'local_id': 0x800, 'cast': 'GlobalVariable'}])
        self.assertEqual(result['modevents'], ['Shared'])
        self.assertEqual(len(result['engine_settings']), 2)
        self.assertEqual(len(result['writes']), 2)
        self.assertIn('BB_Script', result['references'])
        self.assertNotIn('BB_Hidden', result['references'])
        self.assertEqual(papyrus.own_prefix(['BB_One', 'AA_One', 'BB_Two']), 'bb_')
        self.assertEqual(papyrus.own_prefix([], ['AA_Main']), 'aa_')
        self.assertEqual(papyrus.scan('; empty')['form_calls'], [])
        self.assertEqual(papyrus.script_name(r'Scripts\AA_Main.pex'), 'AA_Main')

    def test_docs(self):
        text = '''ESL flagged. Skyrim.esm is the only master. No vanilla records edited.
Places nothing in the world.
Fully compatible with https://github.com/o/B and the rest of the series.
Incompatible with https://github.com/o/C. Do not use with D.esp.
Load A.esp after B.esp. Requires C.esp.
'''
        result = docs.extract(text)
        for field in ('esl_claim', 'only_master_claim', 'no_vanilla_edits_claim', 'places_nothing_claim', 'series_wide'):
            self.assertTrue(result[field], field)
        self.assertIn('B', result['compatible_with'])
        self.assertNotIn('C', result['compatible_with'])
        self.assertIn('C', result['incompatible_with'])
        self.assertIn('D.esp', result['incompatible_with'])
        self.assertIn('B.esp', result['load_after'])
        self.assertIn('C.esp', result['requires_text'])
        empty = docs.extract('An ordinary mod.')
        self.assertFalse(empty['esl_claim'])
        self.assertEqual(empty['compatible_with'], [])
        for phrase in ('edits no vanilla records', 'zero overrides', 'No edits'):
            self.assertTrue(docs.extract(phrase)['no_vanilla_edits_claim'])

    def test_fomod(self):
        result = fomod.parse(CONFIG, ['Core/A.esp', 'Core/Scripts/X.pex', 'Optional/a.txt', 'cond.txt', 'unused.txt', 'fomod/info.xml'])
        self.assertEqual(result['installed_files']['required only'], ['a.esp', 'scripts/x.pex'])
        self.assertIn('scripts/a.txt', result['installed_files']['all options'])
        self.assertIn('cond.txt', result['installed_files']['all options'])
        self.assertEqual(result['missing_sources'], ['missing.txt'])
        self.assertEqual(result['never_installed'], ['unused.txt'])
        self.assertEqual(result['conditional_installs'], 1)
        self.assertEqual(len(result['steps'][0]['groups'][0]['options']), 1)
        self.assertEqual(fomod.info('<fomod><Name>A</Name><Version>1</Version></fomod>'), {'title': 'A', 'version': '1'})
        empty = fomod.parse('<config/>', [])
        self.assertEqual(empty['installed_files']['all options'], [])
        self.assertFalse(empty['missing_sources'])

    def test_fomod_root_folder_and_required_option(self):
        xml = '''<config><requiredInstallFiles><folder source="." destination="Data"/></requiredInstallFiles>
<installSteps><installStep><optionalFileGroups><group><plugins><plugin name="R"><files>
<file source="a.bin" destination="Required/a.bin"/></files><typeDescriptor><type name="Required"/>
</typeDescriptor></plugin></plugins></group></optionalFileGroups></installStep></installSteps></config>'''
        result = fomod.parse(xml, ['a.bin', '.hidden'])
        self.assertEqual(result['installed_files']['required only'], ['data/.hidden', 'data/a.bin', 'required/a.bin'])
        self.assertFalse(result['missing_sources'])

    def test_docs_after_only_and_papyrus_strings(self):
        self.assertEqual(docs.extract('Load A.esp after B.esp.')['load_after'], ['B.esp'])
        result = papyrus.scan('Scriptname AA_S\nSendModEvent("Has;Semicolon") ; ignored')
        self.assertEqual(result['modevents'], ['Has;Semicolon'])

    def test_quoted_plugin_names_with_spaces(self):
        result = docs.extract('Load `My Mod.esp` after `Other Mod.esp`. Do not use with "Third Mod.esm".')
        self.assertEqual(result['load_after'], ['Other Mod.esp'])
        self.assertIn('My Mod.esp', result['mentions'])
        self.assertIn('Third Mod.esm', result['incompatible_with'])
