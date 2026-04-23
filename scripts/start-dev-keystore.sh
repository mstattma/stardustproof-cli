#!/usr/bin/env bash
#
# Thin wrapper that forwards into the canonical start-dev-keystore.sh
# shipped with stardustproof-keystore. Kept here so CLI smoke
# prerequisites ("start the dev keystore with the signing access
# token set") live under a single well-known path per repo, without
# duplicating the daemonization / .env parsing logic.
#
# Discovery order for the keystore repo:
#   1. $STARDUSTPROOF_KEYSTORE_REPO (explicit env override).
#   2. Sibling checkout at ../stardustproof-keystore (the standard
#      layout used by the smoke prerequisites documented in
#      AGENTS.md and the CLI README).
#
# All args are forwarded verbatim, so you can run:
#
#     scripts/start-dev-keystore.sh           # --start (default)
#     scripts/start-dev-keystore.sh --stop
#     scripts/start-dev-keystore.sh --status
#     scripts/start-dev-keystore.sh --restart
#
# The CLI smoke at tests/test_integration_smoke.py expects the
# keystore to already be up on http://localhost:2001/ with
# KEYSTORE_DEV_SIGNING_ACCESS_TOKEN=<matches .env.smoke.local>
# before pytest runs; this shim is the one-liner that gets you
# there without remembering the full invocation.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CLI_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

KEYSTORE_REPO="${STARDUSTPROOF_KEYSTORE_REPO:-$CLI_ROOT/../stardustproof-keystore}"

if [[ ! -d "$KEYSTORE_REPO" ]]; then
    echo "error: keystore repo not found at $KEYSTORE_REPO" >&2
    echo "Set STARDUSTPROOF_KEYSTORE_REPO to an explicit path, or" >&2
    echo "clone https://github.com/mstattma/stardustproof-keystore next to this repo." >&2
    exit 1
fi

CANONICAL="$KEYSTORE_REPO/scripts/start-dev-keystore.sh"
if [[ ! -x "$CANONICAL" ]]; then
    echo "error: $CANONICAL is not an executable script" >&2
    echo "Update the keystore checkout (git pull) or mark the script" >&2
    echo "executable: chmod +x $CANONICAL" >&2
    exit 1
fi

exec "$CANONICAL" "$@"
