"""Desktop transport: primary stdio and secondary Unix clients share one server."""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from codex_desktop import backend as codex_desktop
from codex_desktop.backend import HELPERS, find_cli, hold_runtime, installed_at, is_app_server
from codex_desktop.cli import process_image


def pid_alive(pid):
    if os.name == 'nt':
        try:
            process_image(pid)
        except OSError as exc:
            raise ProcessLookupError(pid) from exc
    else:
        os.kill(pid, 0)
    return True



class DiscoveryTests(unittest.TestCase):
    def test_only_wraps_an_app_server_launch(self):
        self.assertTrue(is_app_server(['-c', 'features.code_mode_host=true', 'app-server', '--analytics-default-enabled']))
        for args in (['--version'], ['exec', 'app-server'], ['app-server', 'proxy'],
                     ['app-server', '--help'], ['app-server', '-c', 'a=true', 'daemon', 'version'],
                     ['app-server', 'daemon', 'version'], ['app-server', '--listen=unix://'],
                     ['app-server', '--listen', 'stdio://'], ['-c', 'app-server', 'exec']):
            self.assertFalse(is_app_server(args), args)

    def test_startup_policy_read_is_not_wrapped(self):
        # The app runs this once at launch; with --listen added the CLI rejects it and the app cannot start.
        for args in (['app-server', '--print-startup-config'],
                     ['-c', 'a=true', 'app-server', '--startup-sign-out', '--print-startup-config',
                      '--startup-client-info', '{"name":"codex_desktop"}']):
            self.assertFalse(is_app_server(args), args)



