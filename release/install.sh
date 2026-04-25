#!/usr/bin/env bash

set -euo pipefail

usage() {
    cat <<'EOF'
Usage: ./install.sh [--current-env] [--python /path/to/python3] [--venv-dir PATH]

Default behavior creates a local virtualenv at ./.venv inside the unpacked
release directory and installs all wheels from ./wheelhouse offline.

Options:
  --current-env        Install into the current Python environment instead.
  --python PATH        Python interpreter to use (default: python3).
  --venv-dir PATH      Target virtualenv directory (default: ./.venv).
  --help               Show this help.
EOF
}

ARTIFACT_ROOT="$(cd "$(dirname "$0")" && pwd)"
WHEELHOUSE="$ARTIFACT_ROOT/wheelhouse"
PYTHON_BIN="python3"
USE_CURRENT_ENV=0
VENV_DIR="$ARTIFACT_ROOT/.venv"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --current-env)
            USE_CURRENT_ENV=1
            shift
            ;;
        --python)
            PYTHON_BIN="$2"
            shift 2
            ;;
        --venv-dir)
            VENV_DIR="$2"
            shift 2
            ;;
        --help|-h)
            usage
            exit 0
            ;;
        *)
            echo "Unknown argument: $1" >&2
            usage >&2
            exit 1
            ;;
    esac
done

if [[ ! -d "$WHEELHOUSE" ]]; then
    echo "wheelhouse directory not found: $WHEELHOUSE" >&2
    exit 1
fi

if [[ $USE_CURRENT_ENV -eq 1 ]]; then
    INSTALL_PYTHON="$PYTHON_BIN"
else
    "$PYTHON_BIN" -m venv "$VENV_DIR"
    INSTALL_PYTHON="$VENV_DIR/bin/python"
fi

install_wheel() {
    local pattern="$1"
    local matches=()
    shopt -s nullglob
    matches=("$WHEELHOUSE"/$pattern)
    shopt -u nullglob

    if [[ ${#matches[@]} -ne 1 ]]; then
        echo "Expected exactly one wheel matching $pattern in $WHEELHOUSE" >&2
        exit 1
    fi

    "$INSTALL_PYTHON" -m pip install --no-index --find-links "$WHEELHOUSE" --no-deps "${matches[0]}"
}

if [[ -f "$WHEELHOUSE/requirements-offline.txt" ]]; then
    "$INSTALL_PYTHON" -m pip install --no-index --find-links "$WHEELHOUSE" -r "$WHEELHOUSE/requirements-offline.txt"
fi

install_wheel 'c2pa_python-*.whl'
install_wheel 'stardustproof_keystore-*.whl'
install_wheel 'stardustproof_c2pa_signer-*.whl'
install_wheel 'stardustproof_cli-*.whl'

if [[ $USE_CURRENT_ENV -eq 1 ]]; then
    echo "Installed into current environment."
    echo "Run: stardustproof --help"
else
    echo "Installed into virtualenv: $VENV_DIR"
    echo "Run: $VENV_DIR/bin/stardustproof --help"
fi
