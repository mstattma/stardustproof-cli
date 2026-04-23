"""Unit tests for the verify pipeline.

These tests monkeypatch the two external dependencies of ``verify_asset``:

- ``stardust.extract_blind`` (Stardust blind extraction)
- ``stardustproof_c2pa_signer.c2patool.verify_detached_manifest``
  (c2patool subprocess wrapper)

so the tests run without needing ffmpeg, the keystore, or a real c2patool
binary. The integration smoke exercises the real end-to-end path.
"""

from __future__ import annotations

import argparse
import io
import json
import sys
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace

import pytest

from stardustproof_cli import stardust, verify as verify_mod
from stardustproof_cli.cli import cmd_verify
from stardustproof_cli.config import StardustConfig, StardustPaths


SMOKE_WM_HEX = "001122334455"


@pytest.fixture
def stub_config(monkeypatch) -> StardustConfig:
    """Return a StardustConfig that passes check_binaries() in tests."""

    config = StardustConfig(paths=StardustPaths())
    # Bypass the strict on-disk binary check.
    monkeypatch.setattr(stardust, "check_binaries", lambda paths: [])
    return config


@pytest.fixture
def asset_and_store(tmp_path: Path):
    """Provide an existing input file and an existing manifest store
    directory; tests opt into writing the manifest file as needed."""

    asset = tmp_path / "signed.jpg"
    asset.write_bytes(b"fake-jpeg-bytes")
    store = tmp_path / "store"
    store.mkdir()
    return asset, store


def _good_report(wm_hex: str, extra_success: bool = True) -> dict:
    return {
        "active_manifest": "urn:c2pa:abc",
        "manifests": {
            "urn:c2pa:abc": {
                "assertion_store": {
                    "c2pa.soft-binding": {
                        "alg": verify_mod.WATERMARK_ALG,
                        "blocks": [{"scope": {}, "value": wm_hex}],
                    }
                }
            }
        },
        "validation_state": "Trusted",
        "validation_results": {
            "activeManifest": {
                "success": [
                    {"code": "signingCredential.trusted"}
                ] if extra_success else [],
                "failure": [],
                "informational": [],
            }
        },
    }


def _fake_c2patool_result(report: dict | None, returncode: int = 0, stderr: str = ""):
    return SimpleNamespace(
        returncode=returncode,
        stdout=json.dumps(report) if report is not None else "",
        stderr=stderr,
        report=report,
    )


def _install_fake_verify(monkeypatch, report, returncode: int = 0, stderr: str = ""):
    """Patch the signer's verify_detached_manifest to avoid shelling out."""

    try:
        import stardustproof_c2pa_signer.c2patool as c2patool_mod
    except Exception:  # pragma: no cover
        pytest.skip("stardustproof_c2pa_signer.c2patool not importable")

    def _fake(**kwargs):
        return _fake_c2patool_result(report, returncode=returncode, stderr=stderr)

    monkeypatch.setattr(c2patool_mod, "verify_detached_manifest", _fake)
    # write_cawg_trust_settings also reads PEM files -- stub it so we
    # don't need real trust anchors.
    monkeypatch.setattr(
        c2patool_mod,
        "write_cawg_trust_settings",
        lambda target_path, **kw: Path(target_path).write_text("# stub\n") or Path(target_path),
    )


# ---- verify_asset() ------------------------------------------------------


def test_verify_asset_extract_returns_none_exit_2(monkeypatch, stub_config, asset_and_store):
    asset, store = asset_and_store
    monkeypatch.setattr(stardust, "extract_blind", lambda *a, **kw: None)

    result = verify_mod.verify_asset(
        input_path=asset, manifest_store=store,
        config=stub_config, wm_bit_profile=48, check_trust=False,
    )
    assert result.ok is False
    assert result.exit_code == 2
    assert result.wm_id_hex is None
    assert "no watermark id" in (result.error or "").lower()


def test_verify_asset_manifest_missing_exit_3(monkeypatch, stub_config, asset_and_store):
    asset, store = asset_and_store
    monkeypatch.setattr(stardust, "extract_blind", lambda *a, **kw: SMOKE_WM_HEX)

    result = verify_mod.verify_asset(
        input_path=asset, manifest_store=store,
        config=stub_config, wm_bit_profile=48, check_trust=False,
    )
    assert result.ok is False
    assert result.exit_code == 3
    assert result.wm_id_hex == SMOKE_WM_HEX
    assert "No manifest found" in (result.error or "")


