import argparse
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from codex_desktop import setup


class ProfileTests(unittest.TestCase):
    def test_profiles_are_idempotent_and_preserve_original_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            for shell in ('posix', 'powershell', 'fish'):
                path = Path(tmp) / shell
                original = '# 私の設定\r\nSet-Alias sudo elevate_process\r\n'.encode()
                path.write_bytes(original)
                command = Path(tmp) / "O'Neil Tools" / 'codex-desktop'
                setup.update_profile(path, shell, command)
                installed = path.read_bytes()
                self.assertTrue(installed.startswith(original))
                setup.update_profile(path, shell, command)
                self.assertEqual(path.read_bytes(), installed)
                setup.update_profile(path, shell)
                self.assertEqual(path.read_bytes(), original)
                self.assertEqual(path.with_name(path.name + '.before-codex-desktop').read_bytes(), original)

    def test_unterminated_profile_gets_a_separating_newline(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'profile'
            path.write_bytes(b'echo existing')
            setup.update_profile(path, 'posix', Path('/tools/codex-desktop'))
            self.assertIn(b'echo existing\n# >>>', path.read_bytes())


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        patcher = patch.dict(os.environ, {'CODEX_HOME': str(self.home)})
        patcher.start(); self.addCleanup(patcher.stop)

    def test_conflicting_integration_is_never_overwritten(self):
        with patch.object(setup, 'user_env', return_value='/other/wrapper'), patch.object(setup, 'installed_command', return_value=Path('/new/wrapper')), patch.object(setup, 'set_user_env') as change:
            with self.assertRaisesRegex(RuntimeError, 'Disable that integration first'):
                setup.enable(argparse.Namespace(app=None, cli=None, shells=False))
            change.assert_not_called()
            self.assertFalse(setup.settings_file().exists())

    @unittest.skipIf(os.name == 'nt', 'macOS setup rollback')
    def test_macos_setup_rolls_back_files_and_environment_on_error(self):
        wrapper = setup.root() / 'app'
        agent = self.home / 'agent.plist'
        setup.root().mkdir()
        setup.settings_file().write_bytes(b'{"previousEnv":""}')
        wrapper.write_bytes(b'old wrapper')
        wrapper.chmod(0o700)
        def set_env(value):
            if value:
                raise OSError('simulated launchctl failure')
        with patch.object(setup.sys, 'platform', 'darwin'), patch.object(setup, 'user_env', return_value=''), patch.object(setup, 'set_user_env', side_effect=set_env) as change, patch.object(setup, 'app_cli', return_value='/app/codex'), patch.object(setup, 'installed_command', return_value=Path('/tools/app')), patch.object(setup, 'agent_path', return_value=agent):
            with self.assertRaisesRegex(OSError, 'simulated'):
                setup.enable(argparse.Namespace(app=None, cli=None, shells=False))
            self.assertEqual(change.call_args.args, ('',))
        self.assertEqual(wrapper.read_bytes(), b'old wrapper')
        self.assertEqual(setup.settings_file().read_bytes(), b'{"previousEnv":""}')
        self.assertFalse(agent.exists())
        if os.name != 'nt':
            self.assertEqual(wrapper.stat().st_mode & 0o777, 0o700)

    def test_app_discovery_uses_a_stable_path(self):
        app = self.home / 'Example.app'
        binary = app / 'Contents/Resources/codex'
        binary.parent.mkdir(parents=True)
        binary.touch()
        if os.name != 'nt':
            self.assertEqual(setup.app_cli(str(app)), str(binary.resolve()))
