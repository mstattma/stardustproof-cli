#!/bin/bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

cd "$REPO_ROOT"

if [[ -f "$REPO_ROOT/.env.smoke.example" ]]; then
    set -a
    # shellcheck disable=SC1091
    source "$REPO_ROOT/.env.smoke.example"
    set +a
fi

if [[ -f "$REPO_ROOT/.env.smoke.local" ]]; then
    set -a
    # shellcheck disable=SC1091
    source "$REPO_ROOT/.env.smoke.local"
    set +a
fi

: "${STARDUSTPROOF_TEST_KEYSTORE_URL:=http://localhost:2001}"
: "${STARDUSTPROOF_TEST_BIN_DIR:=./bin}"

if [[ "$STARDUSTPROOF_TEST_BIN_DIR" != /* ]]; then
    STARDUSTPROOF_TEST_BIN_DIR="$REPO_ROOT/$STARDUSTPROOF_TEST_BIN_DIR"
fi

if [[ -z "${STARDUSTPROOF_TEST_ORG_UUID:-}" ]]; then
    echo "STARDUSTPROOF_TEST_ORG_UUID is required" >&2
    exit 1
fi

if [[ -z "${STARDUSTPROOF_TEST_SIGNING_ACCESS_TOKEN:-}" ]]; then
    echo "STARDUSTPROOF_TEST_SIGNING_ACCESS_TOKEN is required" >&2
    exit 1
fi

export STARDUSTPROOF_TEST_KEYSTORE_URL
export STARDUSTPROOF_TEST_BIN_DIR
export STARDUSTPROOF_TEST_ORG_UUID
export STARDUSTPROOF_TEST_SIGNING_ACCESS_TOKEN
# Propagate optional c2patool override when caller has one set.
if [[ -n "${STARDUSTPROOF_C2PATOOL:-}" ]]; then
    export STARDUSTPROOF_C2PATOOL
fi

echo "[smoke] Repo: $REPO_ROOT"
echo "[smoke] Keystore URL: $STARDUSTPROOF_TEST_KEYSTORE_URL"
echo "[smoke] Bin dir: $STARDUSTPROOF_TEST_BIN_DIR"
echo "[smoke] Starting integration smoke run at $(date -Iseconds)"

PYTHONPATH=src pytest tests/test_integration_smoke.py -m integration -s -vv
