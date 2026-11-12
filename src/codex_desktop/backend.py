"""Give the desktop app's own stdio app-server an additional private transport.

Installed as CODEX_CLI_PATH by `codex-desktop-setup enable`. The real, app-bundled
CLI still receives every argument and environment override supplied by the app.
Its stdio proxy keeps the app connected to that exact server. No second
conversation is resumed.

macOS: a Unix socket in a private directory; no TCP listener is opened.
Windows: Python cannot reach a Unix socket there, so the server listens on
ws://127.0.0.1 with a random port and a capability token kept in a file only
this user can read. The app-server refuses a connection without the token (401).
"""
from __future__ import annotations

import json
import asyncio
import os
from pathlib import Path
import re
import secrets
import signal
import subprocess
import sys
import tempfile
import threading
import time

from websockets.asyncio.client import connect, unix_connect

WINDOWS = os.name == 'nt'
LISTENING = re.compile(r'listening on: (ws://127\.0\.0\.1:\d+)\s*$')


async def stdio_proxy(address):
    """Forward JSON messages unchanged; `codex app-server proxy` uses a different wire format."""
    reader = asyncio.StreamReader(limit=32*1024*1024)
    transport, _ = await asyncio.get_running_loop().connect_read_pipe(
        lambda: asyncio.StreamReaderProtocol(reader), sys.stdin.buffer)
    tasks = []
    try:
        async with unix_connect(str(address), max_size=32*1024*1024) as connection:
            async def sending():
                while line := await reader.readline():
                    if line.strip():
                        await connection.send(line.decode('utf-8').rstrip('\n'))

            async def receiving():
                async for message in connection:
                    if not isinstance(message, str):
                        raise ValueError('app-server returned a non-text WebSocket frame')
                    sys.stdout.buffer.write(message.encode('utf-8') + b'\n')
                    sys.stdout.buffer.flush()

            tasks = [asyncio.create_task(sending()), asyncio.create_task(receiving())]
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        transport.close()


async def stdio_proxy_threads(url, token):
    """stdio_proxy for Windows: asyncio cannot read an inherited pipe there, so a thread reads stdin."""
    loop = asyncio.get_running_loop()
    lines = asyncio.Queue()

    def read_stdin():
        try:
            for line in sys.stdin.buffer:
                loop.call_soon_threadsafe(lines.put_nowait, line)
        finally:
            loop.call_soon_threadsafe(lines.put_nowait, None)

    threading.Thread(target=read_stdin, daemon=True).start()
    tasks = []
    try:
        async with connect(url, additional_headers={'Authorization': 'Bearer ' + token},
                           max_size=32*1024*1024, open_timeout=10) as connection:
            async def sending():
                while (line := await lines.get()) is not None:
                    if line.strip():
                        await connection.send(line.decode('utf-8').rstrip('\r\n'))

            async def receiving():
                async for message in connection:
                    if not isinstance(message, str):
                        raise ValueError('app-server returned a non-text WebSocket frame')
                    sys.stdout.buffer.write(message.encode('utf-8') + b'\n')
                    sys.stdout.buffer.flush()

            tasks = [asyncio.create_task(sending()), asyncio.create_task(receiving())]
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


# What the Windows app ships beside codex.exe. A CLI starts them from its own folder (the code-mode host
# is looked up next to the running exe), so a codex.exe left without them fails every shell tool.
HELPERS = ('codex-code-mode-host.exe', 'codex-command-runner.exe', 'codex-windows-sandbox-setup.exe')


def codex_bin():
    return Path(os.environ.get('LOCALAPPDATA') or Path.home() / 'AppData/Local') / 'OpenAI/Codex/bin'


def installed_at(path):
    """When the file arrived. An unpacked file may keep its packaged mtime, so on Windows creation counts too."""
    info = path.stat()
    if WINDOWS:
        return max(info.st_mtime, getattr(info, 'st_birthtime', None) or info.st_ctime)
    return info.st_mtime


