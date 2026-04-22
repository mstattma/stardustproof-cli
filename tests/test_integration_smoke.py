from __future__ import annotations

import argparse
import os
from pathlib import Path

import pytest

from stardustproof_cli.cli import cmd_sign


FIXTURES_DIR = Path(__file__).parent / "fixtures"


def _smoke_env() -> tuple[str, str, str, str]:
    keystore_url = os.environ.get("STARDUSTPROOF_TEST_KEYSTORE_URL")
    org_uuid = os.environ.get("STARDUSTPROOF_TEST_ORG_UUID")
    access_token = os.environ.get("STARDUSTPROOF_TEST_SIGNING_ACCESS_TOKEN")
    bin_dir = os.environ.get("STARDUSTPROOF_TEST_BIN_DIR")

    if not all([keystore_url, org_uuid, access_token, bin_dir]):
        pytest.skip("Set STARDUSTPROOF_TEST_KEYSTORE_URL, STARDUSTPROOF_TEST_ORG_UUID, STARDUSTPROOF_TEST_SIGNING_ACCESS_TOKEN, and STARDUSTPROOF_TEST_BIN_DIR")
    return keystore_url, org_uuid, access_token, bin_dir


def _build_sign_args(
    *,
    input_path: Path,
    output_path: Path,
    manifest_store: Path,
    org_uuid: str,
    keystore_url: str,
    access_token: str,
    bin_dir: str,
    wm_payload_hex: str,
) -> argparse.Namespace:
    return argparse.Namespace(
        command="sign",
        input=str(input_path),
        output=str(output_path),
        wm_payload_hex=wm_payload_hex,
        wm_bit_profile=len(bytes.fromhex(wm_payload_hex)) * 8,
        manifest_store=str(manifest_store),
        org_uuid=org_uuid,
        keystore_url=keystore_url,
        keystore_api_key="",
        signing_access_token=access_token,
        claim_generator_name="STARDUSTproof CLI Test",
        claim_generator_version="1.0",
        overwrite_manifest=False,
        thumbnail=False,
        bin_dir=bin_dir,
        strength=None,
        sp_width=None,
        sp_height=None,
        sp_density=None,
        p_density=None,
        pm_mode=None,
        seed=None,
        fec=None,
    )


@pytest.mark.integration
def test_sign_image_smoke_with_real_keystore(tmp_path: Path):
    keystore_url, org_uuid, access_token, bin_dir = _smoke_env()

    input_path = FIXTURES_DIR / "sample-photo.jpg"
    output_path = tmp_path / "signed.jpg"
    manifest_store = tmp_path / "manifest-store"
    wm_payload_hex = "00112233445566778899aabbccddeeff001122334455"
    args = _build_sign_args(
        input_path=input_path,
        output_path=output_path,
        manifest_store=manifest_store,
        org_uuid=org_uuid,
        keystore_url=keystore_url,
        access_token=access_token,
        bin_dir=bin_dir,
        wm_payload_hex=wm_payload_hex,
    )

    rc = cmd_sign(args)

    assert rc == 0
    assert output_path.exists()
    assert (manifest_store / f"{wm_payload_hex}.c2pa").exists()


@pytest.mark.integration
def test_sign_video_smoke_with_real_keystore(tmp_path: Path):
    keystore_url, org_uuid, access_token, bin_dir = _smoke_env()

    input_path = FIXTURES_DIR / "big-buck-bunny-trailer-1080p.mov"
    output_path = tmp_path / "signed.mov"
    manifest_store = tmp_path / "manifest-store"
    wm_payload_hex = "00112233445566778899aabbccddeeff001122334455"
    args = _build_sign_args(
        input_path=input_path,
        output_path=output_path,
        manifest_store=manifest_store,
        org_uuid=org_uuid,
        keystore_url=keystore_url,
        access_token=access_token,
        bin_dir=bin_dir,
        wm_payload_hex=wm_payload_hex,
    )

    rc = cmd_sign(args)

    assert rc == 0
    assert output_path.exists()
    assert (manifest_store / f"{wm_payload_hex}.c2pa").exists()