def test_verify_asset_c2patool_failure_exit_4(monkeypatch, stub_config, asset_and_store):
    asset, store = asset_and_store
    (store / f"{SMOKE_WM_HEX}.c2pa").write_bytes(b"fake")
    monkeypatch.setattr(stardust, "extract_blind", lambda *a, **kw: SMOKE_WM_HEX)
    _install_fake_verify(monkeypatch, report=None, returncode=2, stderr="c2patool: bad input")

    result = verify_mod.verify_asset(
        input_path=asset, manifest_store=store,
        config=stub_config, wm_bit_profile=48, check_trust=False,
    )
    assert result.ok is False
    assert result.exit_code == 4
    assert "c2patool exited 2" in (result.error or "")


def test_verify_asset_missing_soft_binding_exit_5(monkeypatch, stub_config, asset_and_store):
    asset, store = asset_and_store
    (store / f"{SMOKE_WM_HEX}.c2pa").write_bytes(b"fake")
    monkeypatch.setattr(stardust, "extract_blind", lambda *a, **kw: SMOKE_WM_HEX)
    bad_report = {
        "active_manifest": "m",
        "manifests": {"m": {"assertion_store": {}}},
        "validation_results": {
            "activeManifest": {"success": [{"code": "ok"}], "failure": [], "informational": []}
        },
    }
    _install_fake_verify(monkeypatch, report=bad_report)

    result = verify_mod.verify_asset(
        input_path=asset, manifest_store=store,
        config=stub_config, wm_bit_profile=48, check_trust=False,
    )
    assert result.ok is False
    assert result.exit_code == 5
    assert "no c2pa.soft-binding" in (result.error or "")


def test_verify_asset_soft_binding_value_mismatch_exit_5(monkeypatch, stub_config, asset_and_store):
    asset, store = asset_and_store
    (store / f"{SMOKE_WM_HEX}.c2pa").write_bytes(b"fake")
    monkeypatch.setattr(stardust, "extract_blind", lambda *a, **kw: SMOKE_WM_HEX)
    wrong_report = _good_report("deadbeefcafe")  # different hex than extracted
    _install_fake_verify(monkeypatch, report=wrong_report)

    result = verify_mod.verify_asset(
        input_path=asset, manifest_store=store,
        config=stub_config, wm_bit_profile=48, check_trust=False,
    )
    assert result.ok is False
    assert result.exit_code == 5
    assert "does not match" in (result.error or "")


def test_verify_asset_validation_failure_exit_6(monkeypatch, stub_config, asset_and_store):
    asset, store = asset_and_store
    (store / f"{SMOKE_WM_HEX}.c2pa").write_bytes(b"fake")
    monkeypatch.setattr(stardust, "extract_blind", lambda *a, **kw: SMOKE_WM_HEX)
    report = _good_report(SMOKE_WM_HEX)
    report["validation_results"]["activeManifest"]["failure"] = [
        {"code": "assertion.dataHash.mismatch", "explanation": "hash mismatch"}
    ]
    _install_fake_verify(monkeypatch, report=report)

    result = verify_mod.verify_asset(
        input_path=asset, manifest_store=store,
        config=stub_config, wm_bit_profile=48, check_trust=False,
    )
    assert result.ok is False
    assert result.exit_code == 6
    assert result.failure and result.failure[0]["code"] == "assertion.dataHash.mismatch"


def test_verify_asset_happy_path_exit_0(monkeypatch, stub_config, asset_and_store):
    asset, store = asset_and_store
    (store / f"{SMOKE_WM_HEX}.c2pa").write_bytes(b"fake")
    monkeypatch.setattr(stardust, "extract_blind", lambda *a, **kw: SMOKE_WM_HEX)
    report = _good_report(SMOKE_WM_HEX)
    _install_fake_verify(monkeypatch, report=report)

    result = verify_mod.verify_asset(
        input_path=asset, manifest_store=store,
        config=stub_config, wm_bit_profile=48, check_trust=False,
    )
    assert result.ok is True
    assert result.exit_code == 0
    assert result.wm_id_hex == SMOKE_WM_HEX
    assert result.manifest_path == store / f"{SMOKE_WM_HEX}.c2pa"
    assert result.soft_binding["data"]["value"] == SMOKE_WM_HEX
    assert result.validation_state == "Trusted"
    assert result.failure == []


def test_verify_asset_missing_input_exit_1(stub_config, tmp_path):
    result = verify_mod.verify_asset(
        input_path=tmp_path / "nope.jpg",
        manifest_store=tmp_path,
        config=stub_config, wm_bit_profile=48, check_trust=False,
    )
    assert result.ok is False and result.exit_code == 1
    assert "Input asset not found" in (result.error or "")