def find_cli(configured):
    """The real CLI. On Windows the app keeps it under a hashed folder that changes on every update.

    An update unpacks the new version beside the old one and then deletes the old one's files, all but a
    codex.exe that is still running. So prefer a version that has its helpers, and among those the newest.
    """
    if configured and configured != 'auto':
        return Path(configured)
    base = codex_bin()
    found = []
    for cli in base.glob('*/codex.exe'):
        try:
            found.append((all((cli.parent / name).is_file() for name in HELPERS), installed_at(cli), cli))
        except OSError:
            continue  # removed while we looked
    if not found:
        raise RuntimeError('Cannot find the desktop app runtime under ' + str(base))
    return max(found, key=lambda item: item[:2])[2]


def hold_runtime(real, env):
    """Open the files of the app runtime this CLI runs from, so the app's update cannot delete them under it.

    Windows only. The app started the wrapper before it unpacked an update, then removed the old version:
    the running codex.exe stayed (Windows will not delete a running exe) but its helpers went, and every
    shell tool failed until the app was quit. Python opens files without FILE_SHARE_DELETE, so an open
    handle keeps a file from being deleted or renamed the same way; reading and starting it still work.
    The folders on PATH under the app's bin (where rg.exe has its own) are held too. Only executables and
    libraries: anything else may be the app's own file in progress. Close them when done.
    """
    if not WINDOWS:
        return []
    try:
        base = codex_bin().resolve()
    except OSError:
        return []
    folders = []
    for folder in [real.parent, *(Path(entry) for entry in env.get('PATH', '').split(os.pathsep) if entry)]:
        try:
            folder = folder.resolve()
        except OSError:
            continue
        if folder.parent == base and folder not in folders:
            folders.append(folder)
    held = []
    for folder in folders:
        try:
            files = [path for path in folder.iterdir() if path.suffix.lower() in ('.exe', '.dll') and path.is_file()]
        except OSError:
            continue
        for path in files:
            try:
                held.append(path.open('rb'))
            except OSError:
                pass  # being written or already gone; nothing to keep
    return held


def is_app_server(args):
    # Skip values of global options, so e.g. `-c key=app-server` cannot match.
    i = 0
    while i < len(args):
        value = args[i]
        if value in ('-c', '--config', '--enable', '--disable'):
            i += 2
            continue
        if value == 'app-server':
            rest = args[i + 1:]
            # --print-startup-config is the app's one-shot policy read; clap rejects it together with --listen.
            if any(a in ('--help', '-h', '--version', '--stdio', '--listen', '--print-startup-config')
                   or a.startswith('--listen=') for a in rest):
                return False
            j = 0
            while j < len(rest):
                if rest[j] in ('-c', '--config', '--enable', '--disable', '--code-mode-host',
                               '--startup-client-info', '--cloud-skills-snapshot', '--ws-auth',
                               '--ws-token-file', '--ws-token-sha256', '--ws-shared-secret-file',
                               '--ws-issuer', '--ws-audience', '--ws-max-clock-skew-seconds'):
                    j += 2
                elif not rest[j].startswith('-'):
                    return False  # a subcommand, including one preceded by options
                else:
                    j += 1
            return True
        if not value.startswith('-'):
            return False
        i += 1
    return False


class BackendExited(RuntimeError):
    """The CLI refused our extra --listen before opening the socket."""


JOB = []  # the job handle, kept open for as long as this process lives


