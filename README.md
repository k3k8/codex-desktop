# Codex Desktop Bridge

Use the Codex CLI and desktop app with one shared app-server.

The desktop app keeps its normal stdio connection. This bridge adds a private
local endpoint to that **same process**, then connects the terminal UI with
`--remote`. Both clients see the same loaded conversation.

## Why this exists

Running the app and CLI on **different conversations** already works without this
bridge. Opening the **same conversation** through separate app-server processes
can fail with `already has an active writer`. A plain `codex resume <id>` does not
automatically join the desktop app's server in the version tested here.

Verified in a temporary `CODEX_HOME`, using the unmodified macOS app's
`codex-cli 0.167.0-alpha.3.2`: a second native server could not resume the first
server's thread; the normal CLI displayed “This conversation is open in another
app”. This project shares the running server instead of creating a second writer.

This is an unofficial integration using experimental Codex transport flags. It
does not ship, copy, or modify OpenAI's binaries. See the official
[app-server documentation](https://learn.chatgpt.com/docs/app-server) and
[CLI reference](https://learn.chatgpt.com/docs/cli/reference).

## Requirements

- macOS or Windows with the ChatGPT/Codex desktop app installed.
- A current official Codex CLI with `--remote` and, on Windows,
  `--remote-auth-token-env`. Check `codex --help`.
- [uv](https://docs.astral.sh/uv/getting-started/installation/) on `PATH`.
  The installer uses uv to supply Python 3.11 and an isolated environment.
- Fully quit the desktop app before installing/upgrading. Reopen it afterward.

Linux users normally use the existing shared CLI daemon; desktop setup is limited
to macOS and Windows. Core tests also run on Linux.

## Install

macOS (zsh/bash):

```sh
curl -fsSL https://raw.githubusercontent.com/k3k8/codex-desktop/v0.1.0/install.sh | sh
```

Windows (PowerShell):

```powershell
& ([scriptblock]::Create((Invoke-RestMethod https://raw.githubusercontent.com/k3k8/codex-desktop/v0.1.0/install.ps1)))
```

The installers pin both the package and script to `v0.1.0`. They install the tool,
enable the app bridge, and add a marked `codex` function to shell profiles. Open a
new terminal, fully quit/reopen the app, then use:

```sh
codex resume <thread-id>
```

Without changing shell profiles:

```sh
uv tool install --python 3.11 --from https://github.com/k3k8/codex-desktop/archive/refs/tags/v0.1.0.tar.gz codex-desktop-bridge
codex-desktop-setup enable
codex-desktop resume <thread-id>
```

`codex-desktop-setup enable --shells` adds/replaces only its own marked block.
Existing profile bytes are kept; each existing profile gets a one-time
`.before-codex-desktop` backup. On Windows, profile locations are obtained from
PowerShell itself, including redirected Documents folders. zsh/bash are supported;
fish is configured if installed. Other shells can invoke `codex-desktop` directly.

If `CODEX_CLI_PATH` already points to another integration, setup stops with its
path. Disable that integration first. The bridge does not silently take it over.

For a nonstandard macOS installation:

```sh
codex-desktop-setup enable --app ~/Applications/ChatGPT.app
```

`--cli /path/to/codex` overrides app runtime discovery. Windows normally uses
`auto` so hashed app runtime directories are discovered again on every launch;
avoid pinning a Windows app runtime manually.

## Chezmoi and Git submodules

Copy the appropriate `run_onchange_after_*` file from
[`examples/chezmoi`](examples/chezmoi) into your source directory. It installs the
pinned release and enables the app bridge. Manage the shell function in your own
shell config, or run `codex-desktop-setup enable --shells` once.

To track the project as a submodule in an existing dotfiles checkout:

```sh
git submodule add https://github.com/k3k8/codex-desktop.git vendor/codex-desktop
git -C vendor/codex-desktop checkout v0.1.0
```

Add `vendor/` to your `.chezmoiignore`; the submodule contains source code, not
chezmoi-encoded home files. Install the checked-out source using:

```sh
uv tool install --python 3.11 --from ./vendor/codex-desktop codex-desktop-bridge
codex-desktop-setup enable
```

Update the release URL in the chezmoi script, or update the pinned submodule,
when upgrading. Commit the changed pin in your dotfiles repository.

## Commands and bypass

- `codex-desktop`: interactive terminal entry; automatically discovers one live
  desktop server. If none or several exist, it stops with an explanation.
- `codex-desktop-app`: app entry, set as `CODEX_CLI_PATH` by setup.
- `codex-desktop-setup status`: shows the settings path and configured app entry.
- `codex-desktop-setup disable`: removes the marked shell blocks and app override.
  Restart the app and terminal afterward.

Management commands such as `login`, `mcp`, `app-server`, and `--help` pass through
to the real CLI without requiring a running app. `exec`, `review`, and `cloud`
should use the native CLI directly. With the shell function installed, bypass it
with `command codex ...` in zsh/bash/fish, or an explicit executable path in
PowerShell, e.g. `& (Get-Command codex.exe -CommandType Application).Source exec ...`.

To uninstall, disable **before** removing the package:

```sh
codex-desktop-setup disable
uv tool uninstall codex-desktop-bridge
```

## Transport and Windows updates

On macOS the bridge uses a Unix socket in a private temporary directory. On
Windows it uses a random loopback-only WebSocket port with a capability token in
a per-user ACL-protected directory. Tokens go to the terminal child through its
environment, never command-line arguments. Connection records live under
`$CODEX_HOME/codex-desktop/instances` (`~/.codex` by default). They are removed on
normal exit; stale records are rejected using process and socket checks.

Windows app updates replace hashed runtime directories and may remove helper
executables while an old `codex.exe` is still running. The bridge prefers a runtime
with its helpers present, orders versions by arrival time as well as packaged
mtime, and holds open handles to its runtime executables/libraries and the app's
PATH runtime folders. Updates cannot delete these files until the bridge exits.
A Windows Job Object with kill-on-close ends the backend when its wrapper is
killed, avoiding orphaned servers that keep conversations locked. A job assignment
failure is reported on stderr.

The update and abrupt-exit tests run on native Windows in CI. A real Microsoft
Store app update still requires a manual smoke test; simulated deletion and
process termination are the automated coverage. This does not make experimental
Codex transport flags a stable public API.

## Development

```sh
uv venv
uv pip install .
.venv/bin/python -m unittest discover -s tests -v
```

On Windows use `.venv\Scripts\python.exe`. Tests do not invoke a model or use real
authentication tokens. To test two clients against a real app-bundled CLI without
model calls, set `CODEX_DESKTOP_TEST_CLI` to its absolute path and rerun the tests.
