"""Install reversible per-user app and shell entry points."""
import argparse
import csv
import ctypes
import io
import json
import os
from pathlib import Path
import plistlib
import re
import shlex
import shutil
import subprocess
import sys

from .paths import codex_home, root, settings_file

LABEL = 'io.github.k3k8.codex-desktop'
BEGIN = '# >>> codex-desktop >>>'
END = '# <<< codex-desktop <<<'


def atomic(path, content):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_bytes(content)
    if os.name != 'nt':
        temporary.chmod(0o600)
    temporary.replace(path)


def private_directory(path):
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != 'nt':
        path.chmod(0o700)
    else:
        # The token must not inherit a permissive ACL from a custom CODEX_HOME.
        result = subprocess.run(['whoami', '/user', '/fo', 'csv', '/nh'], check=True, capture_output=True, text=True)
        sid = next(csv.reader(io.StringIO(result.stdout)))[1]
        subprocess.run(['icacls', str(path), '/inheritance:r', '/grant:r', '*' + sid + ':(OI)(CI)F'],
                       check=True, stdout=subprocess.DEVNULL)


def user_env():
    if sys.platform == 'darwin':
        return subprocess.run(['launchctl', 'getenv', 'CODEX_CLI_PATH'], capture_output=True, text=True).stdout.strip()
    if os.name == 'nt':
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, 'Environment') as key:
            try:
                return winreg.QueryValueEx(key, 'CODEX_CLI_PATH')[0]
            except FileNotFoundError:
                return ''
    raise RuntimeError('Desktop setup supports macOS and Windows. Linux already has a shared CLI daemon.')


def set_user_env(value):
    if sys.platform == 'darwin':
        subprocess.run(['launchctl', 'setenv', 'CODEX_CLI_PATH', value] if value else
                       ['launchctl', 'unsetenv', 'CODEX_CLI_PATH'], check=True)
    elif os.name == 'nt':
        import winreg
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, 'Environment') as key:
            if value:
                winreg.SetValueEx(key, 'CODEX_CLI_PATH', 0, winreg.REG_SZ, value)
            else:
                try:
                    winreg.DeleteValue(key, 'CODEX_CLI_PATH')
                except FileNotFoundError:
                    pass
        result = ctypes.c_size_t()
        ctypes.windll.user32.SendMessageTimeoutW(0xffff, 0x1a, 0, 'Environment', 0x2, 5000, ctypes.byref(result))


def app_cli(application=None, configured=None):
    if configured:
        path = Path(configured).expanduser().resolve()
        if not path.is_file():
            raise RuntimeError('CLI does not exist: ' + str(path))
        return str(path)
    if os.name == 'nt':
        return 'auto'
    apps = [Path(application).expanduser()] if application else [Path('/Applications/ChatGPT.app'), Path('/Applications/Codex.app')]
    for app in apps:
        for suffix in ('Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex',
                       'Contents/Resources/codex-cli/bin/codex', 'Contents/Resources/codex'):
            path = app / suffix
            if path.is_file():
                return str(path.resolve())
    raise RuntimeError('Cannot find the desktop app. Specify --app /path/to/ChatGPT.app or --cli /path/to/codex.')


def installed_command(name):
    # Use this installation, even if an older tool precedes it on PATH.
    path = Path(sys.executable).parent / (name + ('.exe' if os.name == 'nt' else ''))
    if not path.is_file():
        raise RuntimeError('Missing entry point: ' + str(path) + '. Install this package first.')
    return path


def profile_paths():
    if os.name == 'nt':
        found = []
        for shell in ('pwsh', 'powershell'):
            if shutil.which(shell):
                result = subprocess.run([shell, '-NoProfile', '-Command', '$PROFILE.CurrentUserAllHosts'],
                                        check=True, capture_output=True, text=True)
                path = Path(result.stdout.strip())
                if path not in found:
                    found.append(path)
        if not found:
            raise RuntimeError('Cannot find PowerShell to resolve its profile location.')
        return [(p, 'powershell') for p in found]
    files = [(Path.home() / '.zshrc', 'posix'), (Path.home() / '.bashrc', 'posix')]
    fish = Path(os.environ.get('XDG_CONFIG_HOME') or Path.home() / '.config') / 'fish/conf.d/codex-desktop.fish'
    if shutil.which('fish'):
        files.append((fish, 'fish'))
    return files