def end_with_us(child):
    """Windows: make the CLI end when this wrapper ends, however it ends.

    The app ends the wrapper by killing its launcher. uv's launcher takes Python down with it, but lets
    Python's own children break away, so codex.exe stayed behind with no parent, still holding the
    conversations it had loaded (a conversation has one writer). A job with KILL_ON_JOB_CLOSE is closed by
    Windows when this process ends, and that ends the CLI and what it started. BREAKAWAY_OK keeps the CLI's
    own CREATE_BREAKAWAY_FROM_JOB launches working. On failure the CLI just runs without the job.
    """
    if not WINDOWS:
        return
    import ctypes
    from ctypes import wintypes

    class Basic(ctypes.Structure):  # JOBOBJECT_BASIC_LIMIT_INFORMATION
        _fields_ = [('PerProcessUserTimeLimit', ctypes.c_int64), ('PerJobUserTimeLimit', ctypes.c_int64),
                    ('LimitFlags', ctypes.c_uint32), ('MinimumWorkingSetSize', ctypes.c_size_t),
                    ('MaximumWorkingSetSize', ctypes.c_size_t), ('ActiveProcessLimit', ctypes.c_uint32),
                    ('Affinity', ctypes.c_size_t), ('PriorityClass', ctypes.c_uint32),
                    ('SchedulingClass', ctypes.c_uint32)]

    class Extended(ctypes.Structure):  # JOBOBJECT_EXTENDED_LIMIT_INFORMATION
        _fields_ = [('BasicLimitInformation', Basic), ('IoInfo', ctypes.c_uint64 * 6),
                    ('ProcessMemoryLimit', ctypes.c_size_t), ('JobMemoryLimit', ctypes.c_size_t),
                    ('PeakProcessMemoryUsed', ctypes.c_size_t), ('PeakJobMemoryUsed', ctypes.c_size_t)]

    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.CreateJobObjectW.argtypes = (wintypes.LPVOID, wintypes.LPCWSTR)
    kernel.SetInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    try:
        if not JOB:
            job = kernel.CreateJobObjectW(None, None)  # not inheritable: the CLI must not keep it open
            if not job:
                raise ctypes.WinError(ctypes.get_last_error())
            limits = Extended()
            limits.BasicLimitInformation.LimitFlags = 0x2000 | 0x800  # KILL_ON_JOB_CLOSE | BREAKAWAY_OK
            if not kernel.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
                error = ctypes.WinError(ctypes.get_last_error())
                kernel.CloseHandle(job)
                raise error
            JOB.append(job)
        process = kernel.OpenProcess(0x0100 | 0x0001, False, child.pid)  # PROCESS_SET_QUOTA | PROCESS_TERMINATE
        if not process:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            if not kernel.AssignProcessToJobObject(JOB[0], process):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            kernel.CloseHandle(process)
    except OSError as exc:
        print('codex-desktop: the CLI may outlive this wrapper: ' + str(exc), file=sys.stderr)


def stop_child(child):
    if child is not None and child.poll() is None:
        child.terminate()
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait()


def serve_windows(real, args, directory, env=None):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    record = directory / (str(os.getpid()) + '.json')
    token_file = directory / (str(os.getpid()) + '.token')
    token = secrets.token_urlsafe(32)
    backend = None
    try:
        token_file.write_text(token)
        # Port 0: the server picks a free port and prints it on stderr, which we then pass through.
        backend = subprocess.Popen([str(real), *args, '--listen', 'ws://127.0.0.1:0', '--ws-auth', 'capability-token',
                                    '--ws-token-file', str(token_file)],
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, env=env)
        end_with_us(backend)
        found = {}

        def pump():
            for raw in backend.stderr:
                text = raw.decode('utf-8', 'replace')
                if 'url' not in found and (match := LISTENING.search(text.strip())):
                    found['url'] = match.group(1)
                try:
                    sys.stderr.write(text)
                    sys.stderr.flush()
                except (OSError, ValueError, AttributeError):
                    pass

        threading.Thread(target=pump, daemon=True).start()
        deadline = time.monotonic() + 20
        while 'url' not in found:
            if backend.poll() is not None:
                raise BackendExited(f'desktop app-server exited ({backend.returncode})')
            if time.monotonic() >= deadline:
                raise TimeoutError('desktop app-server did not report its address')
            time.sleep(.05)
        temporary = record.with_suffix('.tmp')
        temporary.write_text(json.dumps(dict(url=found['url'], token=str(token_file), pid=os.getpid(),
                                             serverPid=backend.pid, kind='desktop', cli=str(real))))
        temporary.replace(record)
        asyncio.run(stdio_proxy_threads(found['url'], token))
        return 0
    finally:
        record.unlink(missing_ok=True)
        stop_child(backend)
        token_file.unlink(missing_ok=True)


