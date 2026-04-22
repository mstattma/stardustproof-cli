from __future__ import annotations

import argparse
import base64
import os
from pathlib import Path

import pytest

from stardustproof_cli.cli import cmd_sign


_PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9WlH0x0AAAAASUVORK5CYII="
)


@pytest.mark.integration
def test_sign_smoke_with_real_keystore(tmp_path: Path):
    keystore_url = os.environ.get("STARDUSTPROOF_TEST_KEYSTORE_URL")
    org_uuid = os.environ.get("STARDUSTPROOF_TEST_ORG_UUID")
    access_token = os.environ.get("STARDUSTPROOF_TEST_SIGNING_ACCESS_TOKEN")
    bin_dir = os.environ.get("STARDUSTPROOF_TEST_BIN_DIR")

    if not all([keystore_url, org_uuid, access_token, bin_dir]):
        pytest.skip("Set STARDUSTPROOF_TEST_KEYSTORE_URL, STARDUSTPROOF_TEST_ORG_UUID, STARDUSTPROOF_TEST_SIGNING_ACCESS_TOKEN, and STARDUSTPROOF_TEST_BIN_DIR")

    input_path = tmp_path / "input.png"
    output_path = tmp_path / "signed.png"
    manifest_store = tmp_path / "manifest-store"
    input_path.write_bytes(_PNG_1X1)

    args = argparse.Namespace(
        command="sign",
        input=str(input_path),
        output=str(output_path),
        wm_payload_hex="00112233445566778899aabbccddeeff001122334455",
        wm_bit_profile=192,
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

    rc = cmd_sign(args)

    assert rc == 0
    assert output_path.exists()
    assert (manifest_store / "00112233445566778899aabbccddeeff001122334455.c2pa").exists()
