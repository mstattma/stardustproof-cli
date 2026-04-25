from __future__ import annotations

import importlib.util
import subprocess
import sys
import types
import zipfile
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
BUILD_RELEASE_PATH = REPO_ROOT / "scripts" / "build_release.py"


def _load_build_release_module() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location("build_release", BUILD_RELEASE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


build_release = _load_build_release_module()


def _build_module_is_runnable() -> bool:
    result = subprocess.run(
        [sys.executable, "-m", "build", "--version"],
        cwd=Path(__file__).resolve().parent,
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


@pytest.mark.skipif(
    not _build_module_is_runnable(),
    reason="build package is not installed in this test environment",
)
def test_cli_wheel_contains_expected_bundled_assets(tmp_path: Path):
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()

    subprocess.run(
        [
            sys.executable,
            "-m",
            "build",
            "--wheel",
            "--outdir",
            str(wheelhouse),
            str(REPO_ROOT),
        ],
        cwd=tmp_path,
        check=True,
    )

    wheels = list(wheelhouse.glob("stardustproof_cli-*.whl"))
    assert len(wheels) == 1

    with zipfile.ZipFile(wheels[0]) as zf:
        names = set(zf.namelist())

    missing = sorted(build_release.CLI_BUNDLED_WHEEL_PATHS - names)
    assert missing == []