def serve(real, args, directory, env=None):
    if WINDOWS:
        return serve_windows(real, args, directory, env)
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    record = directory / (str(os.getpid()) + '.json')
    backend = None
    handlers = {}

    def stop(signum, frame):
        raise SystemExit(128 + signum)

    try:
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            handlers[sig] = signal.signal(sig, stop)
        # macOS sockaddr_un is short. Keep the actual socket out of long home paths.
        with tempfile.TemporaryDirectory(prefix='codex-desktop-', dir='/tmp') as tmp:
            address = Path(tmp) / 'app.sock'
            backend = subprocess.Popen([str(real), *args, '--listen', 'unix://' + str(address)],
                                       stdin=subprocess.DEVNULL, stdout=sys.stderr, env=env)
            deadline = time.monotonic() + 20
            while not address.is_socket():
                if backend.poll() is not None:
                    raise BackendExited(f'desktop app-server exited ({backend.returncode})')
                if time.monotonic() >= deadline:
                    raise TimeoutError('desktop app-server socket did not appear')
                time.sleep(.05)
            os.chmod(address, 0o600)
            temporary = record.with_suffix('.tmp')
            temporary.write_text(json.dumps(dict(socket=str(address), pid=os.getpid(),
                                                serverPid=backend.pid, kind='desktop')))
            temporary.chmod(0o600)
            temporary.replace(record)
            asyncio.run(stdio_proxy(address))
            return 0
    finally:
        record.unlink(missing_ok=True)
        for child in (backend,):
            if child is not None and child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()
        for sig, handler in handlers.items():
            signal.signal(sig, handler)


def run_directly(real, args, env):
    if not WINDOWS:
        os.execve(real, [str(real), *args], env)
    # Windows has no exec: os.execve starts a new process and ends this one, which the app would see
    # as its CLI exiting. Run the CLI as a child on the same stdio instead and pass its exit code on.
    child = subprocess.Popen([str(real), *args], env=env)
    end_with_us(child)
    try:
        return child.wait()
    finally:
        stop_child(child)


def main(settings_path=None):
    from codex_desktop.paths import settings_file
    settings = Path(settings_path or os.environ.get('CODEX_DESKTOP_SETTINGS') or settings_file())
    config = json.loads(settings.read_text())
    real = find_cli(config['cli'])
    if real.resolve() == Path(sys.argv[0]).resolve():
        raise RuntimeError('desktop CLI points back to its wrapper')
    env = dict(os.environ)
    env.pop('CODEX_CLI_PATH', None)
    env['CODEX_APP_RUNTIME_CLI_PATH'] = str(real)
    held = hold_runtime(real, env)
    try:
        if not is_app_server(sys.argv[1:]):
            return run_directly(real, sys.argv[1:], env)
        try:
            return serve(real, sys.argv[1:], config['instances'], env)
        except BackendExited as exc:
            # This wrapper sits on the app's startup path: when the CLI will not take our --listen
            # (a new one-shot flag, say), run it untouched rather than leave the app unable to start.
            print('codex-desktop: ' + str(exc) + '; running the CLI directly', file=sys.stderr)
            return run_directly(real, sys.argv[1:], env)
    finally:
        for handle in held:
            handle.close()


def entry(settings_path=None):
    try:
        code = main(settings_path)
    except (OSError, ValueError, RuntimeError, TimeoutError) as exc:
        print('codex-desktop: ' + str(exc), file=sys.stderr)
        code = 1
    if WINDOWS:
        # The stdin reader thread may still sit in a blocking read; a normal shutdown then aborts with
        # "could not acquire lock for <stdin>". Everything is cleaned up by now, so leave directly.
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.flush()
            except (OSError, ValueError, AttributeError):
                pass
        os._exit(code or 0)
    return code


if __name__ == '__main__':
    sys.exit(entry())
