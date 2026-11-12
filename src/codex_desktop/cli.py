#!/usr/bin/env python3
"""Connect the interactive CLI to the desktop app's own server."""
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys

WINDOWS = os.name == "nt"


def process_image(pid):
    """Inspect Windows processes without os.kill(pid, 0), which is unsafe there."""
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    kernel.QueryFullProcessImageNameW.argtypes = (
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD))
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        raise OSError("process not accessible")
    try:
        code = wintypes.DWORD()
        if not kernel.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value != 259:
            raise OSError("process exited")
        buffer = ctypes.create_unicode_buffer(32768)
        length = wintypes.DWORD(len(buffer))
        if not kernel.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(length)):
            raise OSError("cannot inspect process")
        return Path(buffer.value)
    finally:
        kernel.CloseHandle(handle)


def fail(message):
    print("codex: " + message, file=sys.stderr)
    raise SystemExit(1)


def run_cli(cli, args, env=None):
    argv = [cli, *args]
    if WINDOWS:
        # execv on Windows starts a child without preserving the shell's wait.
        raise SystemExit(subprocess.run(argv, env=env).returncode)
    if env is None:
        os.execv(cli, argv)
    else:
        os.execve(cli, argv, env)


def desktop_connection(home):
    connections = {}
    for record_path in [*(home / "codex-desktop/instances").glob("*.json"),
                        *(home / "codama/desktop/instances").glob("*.json")]:
        try:
            info = record_path.stat()
            if (not stat.S_ISREG(info.st_mode) or info.st_size > 16384
                    or (not WINDOWS and (info.st_uid != os.getuid() or info.st_mode & 0o022))):
                continue
            record = json.loads(record_path.read_text())
            if not isinstance(record, dict) or record.get("kind") != "desktop":
                continue
            for key in ("pid", "serverPid"):
                pid = record[key]
                if type(pid) is not int or pid <= 0:
                    raise ValueError("invalid pid")
                if WINDOWS:
                    image = process_image(pid)
                else:
                    os.kill(pid, 0)
            if WINDOWS:
                url = record["url"]
                match = re.fullmatch(r"ws://127\.0\.0\.1:([0-9]+)", url)
                if not match or not 0 < int(match[1]) < 65536:
                    continue
                token = Path(record["token"]).resolve()
                if token != record_path.with_suffix(".token").resolve() or not token.is_file():
                    continue
                # Older records omit cli; the server process is authoritative.
                cli = Path(record.get("cli") or image)
                if not cli.is_file() or cli.resolve() != image.resolve():
                    continue
                connections[url] = {"address": url, "token": token, "cli": str(cli)}
                continue
            address = Path(record["socket"])
            if not address.is_absolute():
                continue
            parent, info = address.parent.stat(), address.stat()
            if (not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid()
                    or parent.st_uid != os.getuid() or parent.st_mode & 0o077):
                continue
            url = "unix://" + str(address)
            connections[url] = {"address": url}
        except (OSError, ValueError, KeyError, TypeError):
            continue
    if not connections:
        fail("No desktop connection. Run codex-desktop-setup enable, then fully restart the app.")
    if len(connections) != 1:
        fail("Multiple desktop servers found. Quit the other desktop app instances.")
    return next(iter(connections.values()))


def desktop_address(home):
    return desktop_connection(home)["address"]


def locate_cli(connection=None):
    # Prefer the standalone installer's link, which its updater keeps current.
    # An older copy earlier on PATH (an npm install under a node version, say)
    # may not know --remote.
    me = Path(__file__).resolve()
    candidates = [str(Path.home() / ".local/bin/codex"), shutil.which("codex")]
    if WINDOWS:
        candidates.insert(0, (connection or {}).get("cli"))
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData/Local")
        candidates.extend(str(p) for p in sorted(
            (base / "OpenAI/Codex/bin").glob("*/codex.exe"),
            key=lambda p: p.stat().st_ctime, reverse=True))
    for found in candidates:
        if not found:
            continue
        path = Path(found)
        if path.is_file() and os.access(path, os.X_OK) and path.resolve() not in (me, Path(sys.argv[0]).resolve()):
            return str(path)
    return None


def find_cli(connection=None):
    cli = locate_cli(connection)
    if not cli:
        fail("Cannot find Codex CLI. Install the official standalone CLI first.")
    return cli


def direct_cli():
    """How to run the CLI itself, bypassing this wrapper, in the user's shell."""
    if not WINDOWS:
        return "command codex"
    # PowerShell has no `command`, and the codex function shadows codex.exe,
    # so name the binary by path.
    cli = locate_cli()
    if not cli:
        return "& '<path to codex.exe>'"
    return "& '" + cli.replace("'", "''") + "'"


def main():
    args = sys.argv[1:]
    # Locate the command even when global options precede it.
    value_options = {
        "-c", "--config", "-i", "--image", "-m", "--model", "-p", "--profile",
        "-s", "--sandbox", "-a", "--ask-for-approval", "-C", "--cd", "--add-dir",
        "--enable", "--disable", "--remote", "--remote-auth-token-env",
    }
    command = None
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "--":
            break
        if not arg.startswith("-"):
            command = arg
            break
        index += 2 if arg in value_options else 1
    # These maintain the installation/account and do not start a conversation.
    management = {
        "login", "logout", "mcp", "plugin", "app-server", "remote-control",
        "app", "completion", "update", "doctor", "sandbox", "debug", "apply",
        "migrate-rollouts", "migrate-nfs-db", "features", "help", "exec-server",
    }
    if (any(arg in ("--help", "-h", "--version", "-V") for arg in args)
            or command in management):
        cli = find_cli()
        return run_cli(cli, args)
    if command in ("exec", "e", "review", "cloud"):
        fail("This entry point is for interactive desktop sessions. For this command, use " + direct_cli() + ".")
    if any(arg == "--no-daemon" or arg == "--remote"
           or arg.startswith("--remote=") or arg == "--remote-auth-token-env"
           or arg.startswith("--remote-auth-token-env=") for arg in args):
        fail("Desktop connections are discovered automatically. To set an endpoint, use " + direct_cli() + ".")
    home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    connection = desktop_connection(home)
    cli = find_cli(connection)
    remote_args = ["--remote", connection["address"]]
    env = os.environ.copy()
    if "token" in connection:
        token_file = connection["token"]
        if token_file.stat().st_size > 4096:
            fail("Invalid desktop authentication file.")
        token = token_file.read_text(encoding="utf-8").strip()
        if not token or "\n" in token or "\r" in token:
            fail("Invalid desktop authentication file.")
        env["CODEX_DESKTOP_AUTH_TOKEN"] = token
        remote_args += ["--remote-auth-token-env", "CODEX_DESKTOP_AUTH_TOKEN"]
    run_cli(cli, [*remote_args, *args], env)


if __name__ == "__main__":
    main()
