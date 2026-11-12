#!/bin/sh
set -eu
# uv supplies an isolated Python and stable console entry points; no sudo required.
command -v uv >/dev/null 2>&1 || { echo 'Install uv first: https://docs.astral.sh/uv/getting-started/installation/' >&2; exit 1; }
source_url=${CODEX_DESKTOP_SOURCE:-https://github.com/k3k8/codex-desktop/archive/refs/tags/v0.1.0.tar.gz}
uv tool install --force --python 3.11 --from "$source_url" codex-desktop-bridge
bin_dir=$(uv tool dir --bin)
if [ "${CODEX_DESKTOP_SKIP_ENABLE:-0}" != 1 ]; then
    "$bin_dir/codex-desktop-setup" enable --shells
fi
printf 'Installed in %s. Add this directory to PATH if needed.\n' "$bin_dir"