def update_profile(path, shell, command=None):
    original = path.read_bytes() if path.exists() else b''
    # Work in bytes so CRLF and non-ASCII user content are preserved exactly.
    pattern = rb'(?m)^' + re.escape(BEGIN.encode()) + rb'\r?\n.*?^' + re.escape(END.encode()) + rb'(?:\r?\n|$)'
    cleaned = re.sub(pattern, b'', original, flags=re.DOTALL)
    if command is None:
        changed = cleaned
    else:
        if shell == 'powershell':
            quoted = "'" + str(command).replace("'", "''") + "'"
            body = 'function global:codex { & ' + quoted + ' @args }'
        elif shell == 'fish':
            quoted = "'" + str(command).replace('\\', '\\\\').replace("'", "\\'") + "'"
            body = 'function codex\n    command ' + quoted + ' $argv\nend'
        else:
            body = 'codex() { command ' + shlex.quote(str(command)) + ' "$@"; }'
        block = (BEGIN + '\n' + body + '\n' + END + '\n').encode('utf-8')
        changed = cleaned + (b'\n' if cleaned and not cleaned.endswith(b'\n') else b'') + block
    if changed != original:
        # Keep a single original backup for manual recovery; never overwrite it on upgrades.
        backup = path.with_name(path.name + '.before-codex-desktop')
        if path.exists() and not backup.exists():
            atomic(backup, original)
        mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
        atomic(path, changed)
        if os.name != 'nt':
            path.chmod(mode)


def agent_path():
    return Path.home() / 'Library/LaunchAgents' / (LABEL + '.plist')


def enable(args):
    current = user_env()
    wrapper = installed_command('codex-desktop-app') if os.name == 'nt' else root() / 'app'
    if current and current != str(wrapper):
        raise RuntimeError('CODEX_CLI_PATH already points to ' + current + '. Disable that integration first; it will not be overwritten.')
    real = app_cli(args.app, args.cli)
    profiles = profile_paths() if args.shells else []
    cli = installed_command('codex-desktop') if profiles else None
    private_directory(root())
    instances = root() / 'instances'
    private_directory(instances)
    old = json.loads(settings_file().read_text()) if settings_file().exists() else {}
    settings = dict(cli=real, instances=str(instances), previousEnv=old.get('previousEnv', current),
                    wrapper=str(wrapper), profiles=old.get('profiles', []))
    for path, shell in profiles:
        row = dict(path=str(path), shell=shell)
        if row not in settings['profiles']:
            settings['profiles'].append(row)
    paths = [settings_file()]
    if sys.platform == 'darwin':
        paths.extend([wrapper, agent_path()])
    paths.extend(path for path, _ in profiles)
    before = {p: (p.read_bytes(), p.stat().st_mode & 0o777) if p.exists() else None for p in paths}
    try:
        atomic(settings_file(), json.dumps(settings, indent=2).encode())
        if sys.platform == 'darwin':
            script = '#!/bin/sh\nexport CODEX_DESKTOP_SETTINGS=' + shlex.quote(str(settings_file())) + '\nexec ' + shlex.quote(str(installed_command('codex-desktop-app'))) + ' "$@"\n'
            atomic(wrapper, script.encode())
            wrapper.chmod(0o700)
            atomic(agent_path(), plistlib.dumps(dict(Label=LABEL, RunAtLoad=True,
                   ProgramArguments=['/bin/launchctl', 'setenv', 'CODEX_CLI_PATH', str(wrapper)])))
        for path, shell in profiles:
            update_profile(path, shell, cli)
        set_user_env(str(wrapper))
    except BaseException:
        for path, previous in before.items():
            if previous is None:
                path.unlink(missing_ok=True)
            else:
                atomic(path, previous[0])
                if os.name != 'nt':
                    path.chmod(previous[1])
        set_user_env(current)
        raise
    print('Enabled. Fully quit and reopen the desktop app. Open a new terminal for shell changes.')
    print('Connect explicitly with: codex-desktop resume <thread-id>')


def disable():
    if not settings_file().exists():
        print('No setup to disable.')
        return
    settings = json.loads(settings_file().read_text())
    if user_env() == settings['wrapper']:
        set_user_env(settings.get('previousEnv', ''))
    if sys.platform == 'darwin':
        subprocess.run(['launchctl', 'bootout', 'gui/' + str(os.getuid()) + '/' + LABEL],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        agent_path().unlink(missing_ok=True)
    for row in settings.get('profiles', []):
        update_profile(Path(row['path']), row['shell'])
    print('Disabled. Fully quit and reopen the desktop app and terminal.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['enable', 'disable', 'status'])
    parser.add_argument('--app', help='macOS .app path')
    parser.add_argument('--cli', help='Override the app-bundled CLI path')
    parser.add_argument('--shells', action='store_true', help='Install a reversible codex shell function')
    args = parser.parse_args()
    try:
        if args.action == 'enable':
            enable(args)
        elif args.action == 'disable':
            disable()
        else:
            print(json.dumps(dict(settings=str(settings_file()), CODEX_CLI_PATH=user_env()), indent=2))
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
        parser.exit(1, 'codex-desktop: ' + str(exc) + '\n')
