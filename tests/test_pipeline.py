import contextlib
import io
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from helpers import make_plugin, make_zip, make_clone
from modconflict.cli import main
from modconflict.extract import build_mod, InputError
from modconflict.analyze import analyze
from modconflict.render import graph_data, report, mermaid, write


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.clones = self.root / 'clones'
        self.releases = self.root / 'releases'
        self.out = self.root / 'out'
        self.clones.mkdir()
        self.releases.mkdir()

    def package(self, name, members):
        make_zip(self.releases / name / 'v1.zip', members)

    def run_cli(self, args):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            try:
                return main(args)
            except SystemExit as exc:
                return exc.code

    def args(self):
        return ['--clones', str(self.clones), '--releases', str(self.releases), '--out', str(self.out), '--quiet', '--no-timestamp']

    def test_preference_summary_and_full(self):
        clone = make_clone(self.clones / 'A', '# A', {'AA_Main': 'Scriptname AA_Main'}, {'AA_Value': '0x800'})
        (clone / 'A.esp').write_bytes(make_plugin(records=[('QUST', 0x800, 'Wrong', [])]))
        self.package('A', {'A.esp': make_plugin(records=[('GLOB', 0x800, 'AA_Value', ['AA_Main'])]),
                           'fomod/info.xml': '<fomod><Name>Title</Name><Version>2.0</Version></fomod>',
                           'Scripts/AA_Main.pex': b'fake'})
        mod = build_mod('A', clone, self.releases / 'A')
        self.assertEqual(mod['plugins'][0]['records'][0]['type'], 'GLOB')
        self.assertEqual(mod['version'], '2.0')
        self.assertEqual(mod['title'], 'Title')
        self.assertEqual(mod['provenance']['scripts']['source'], 'both')
        self.assertFalse(mod['facts'])
        self.assertEqual(self.run_cli(self.args()), 0)
        data = json.loads((self.out / 'graph.json').read_text())
        self.assertNotIn('generated_at', data)
        self.assertNotIn('records', data['mods'][0]['plugins'][0])
        self.assertEqual(self.run_cli(self.args() + ['--full']), 0)
        data = json.loads((self.out / 'graph.json').read_text())
        self.assertIn('records', data['mods'][0]['plugins'][0])
        for path in self.out.iterdir():
            self.assertNotIn('Scriptname ', path.read_text())

    def test_clone_only_and_unpacked_fallback(self):
        clone = make_clone(self.clones / 'A', None, {'AA_Main': 'Scriptname AA_Main'})
        (clone / 'A.esp').write_bytes(make_plugin(records=[('GLOB', 0x800, 'AA_Value', [])]))
        mod = build_mod('A', clone)
        self.assertEqual(mod['plugins'][0]['label'], 'verified')
        self.assertTrue(any('Missing README' in x for x in mod['gaps']))
        self.assertTrue(any('No release zip' in x for x in mod['gaps']))
        unpacked = self.releases / 'B'
        unpacked.mkdir()
        (unpacked / 'B.esp').write_bytes(make_plugin())
        self.assertEqual(build_mod('B', release=unpacked)['plugins'][0]['source'], 'clone')
        self.assertEqual(self.run_cli(['--clones', str(self.clones), '--out', str(self.out)]), 0)
        self.assertEqual(self.run_cli(['--releases', str(self.releases), '--out', str(self.out)]), 0)

    def test_fail_codes_filters_json_only(self):
        for name in ('A', 'B'):
            self.package(name, {name + '.esp': make_plugin(records=[('GLOB', 0x800, 'Shared', [])])})
        self.assertEqual(self.run_cli(self.args() + ['--fail-on', 'collision']), 1)
        self.assertEqual(self.run_cli(self.args() + ['--fail-on', 'probable']), 1)
        self.assertEqual(self.run_cli(self.args() + ['--fail-on', 'never']), 0)
        args = self.args() + ['--mods', 'A', '--extra', 'B', '--json-only', '--out', str(self.root / 'json')]
        self.assertEqual(self.run_cli(args), 0)
        self.assertEqual([p.name for p in (self.root / 'json').iterdir()], ['graph.json'])
        data = json.loads((self.root / 'json/graph.json').read_text())
        self.assertTrue(data['mods'][1]['extra'])
        self.assertEqual(self.run_cli(self.args() + ['--mods', 'A', '--fail-on', 'collision']), 0)
        for args in ([], ['--clones', str(self.root / 'missing')], self.args() + ['--mods', 'missing'], self.args() + ['--fail-on', 'bad']):
            self.assertEqual(self.run_cli(args), 2)
        empty = self.root / 'empty'
        empty.mkdir()
        self.assertEqual(self.run_cli(['--clones', str(empty)]), 2)

    def test_probable_threshold(self):
        for name in ('A', 'B'):
            make_clone(self.clones / name, 'README', {name: 'Scriptname ' + name + '\nSendModEvent("shared")'})
        self.assertEqual(self.run_cli(self.args() + ['--fail-on', 'collision']), 0)
        self.assertEqual(self.run_cli(self.args() + ['--fail-on', 'probable']), 1)

    def test_zip_safety_and_caps(self):
        for member in ('../escape', '/absolute', r'..\escape', 'C:/escape', 'a/../../escape'):
            self.package('Bad', {member: b'x'})
            with self.assertRaises(InputError):
                build_mod('Bad', release=self.releases / 'Bad')
            self.assertEqual(self.run_cli(self.args()), 2)
        self.package('Bad', {'safe': b'abc', 'other': b'def'})
        with patch('modconflict.extract.MAX_BYTES', 5):
            with self.assertRaises(InputError):
                build_mod('Bad', release=self.releases / 'Bad')
        with patch('modconflict.extract.MAX_ENTRIES', 1):
            with self.assertRaises(InputError):
                build_mod('Bad', release=self.releases / 'Bad')
        (self.releases / 'Bad/v1.zip').write_bytes(b'not a zip')
        self.assertEqual(self.run_cli(self.args()), 2)

    def test_parse_gaps(self):
        clone = make_clone(self.clones / 'A', 'A')
        pins = clone / 'src/plugin/formids.json'
        pins.parent.mkdir(parents=True)
        pins.write_text('invalid')
        self.package('A', {'A.esp': b'bad', 'fomod/info.xml': '<bad', 'fomod/ModuleConfig.xml': '<bad'})
        mod = build_mod('A', clone, self.releases / 'A')
        self.assertGreaterEqual(len(mod['gaps']), 5)
        data = graph_data([mod], analyze([mod]), no_timestamp=True)
        self.assertIn('Unreadable plugin', report(data))

    def test_renderer_determinism_matrix_and_mermaid_ids(self):
        mods = []
        for name in ('A-repo', '2.B'):
            self.package(name, {name + '.esp': make_plugin(records=[('GLOB', 0x800, 'Shared', [])])})
            mods.append(build_mod(name, release=self.releases / name, extra=name == '2.B'))
        data = graph_data(mods, analyze(mods), no_timestamp=True)
        self.assertEqual(data, graph_data(mods, analyze(mods), no_timestamp=True))
        self.assertIn('generated_at', graph_data(mods, analyze(mods)))
        text = report(data)
        matrix = text.split('## Pairwise status matrix')[1].split('Legend:')[0]
        self.assertIn('| A-repo | X | . |', matrix)
        self.assertIn('| 2.B | . | X |', matrix)
        mmd = mermaid(data)
        self.assertIn('==>', mmd)
        self.assertIn('class m0_2_B extra', mmd)
        for node in re.findall(r'^  (\S+)\[', mmd, re.M):
            self.assertRegex(node, r'^[A-Za-z_][A-Za-z0-9_]*$')
        write(self.out, data)
        before = {p.name: p.read_bytes() for p in self.out.iterdir()}
        write(self.out, graph_data(mods, analyze(mods), no_timestamp=True))
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.out.iterdir()})
        self.assertIn('128-unit', text)

    def test_launchers(self):
        repo = Path(__file__).resolve().parents[1]
        for command in ([sys.executable, '-m', 'modconflict', '--help'], [str(repo / 'bin/mod-conflict-graph'), '--help']):
            result = subprocess.run(command, cwd=str(repo), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(b'--full', result.stdout)

    def test_repo_hygiene(self):
        root = Path(__file__).resolve().parents[1]
        forbidden = {'.psc', '.pex', '.esp', '.esm', '.esl', '.bsa', '.zip'}
        offenders = [p.relative_to(root).as_posix() for p in root.rglob('*') if p.is_file() and p.suffix.casefold() in forbidden]
        self.assertEqual(offenders, [])

    def test_release_variants_and_release_documentation(self):
        self.package('A', {'A.esp': make_plugin(records=[('GLOB', 0x800, 'One', [])]),
                           'README.md': 'No edits. Compatible with https://github.com/o/B',
                           'archive.bsa': b'fake'})
        make_zip(self.releases / 'A/optional.zip', {'A.esp': make_plugin(records=[('QUST', 0x900, 'Two', [])])})
        mod = build_mod('A', release=self.releases / 'A')
        self.assertEqual(len(mod['plugins']), 2)
        self.assertEqual({r['editor_id'] for p in mod['plugins'] for r in p['records']}, {'One', 'Two'})
        self.assertIn('B', mod['docs']['compatible_with'])
        self.assertEqual(mod['provenance']['docs']['source'], 'release-zip')
        self.assertFalse(any('Missing README' in gap for gap in mod['gaps']))
        self.assertTrue(any('BSA' in gap for gap in mod['gaps']))
        self.assertTrue(any('Multiple release archives' in gap for gap in mod['gaps']))
        self.assertIn('package-plugin-variants', {f['kind'] for f in mod['facts']})
        self.assertEqual(self.run_cli(self.args()), 0)

    def test_clone_documentation_preferred_and_symlink_skipped(self):
        clone = make_clone(self.clones / 'A', 'No edits.')
        (clone / 'linked.psc').symlink_to(self.root / 'missing.psc')
        self.package('A', {'A.esp': make_plugin(), 'README.md': 'ESL.'})
        mod = build_mod('A', clone, self.releases / 'A')
        self.assertFalse(mod['docs']['esl_claim'])
        self.assertTrue(mod['docs']['no_vanilla_edits_claim'])
        self.assertFalse(mod['source_facts'])

    def test_dependency_arrows_and_probable_rendering(self):
        self.package('A', {'A.esp': make_plugin()})
        self.package('B', {'B.esp': make_plugin(masters=['A.esp'])})
        mods = [build_mod(name, release=self.releases / name) for name in ('A', 'B')]
        data = graph_data(mods, analyze(mods), no_timestamp=True)
        self.assertIn('m1_B -->|', mermaid(data))
        self.assertTrue(any(line.endswith(' m0_A') for line in mermaid(data).splitlines() if '-->' in line))
        for name in ('A', 'B'):
            mods[0 if name == 'A' else 1]['source_facts'] = [{'name': name, 'references': [], 'writes': [],
                'form_calls': [], 'modevents': ['Shared'], 'engine_settings': []}]
        self.assertIn('-.->', mermaid(graph_data(mods, analyze(mods), no_timestamp=True)))