class RuntimeTests(unittest.TestCase):
    """The Windows app's runtime folders under %LOCALAPPDATA%\\OpenAI\\Codex\\bin, as an update leaves them."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.local = Path(tmp.name)
        self.bin = self.local/'OpenAI'/'Codex'/'bin'
        self.bin.mkdir(parents=True)
        self.ages = {}
        patcher = patch.dict(os.environ, {'LOCALAPPDATA': str(self.local)})
        patcher.start()
        self.addCleanup(patcher.stop)
        # os.utime cannot set a creation time, so the order of arrival is given here instead.
        patcher = patch.object(codex_desktop, 'installed_at', lambda path: self.ages[path.parent.name])
        patcher.start()
        self.addCleanup(patcher.stop)

    def version(self, name, age, helpers=HELPERS):
        folder = self.bin/name
        folder.mkdir()
        for file in ('codex.exe', *helpers):
            (folder/file).write_bytes(b'MZ')
        self.ages[name] = age
        return folder/'codex.exe'

    def test_prefers_the_newest_version(self):
        self.version('a159911ff70742e9', 1)
        newest = self.version('f71a721c0ffee000', 2)
        self.assertEqual(find_cli('auto'), newest)

    def test_skips_a_running_codex_whose_helpers_were_removed(self):
        # The second time: the app removed the old version while our app-server still ran its codex.exe.
        self.version('f71a721c0ffee000', 3, helpers=())
        whole = self.version('5449c1f0deadbeef', 2)
        self.assertEqual(find_cli('auto'), whole)
        # and a version still being unpacked, its helpers not there yet
        self.version('9999999999999999', 4, helpers=HELPERS[:1])
        self.assertEqual(find_cli('auto'), whole)

    def test_falls_back_to_the_newest_when_none_is_whole(self):
        self.version('a159911ff70742e9', 1, helpers=())
        newest = self.version('f71a721c0ffee000', 2, helpers=())
        self.assertEqual(find_cli('auto'), newest)

    def test_configured_path_and_missing_app(self):
        self.assertEqual(find_cli(r'C:\codex\codex.exe'), Path(r'C:\codex\codex.exe'))
        with self.assertRaises(RuntimeError):
            find_cli('auto')

    def test_holds_the_runtime_it_runs_and_rg_on_path(self):
        cli = self.version('5449c1f0deadbeef', 2)
        self.version('f71a721c0ffee000', 1)  # an older version, not ours to hold
        rg = self.bin/'a7d4c49000000000'
        rg.mkdir()
        (rg/'rg.exe').write_bytes(b'MZ')
        (cli.parent/'codex.exe.partial').write_bytes(b'')  # not a runtime file: maybe the app's, in progress
        elsewhere = self.local/'tools'
        elsewhere.mkdir()
        (elsewhere/'tool.exe').write_bytes(b'MZ')
        env = dict(PATH=os.pathsep.join((str(elsewhere), str(rg), str(self.local/'absent'))))
        with patch.object(codex_desktop, 'WINDOWS', True):
            held = hold_runtime(cli, env)
            self.addCleanup(lambda: [handle.close() for handle in held])
            self.assertEqual(sorted(Path(handle.name).name for handle in held),
                             sorted(['codex.exe', *HELPERS, 'rg.exe']))
            folders = {Path(handle.name).parent.resolve() for handle in held}
            self.assertEqual(folders, {cli.parent.resolve(), rg.resolve()})  # not the other version
            # A CLI outside the app's folder is the user's own: leave it alone.
            self.assertEqual(hold_runtime(elsewhere/'tool.exe', {}), [])
        if os.name != 'nt':
            self.assertEqual(hold_runtime(cli, env), [])  # an open file does not keep it from being deleted here

    @unittest.skipUnless(os.name == 'nt', 'Windows file sharing')
    def test_held_helpers_survive_the_update_and_still_start(self):
        cli = self.version('a159911ff70742e9', 1)
        # A real executable that runs from any folder stands in for the code-mode host.
        system = Path(os.environ.get('SystemRoot', r'C:\Windows'))/'System32'/'cmd.exe'
        host = cli.parent/HELPERS[0]
        host.write_bytes(system.read_bytes())
        held = hold_runtime(cli, {})
        try:
            subprocess.run(['powershell', '-NoProfile', '-Command',
                            f"Remove-Item -LiteralPath '{cli.parent}' -Recurse -Force -ErrorAction SilentlyContinue"],
                           check=False, timeout=60)
            for name in HELPERS:
                self.assertTrue((cli.parent/name).is_file(), name)
            self.assertEqual(subprocess.run([str(host), '/c', 'exit', '3'], timeout=30).returncode, 3)
        finally:
            for handle in held:
                handle.close()
        os.remove(host)  # released: the next update may remove it


class InstalledAtTests(unittest.TestCase):
    def test_counts_creation_on_windows(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'codex.exe'
            path.write_bytes(b'MZ')
            packaged = time.time() - 3*86400
            os.utime(path, (packaged, packaged))  # an unpacker that keeps the packaged time
            if os.name == 'nt':
                self.assertGreater(installed_at(path), packaged + 86400)
            else:
                self.assertAlmostEqual(installed_at(path), packaged, delta=1)


class FallbackTests(unittest.TestCase):
    def run_wrapper(self, *args):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # Stands in for a CLI whose clap parser rejects --listen next to any one-shot flag.
            if os.name == 'nt':
                fake = root/'fake_codex.py'
                fake.write_text('import sys\nargs = sys.argv[1:]\n'
                                'if "--listen" in args:\n    print("rejected", file=sys.stderr); sys.exit(2)\n'
                                'print("ran: " + " ".join(args))\n')
                cli = root/'codex.cmd'
                cli.write_text(f'@"{sys.executable}" "{fake}" %*\r\n')
            else:
                cli = root/'codex'
                cli.write_text('#!/bin/sh\nfor a in "$@"; do [ "$a" = --listen ] && { echo rejected >&2; exit 2; }; done\n'
                               'echo "ran: $*"\n')
                cli.chmod(0o700)
            settings = root/'desktop.json'
            settings.write_text(json.dumps(dict(cli=str(cli), instances=str(root/'instances'))))
            return subprocess.run([sys.executable, '-m', 'codex_desktop.backend', *args],
                                  capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL,
                                  env={**os.environ, 'CODEX_DESKTOP_SETTINGS': str(settings)})

    def test_policy_read_reaches_the_cli_untouched(self):
        done = self.run_wrapper('app-server', '--print-startup-config')
        self.assertEqual((done.returncode, done.stdout), (0, 'ran: app-server --print-startup-config\n'), done.stderr)

    def test_runs_the_cli_directly_when_it_refuses_listen(self):
        done = self.run_wrapper('app-server', '--some-future-oneshot')
        self.assertEqual((done.returncode, done.stdout), (0, 'ran: app-server --some-future-oneshot\n'), done.stderr)


class LifetimeTests(unittest.TestCase):
    @unittest.skipUnless(os.name == 'nt', 'Windows job objects')
    def test_cli_ends_when_the_wrapper_is_killed(self):
        # The app kills the wrapper's launcher; the CLI used to stay behind, holding its conversations.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pidfile = root/'cli.pid'
            fake = root/'fake_codex.py'
            fake.write_text(f'import os, time\nopen({str(pidfile)!r}, "w").write(str(os.getpid()))\ntime.sleep(120)\n')
            cli = root/'codex.cmd'
            cli.write_text(f'@"{sys.executable}" "{fake}" %*\r\n')
            settings = root/'desktop.json'
            settings.write_text(json.dumps(dict(cli=str(cli), instances=str(root/'instances'))))
            wrapper = subprocess.Popen([sys.executable, '-m', 'codex_desktop.backend', 'exec-server'],
                                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                       env={**os.environ, 'CODEX_DESKTOP_SETTINGS': str(settings)})
            try:
                deadline = time.monotonic() + 30
                while not (pidfile.exists() and pidfile.read_text()):
                    self.assertLess(time.monotonic(), deadline, 'the fake CLI did not start')
                    time.sleep(.1)
                pid = int(pidfile.read_text())
                self.assertTrue(pid_alive(pid))
                wrapper.kill()  # no chance to clean up, as when the app ends it
                wrapper.wait(timeout=10)
                deadline = time.monotonic() + 10
                while True:
                    try:
                        pid_alive(pid)
                    except ProcessLookupError:
                        break
                    self.assertLess(time.monotonic(), deadline, 'the CLI outlived its wrapper')
                    time.sleep(.1)
            finally:
                if wrapper.poll() is None:
                    wrapper.kill()


if __name__ == "__main__":
    unittest.main()
