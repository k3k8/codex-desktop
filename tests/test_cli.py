"""Run with Python; no model calls, real tokens, or app-server changes."""
import importlib.machinery
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.dont_write_bytecode = True

from codex_desktop import cli as app


class ConnectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.records = self.home / 'codama/desktop/instances'
        self.records.mkdir(parents=True)
        self.cli = self.home / 'codex.exe'
        self.cli.touch()

    def record(self, name='1', **changes):
        token = self.records / (name + '.token')
        token.write_text('synthetic-test-credential')
        record = dict(kind='desktop', pid=123, serverPid=456,
                      url='ws://127.0.0.1:12345', token=str(token), cli=str(self.cli))
        record.update(changes)
        (self.records / (name + '.json')).write_text(json.dumps(record))

    def test_windows_skips_stale_records_and_uses_server_executable(self):
        self.record()
        self.record('2', pid=999)
        (self.records / 'broken.json').write_text('{')
        def image(pid):
            if pid == 999:
                raise OSError('dead')
            return self.cli
        with patch.object(app, 'WINDOWS', True), patch.object(app, 'process_image', image):
            result = app.desktop_connection(self.home)
        self.assertEqual(result['cli'], str(self.cli))
        self.assertEqual(result['address'], 'ws://127.0.0.1:12345')

    def test_windows_rejects_external_urls_token_paths_and_wrong_process(self):
        for changes in (dict(url='ws://10.0.0.1:12345'), dict(url='ws://127.0.0.1:99999'),
                        dict(token=str(self.home / 'elsewhere.token')), dict(cli=str(self.home / 'wrong.exe'))):
            self.record(**changes)
            with patch.object(app, 'WINDOWS', True), patch.object(app, 'process_image', return_value=self.cli), patch('sys.stderr', io.StringIO()):
                with self.assertRaises(SystemExit):
                    app.desktop_connection(self.home)

    def test_windows_rejects_ambiguous_servers(self):
        self.record()
        self.record('2', url='ws://127.0.0.1:12346')
        with patch.object(app, 'WINDOWS', True), patch.object(app, 'process_image', return_value=self.cli), patch('sys.stderr', io.StringIO()):
            with self.assertRaises(SystemExit):
                app.desktop_connection(self.home)

    def test_credentials_are_passed_only_in_child_environment(self):
        self.record()
        with patch.object(app, 'WINDOWS', True), patch.object(app, 'process_image', return_value=self.cli), patch.object(app, 'find_cli', return_value=str(self.cli)), patch.dict(os.environ, {'CODEX_HOME': str(self.home)}), patch.object(app, 'run_cli') as execute, patch.object(sys, 'argv', ['codex-desktop', 'resume', '会話 ID']):
            app.main()
            _, argv, env = execute.call_args.args
            self.assertEqual(argv, ['--remote', 'ws://127.0.0.1:12345', '--remote-auth-token-env', 'CODEX_DESKTOP_AUTH_TOKEN', 'resume', '会話 ID'])
            self.assertEqual(env['CODEX_DESKTOP_AUTH_TOKEN'], 'synthetic-test-credential')
            self.assertNotIn('synthetic-test-credential', argv)
            self.assertNotEqual(os.environ.get('CODEX_DESKTOP_AUTH_TOKEN'), 'synthetic-test-credential')

    @unittest.skipIf(os.name == 'nt', 'Unix socket permissions')
    def test_unix_socket_and_permissions(self):
        address = self.home / 'app.sock'
        with socket.socket(socket.AF_UNIX) as listener:
            listener.bind(str(address))
            self.record(pid=os.getpid(), serverPid=os.getpid(), socket=str(address))
            with patch.object(app, 'WINDOWS', False):
                self.assertEqual(app.desktop_address(self.home), 'unix://' + str(address))
                self.home.chmod(0o755)
                with patch('sys.stderr', io.StringIO()), self.assertRaises(SystemExit):
                    app.desktop_address(self.home)

    def test_management_does_not_require_a_running_app(self):
        with patch.object(app, 'find_cli', return_value=str(self.cli)), patch.object(app, 'run_cli') as execute, patch.object(sys, 'argv', ['codex-desktop', '-c', 'x=true', 'app-server', 'daemon', 'version']):
            app.main()
            self.assertEqual(execute.call_args.args, (str(self.cli), ['-c', 'x=true', 'app-server', 'daemon', 'version']))

    def test_bypass_hint_fits_the_shell(self):
        for argv in (['exec', 'hi'], ['--remote', 'ws://127.0.0.1:1']):
            for windows, expected in ((False, 'command codex'), (True, "& '" + str(self.cli) + "'")):
                with patch.object(app, 'WINDOWS', windows), patch.object(app, 'locate_cli', return_value=str(self.cli)), patch.object(sys, 'argv', ['codex-desktop', *argv]), patch('sys.stderr', io.StringIO()) as err:
                    with self.assertRaises(SystemExit):
                        app.main()
                self.assertIn(expected, err.getvalue())
                if windows:
                    self.assertNotIn('command codex', err.getvalue())

    def test_windows_bypass_hint_quotes_the_path(self):
        with patch.object(app, 'WINDOWS', True), patch.object(app, 'locate_cli', return_value="C:\\Users\\O'Neil\\codex.exe"):
            self.assertEqual(app.direct_cli(), "& 'C:\\Users\\O''Neil\\codex.exe'")
        with patch.object(app, 'WINDOWS', True), patch.object(app, 'locate_cli', return_value=None):
            self.assertNotIn('command codex', app.direct_cli())

    def test_windows_waits_and_returns_child_exit_status(self):
        import subprocess
        with patch.object(app, 'WINDOWS', True), patch.object(app.subprocess, 'run', return_value=subprocess.CompletedProcess([], 17)) as run:
            with self.assertRaises(SystemExit) as stopped:
                app.run_cli(str(self.cli), ['agents'], {'test': 'value'})
            self.assertEqual(stopped.exception.code, 17)
            run.assert_called_once_with([str(self.cli), 'agents'], env={'test': 'value'})


if __name__ == "__main__":
    unittest.main()
