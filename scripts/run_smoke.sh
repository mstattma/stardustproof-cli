#!/bin/bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

cd "$REPO_ROOT"

: "${STARDUSTPROOF_TEST_KEYSTORE_URL:=http://localhost:2001}"
: "${STARDUSTPROOF_TEST_BIN_DIR:=$REPO_ROOT/bin}"

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

PYTHONPATH=src pytest tests/test_integration_smoke.py -m integration -q