def test_verify_asset_trust_defaults_missing_exit_1(
    monkeypatch, stub_config, asset_and_store
):
    asset, store = asset_and_store
    monkeypatch.setattr(
        verify_mod, "resolve_default_trust_anchor_paths", lambda: None
    )
    # check_trust=True (default) with no --trust-anchors and no auto-discovery
    # must exit 1 with a clear error.
    result = verify_mod.verify_asset(
        input_path=asset, manifest_store=store,
        config=stub_config, wm_bit_profile=48, check_trust=True,
    )
    assert result.ok is False and result.exit_code == 1
    assert "No --trust-anchors" in (result.error or "")


def test_verify_asset_trust_anchor_file_missing_exit_1(
    monkeypatch, stub_config, asset_and_store, tmp_path
):
    asset, store = asset_and_store
    # Supplying a non-existent path should short-circuit before extract.
    result = verify_mod.verify_asset(
        input_path=asset, manifest_store=store,
        config=stub_config, wm_bit_profile=48,
        trust_anchors=[tmp_path / "does-not-exist.pem"],
        check_trust=True,
    )
    assert result.ok is False and result.exit_code == 1
    assert "Trust anchor PEM not found" in (result.error or "")


# ---- cmd_verify() + JSON output ------------------------------------------


def _make_verify_args(**overrides) -> argparse.Namespace:
    base = dict(
        command="verify",
        input="",
        manifest_store="",
        wm_bit_profile=48,
        trust_anchors=None,
        cawg_trust_anchors=None,
        no_trust=True,
        bin_dir=None,
        json_output=False,
        strength=None,
        sp_width=None,
        sp_height=None,
        sp_density=None,
        p_density=None,
        pm_mode=None,
        seed=None,
        fec=None,
    )
    base.update(overrides)
    return argparse.Namespace(**base)


def test_cmd_verify_human_output_happy(monkeypatch, asset_and_store):
    asset, store = asset_and_store
    (store / f"{SMOKE_WM_HEX}.c2pa").write_bytes(b"fake")
    monkeypatch.setattr(stardust, "check_binaries", lambda paths: [])
    monkeypatch.setattr(stardust, "extract_blind", lambda *a, **kw: SMOKE_WM_HEX)
    _install_fake_verify(monkeypatch, report=_good_report(SMOKE_WM_HEX))

    args = _make_verify_args(
        input=str(asset), manifest_store=str(store),
    )

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cmd_verify(args)
    out = buf.getvalue()

    assert rc == 0
    assert "OK: asset + manifest verify cleanly" in out
    assert "[verify] Blind extraction: 001122334455" in out
    assert "[verify] Soft-binding: alg=castlabs.stardust value=001122334455" in out
    assert "validation_state: Trusted" in out


def test_cmd_verify_json_output_shape(monkeypatch, asset_and_store):
    asset, store = asset_and_store
    (store / f"{SMOKE_WM_HEX}.c2pa").write_bytes(b"fake")
    monkeypatch.setattr(stardust, "check_binaries", lambda paths: [])
    monkeypatch.setattr(stardust, "extract_blind", lambda *a, **kw: SMOKE_WM_HEX)
    _install_fake_verify(monkeypatch, report=_good_report(SMOKE_WM_HEX))

    args = _make_verify_args(
        input=str(asset), manifest_store=str(store), json_output=True,
    )

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cmd_verify(args)
    line = buf.getvalue().strip()

    assert rc == 0
    payload = json.loads(line)
    assert payload["ok"] is True
    assert payload["exit_code"] == 0
    assert payload["wm_id_hex"] == SMOKE_WM_HEX
    assert payload["soft_binding"]["data"]["value"] == SMOKE_WM_HEX
    assert payload["validation_state"] == "Trusted"
    assert payload["failure"] == []
    assert "total_s" in payload["timings"]


def test_cmd_verify_exit_code_surfaces_to_caller(monkeypatch, asset_and_store):
    asset, store = asset_and_store
    monkeypatch.setattr(stardust, "check_binaries", lambda paths: [])
    monkeypatch.setattr(stardust, "extract_blind", lambda *a, **kw: None)

    args = _make_verify_args(input=str(asset), manifest_store=str(store))

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cmd_verify(args)
    out = buf.getvalue()

    assert rc == 2
    assert "FAIL (exit 2)" in out
