#!/usr/bin/env bash
# Installs Hermes Agent at one tag or commit, for the upstream contract tests.
#
#   upstream/install-hermes.sh <ref> <dir>
#
# Hermes refuses wheel builds, so this is a checkout with its own locked
# environment (`uv sync --frozen`), the way its developers run it. The web UI
# is not built: the tests only talk to the API, and `hermes dashboard
# --skip-build` serves whatever dist it finds, so a stub page stands in.
# pytest and PyYAML (tests/requirements.txt's versions) go into Hermes' own
# environment, because the tests start the
# dashboard with that Python and import nothing else. The websocket client
# comes from Hermes' own lock, so it is checked rather than pinned over it.
# HERMES_REPO overrides the upstream URL (a fork or a mirror).
set -euo pipefail

ref="${1:?usage: install-hermes.sh <ref> <dir>}"
dir="${2:?usage: install-hermes.sh <ref> <dir>}"
case "$ref" in
    -*) echo "install-hermes.sh: '$ref' is not a ref" >&2; exit 2 ;;
esac

if [ ! -d "$dir/.git" ]; then
    git clone --quiet --filter=blob:none "${HERMES_REPO:-https://github.com/NousResearch/hermes-agent}" "$dir"
fi
git -C "$dir" fetch --quiet --tags origin
git -C "$dir" checkout --quiet --detach "$ref"
(cd "$dir" && uv sync --frozen --quiet)
uv pip install --quiet --python "$dir/.venv/bin/python" pytest==9.1.1 pyyaml==6.0.3
if ! "$dir/.venv/bin/python" -c 'import websockets' 2>/dev/null; then
    echo "install-hermes.sh: Hermes $ref doesn't ship the websockets package the tests use" >&2
    exit 1
fi

dist="$dir/hermes_cli/web_dist"
if [ ! -f "$dist/index.html" ]; then
    mkdir -p "$dist"
    printf '<!doctype html><title>Hermes</title>\n' > "$dist/index.html"
fi

echo "Hermes $(git -C "$dir" describe --tags --always) at $dir"
