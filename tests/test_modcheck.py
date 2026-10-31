import contextlib
import io
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from helpers import make_plugin, make_zip
from modconflict.cli import main
from modconflict import modcheck
from modconflict.plugin import parse_header, parse_plugin

INFO = '<fomod><Name>Alpha</Name><Version>%s</Version></fomod>'
CONFIG = '''<config><moduleName>Alpha</moduleName>
<moduleDependencies operator="And"><fileDependency file="%s" state="Active"/></moduleDependencies>
<requiredInstallFiles><file source="Alpha.esp"/></requiredInstallFiles></config>'''
OVERRIDE = [('QUST', 0x00000D62, 'SharedQuest', [])]


def write(root, files):
    root = Path(root)
    for name, value in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(value, bytes):
            path.write_bytes(value)
        else:
            path.write_text(value)
    return root


class ModcheckTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.mod = self.root / 'Alpha'

    def make(self, masters=('Skyrim.esm',), files=None, records=(), version='1.0.0', changelog='1.0.0'):
        base = {'Alpha.esp': make_plugin(masters=masters, records=records)}
        if version is not None:
            base['fomod/info.xml'] = INFO % version
        if changelog is not None:
            base['CHANGELOG.md'] = '# Changelog\n\n## [%s] - 2026-10-01\n- Initial release.\n' % changelog
        base.update(files or {})
        return write(self.mod, base)

    def check(self, *args, config=None):
        """Run the CLI subcommand; return (exit code, report text)."""
        if config is not None:
            (self.mod / 'modcheck.toml').write_text(config)
        out = self.root / 'report.md'
        stderr = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(stderr):
            try:
                code = main(['modcheck', str(self.mod), '--no-git', '--out', str(out), '--quiet'] + list(args))
            except SystemExit as exc:
                code = exc.code
        self.stderr = stderr.getvalue()
        return code, out.read_text() if out.exists() else ''

    def kinds(self, level, *args, **kwargs):
        config = modcheck.load_config(kwargs.pop('config_path', None))
        result = modcheck.run(self.mod, config, use_git=False, **kwargs)
        return sorted(f['kind'] for f in result['findings'] if f['level'] == level)

    # Headers ---------------------------------------------------------------

    def test_header_parser_reads_masters_author_description(self):
        data = make_plugin(masters=['Skyrim.esm', 'SkyUI_SE.esp'], esl=True, author='Someone',
                           description='Version: 1.2.3', records=[('GLOB', 0x800, 'A_G', [])])
        header = parse_header(data, 'Alpha.esp')
        self.assertEqual(header.masters, ['Skyrim.esm', 'SkyUI_SE.esp'])
        self.assertEqual((header.author, header.description), ('Someone', 'Version: 1.2.3'))
        self.assertTrue(header.is_esl)
        self.assertEqual(header.records, [])
        full = parse_plugin(data, 'Alpha.esp')
        self.assertEqual(full.masters, header.masters)
        self.assertEqual(len(full.records), 1)

    # Masters ---------------------------------------------------------------

    def test_undeclared_master_blocks(self):
        self.make(masters=['Skyrim.esm', 'SkyUI_SE.esp'])
        code, text = self.check()
        self.assertEqual(code, 1)
        self.assertIn('undeclared-master', text)
        self.assertIn('SkyUI_SE.esp', text)
        self.assertIn('**FAIL**', text)

    def test_masters_declared_by_each_source(self):
        cases = {
            'fomod': {'fomod/ModuleConfig.xml': CONFIG % 'SkyUI_SE.esp'},
            'readme': {'README.md': 'Requires [SkyUI](https://example.invalid) (`SkyUI_SE.esp`).'},
            'info': {'fomod/info.xml': '<fomod><Version>1.0.0</Version><Description>Needs SkyUI_SE.esp</Description></fomod>'},
            'requirements': {'requirements.list': '# hard masters\nSkyUI_SE.esp\n',
                             'modcheck.toml': '[masters]\nrequirements_file = "requirements.list"\n'},
            'config': {'modcheck.toml': '[masters]\ndeclared = ["skyui_se.esp"]\n'},
        }
        for label, files in cases.items():
            with self.subTest(label):
                shutil.rmtree(self.mod, ignore_errors=True)
                self.make(masters=['Skyrim.esm', 'SkyUI_SE.esp'], files=files)
                code, text = self.check()
                self.assertEqual(code, 0, text)
                self.assertNotIn('undeclared-master', text)

    def test_own_plugins_implicit_masters_and_excluded_dirs(self):
        self.make(masters=['Skyrim.esm', 'Update.esm'], files={
            'Alpha - Patch.esp': make_plugin(masters=['Skyrim.esm', 'Alpha.esp']),
            'tests/fixtures/Fake.esp': make_plugin(masters=['Nothing.esm']),
        })
        self.assertEqual(self.kinds('blocker'), [])
        (self.mod / 'modcheck.toml').write_text('[masters]\nimplicit = ["Skyrim.esm"]\n')
        self.assertEqual(self.kinds('blocker', config_path=self.mod / 'modcheck.toml'), ['undeclared-master'])

    def test_unreadable_plugin_blocks(self):
        self.make(files={'Broken.esp': b'not a plugin'})
        self.assertIn('unreadable-plugin', self.kinds('blocker'))

    def test_release_package_plugins_take_precedence(self):
        self.make(masters=['Skyrim.esm'])
        release = self.root / 'release'
        make_zip(release / 'Alpha-1.0.0.zip', {'Alpha.esp': make_plugin(masters=['Skyrim.esm', 'Missing.esm']),
                                               'fomod/info.xml': INFO % '1.0.0'})
        code, text = self.check('--release', str(release))
        self.assertEqual(code, 1)
        self.assertIn('Missing.esm', text)

    # Conflicts -------------------------------------------------------------

    def peer(self, name='Beta'):
        return write(self.root / name, {name + '.esp': make_plugin(masters=['Skyrim.esm'], records=OVERRIDE)})

    def test_record_conflict_with_peer_blocks(self):
        self.make(records=OVERRIDE)
        beta = self.peer()
        code, text = self.check('--against', str(beta))
        self.assertEqual(code, 1)
        self.assertIn('record-override', text)
        self.assertIn('Suggested load order', text)
        self.assertEqual(self.check()[0], 0)

    def test_conflict_threshold_and_peers_from_config(self):
        self.make(records=OVERRIDE)
        self.peer()
        config = '[conflicts]\nagainst = ["../Beta"]\nfail_on = "never"\n'
        code, text = self.check(config=config)
        self.assertEqual(code, 0)
        self.assertIn('record-override', text.split('## Warnings')[1])
        code, _text = self.check('--fail-on', 'warning')
        self.assertEqual(code, 1)

    def test_ignore_rules(self):
        self.make(records=OVERRIDE)
        beta = self.peer()
        config = '''
[[ignore]]
check = "conflicts"
kind = "record-override"
subject = "Beta"
reason = "Beta ships a compatibility patch"

[[ignore]]
kind = "esl-*"
reason = "stale rule"
'''
        code, text = self.check('--against', str(beta), config=config)
        self.assertEqual(code, 0, text)
        self.assertIn('Ignored by modcheck.toml', text)
        self.assertIn('Beta ships a compatibility patch', text)
        self.assertIn('unused-ignore', text)

    def test_duplicate_peer_name_is_an_error(self):
        self.make()
        other = write(self.root / 'x' / 'Alpha', {'A.esp': make_plugin()})
        self.assertEqual(self.check('--against', str(other))[0], 2)

    # Versions --------------------------------------------------------------

    def test_version_key(self):
        key = modcheck.version_key
        self.assertEqual(key('v1.2'), key('1.2.0'))
        self.assertLess(key('1.2.0-beta.2'), key('1.2.0'))
        self.assertLess(key('1.2.0-beta.2'), key('1.2.0-beta.10'))
        self.assertLess(key('1.9'), key('1.10'))
        self.assertIsNone(key('final'))

    def test_changelog_parsing(self):
        text = '# Changelog\n\n## [Unreleased]\n- wip\n\n## v2.1.0 (2026-01-01)\n\n## 2.0.0\n'
        self.assertEqual(modcheck.changelog_version(text), ('2.1.0', True))
        self.assertEqual(modcheck.changelog_version('## 1.0 - first\n'), ('1.0', False))
        self.assertEqual(modcheck.changelog_version('# Notes\n'), (None, False))

    def test_version_mismatch_blocks(self):
        self.make(version='1.1.0', changelog='1.0.0')
        code, text = self.check()
        self.assertEqual(code, 1)
        self.assertIn('version-mismatch', text)
        self.assertIn('## Versions', text)

    def test_plugin_description_version(self):
        self.make(files={'Alpha.esp': make_plugin(masters=['Skyrim.esm'], description='Alpha. Version: 1.0.1')})
        self.assertEqual(self.kinds('blocker'), ['version-mismatch'])
        for text in ('Requires SKSE v2.2.3', 'Alpha by me'):
            write(self.mod, {'Alpha.esp': make_plugin(masters=['Skyrim.esm'], description=text)})
            self.assertEqual(self.kinds('blocker'), [], text)
        write(self.mod, {'Alpha.esp': make_plugin(masters=['Skyrim.esm'], description='1.0.1 - Alpha')})
        self.assertEqual(self.kinds('blocker'), ['version-mismatch'])
        (self.mod / 'modcheck.toml').write_text('[version]\nplugin_description = false\n')
        self.assertEqual(self.kinds('blocker', config_path=self.mod / 'modcheck.toml'), [])

    def test_changelog_warnings(self):
        self.make(changelog=None, files={'CHANGELOG.md': '## [Unreleased]\n- wip\n\n## 1.0.0\n'})
        self.assertEqual(self.kinds('warning'), ['changelog-unreleased'])
        (self.mod / 'CHANGELOG.md').unlink()
        self.assertEqual(self.kinds('warning'), ['missing-changelog'])

    def test_explicit_tag(self):
        self.make()
        self.assertEqual(self.check('--tag', 'v1.0.0')[0], 0)
        code, text = self.check('--tag', 'v1.0.1')
        self.assertEqual(code, 1)
        self.assertIn('tag-mismatch', text)

    @unittest.skipUnless(shutil.which('git'), 'git is not installed')
    def test_git_tags(self):
        self.make()
        env = dict(os.environ, GIT_AUTHOR_NAME='t', GIT_AUTHOR_EMAIL='t@example.invalid',
                   GIT_COMMITTER_NAME='t', GIT_COMMITTER_EMAIL='t@example.invalid', GIT_CONFIG_GLOBAL=os.devnull)

        def git(*args):
            subprocess.run(['git', '-C', str(self.mod)] + list(args), check=True, env=env, capture_output=True)

        def kinds():
            config = modcheck.load_config(None)
            result = modcheck.run(self.mod, config)
            return sorted(f['kind'] for f in result['findings'] if f['level'] in ('blocker', 'warning'))

        git('init', '-q')
        git('add', '.')
        git('commit', '-qm', 'one')
        git('tag', 'v1.0.0')
        self.assertEqual(kinds(), [])
        (self.mod / 'notes.txt').write_text('x')
        git('add', '.')
        git('commit', '-qm', 'two')
        self.assertEqual(kinds(), ['version-not-bumped'])
        git('tag', 'v2.0.0', 'HEAD~1')
        git('tag', '-d', 'v1.0.0')
        self.assertEqual(kinds(), ['version-behind-tag'])
        git('tag', 'v1.0.1')
        self.assertEqual(kinds(), ['tag-mismatch'])

    def test_git_unavailable_is_info(self):
        self.make()
        result = modcheck.run(self.mod, modcheck.load_config(None))
        self.assertIn('git-unavailable', [f['kind'] for f in result['findings'] if f['level'] == 'info'])
        self.assertFalse(result['failed'])

    # Config and CLI --------------------------------------------------------

    def test_config_errors_exit_2(self):
        self.make()
        bad = {'unknown': 'colour = "red"\n',
               'type': '[masters]\ndeclared = "SkyUI_SE.esp"\n',
               'no-reason': '[[ignore]]\nkind = "x"\n',
               'no-pattern': '[[ignore]]\nreason = "x"\n',
               'check': '[[ignore]]\ncheck = "nope"\nreason = "x"\n',
               'syntax': '[masters\n'}
        for label, text in bad.items():
            with self.subTest(label):
                self.assertEqual(self.check(config=text)[0], 2)
                self.assertIn('modcheck:', self.stderr)

    def test_summary_append_stdout_and_name(self):
        self.make()
        (self.mod / 'modcheck.toml').write_text('name = "Alpha Mod"\n')
        summary = self.root / 'summary.md'
        summary.write_text('previous\n')
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = main(['modcheck', str(self.mod), '--no-git', '--summary', str(summary)])
        self.assertEqual(code, 0)
        self.assertTrue(stdout.getvalue().startswith('# modcheck: Alpha Mod'))
        self.assertTrue(summary.read_text().startswith('previous\n# modcheck: Alpha Mod'))

    def test_usage_errors(self):
        self.assertEqual(self.check()[0], 2)
        self.make()
        self.assertEqual(self.check('--release', str(self.root / 'missing'))[0], 2)
        self.assertEqual(self.check('--config', str(self.root / 'missing.toml'))[0], 2)

    def test_main_cli_unchanged(self):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                main([])
        self.assertEqual(raised.exception.code, 2)

    def test_action_definition(self):
        text = (Path(__file__).resolve().parents[1] / 'action.yml').read_text()
        self.assertIn("using: 'composite'", text)
        script = text.split('run: |\n', 1)[1]
        self.assertNotIn('${{', script)
        self.assertIn('GITHUB_STEP_SUMMARY', script)

    @unittest.skipUnless(shutil.which('bash'), 'bash is not installed')
    def test_action_script_runs(self):
        repo = Path(__file__).resolve().parents[1]
        text = (repo / 'action.yml').read_text()
        script = '\n'.join(line[8:] for line in text.split('run: |\n', 1)[1].splitlines())
        self.make(masters=['Skyrim.esm', 'SkyUI_SE.esp'], records=OVERRIDE)
        self.peer()
        summary, output = self.root / 'summary.md', self.root / 'output.txt'
        env = dict(os.environ, GITHUB_ACTION_PATH=str(repo), GITHUB_STEP_SUMMARY=str(summary),
                   GITHUB_OUTPUT=str(output), RUNNER_TEMP=str(self.root), MODCHECK_PATH=str(self.mod),
                   MODCHECK_CONFIG='', MODCHECK_AGAINST='\n%s\r\n  \n' % (self.root / 'Beta'),
                   MODCHECK_RELEASE='', MODCHECK_TAG='', MODCHECK_FAIL_ON='blocker', MODCHECK_GIT='false',
                   MODCHECK_PYTHON=sys.executable, MODCHECK_REPORT='', MODCHECK_REF_TYPE='tag',
                   MODCHECK_REF_NAME='v1.0.0')
        done = subprocess.run(['bash', '-c', script], env=env, capture_output=True, text=True)
        self.assertEqual(done.returncode, 1, done.stderr)
        report = (self.root / 'modcheck-report.md').read_text()
        self.assertEqual(summary.read_text(), report)
        for needle in ('undeclared-master', 'record-override', 'release tag (--tag)'):
            self.assertIn(needle, report)
        self.assertIn('exit-code=1', output.read_text())

if __name__ == '__main__':
    unittest.main()
